"""Generic SCPI-over-socket analyzer.

Most swept analyzers speak nearly the same SCPI. A driver for a new instrument is
usually a subclass that only overrides the ``CMD`` table (and maybe ``parse_trace``
or ``STATE``); see siglent.py for an example and README.md for a walk-through."""
from __future__ import annotations

import socket
import threading
import time

import numpy as np

from .base import AnalyzerError, Cancelled, SpectrumAnalyzer


class SocketTransport:
    """Line-oriented SCPI over a raw TCP socket (usually port 5025)."""

    def __init__(self, host: str, port: int = 5025, timeout: float = 10.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock: socket.socket | None = None
        self._buf = b""

    def open(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise AnalyzerError(f"cannot connect to {self.host}:{self.port}: {e}") from e
        self._buf = b""

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def write(self, cmd: str):
        if not self.sock:
            raise AnalyzerError("not connected")
        self.sock.sendall((cmd + "\n").encode())

    def readline(self, timeout: float | None = None, cancel: threading.Event | None = None) -> str:
        """Read one response line. Polls in short slices so a cancel request is noticed;
        the response is still consumed after a cancel to keep the protocol in sync."""
        if not self.sock:
            raise AnalyzerError("not connected")
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        cancelled = False
        self.sock.settimeout(0.25)
        try:
            while b"\n" not in self._buf:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                if time.monotonic() > deadline:
                    raise AnalyzerError("timeout waiting for analyzer response")
                try:
                    chunk = self.sock.recv(1 << 20)
                except socket.timeout:
                    continue
                if not chunk:
                    raise AnalyzerError("connection closed by analyzer")
                self._buf += chunk
        finally:
            self.sock.settimeout(self.timeout)
        line, _, self._buf = self._buf.partition(b"\n")
        if cancelled:
            raise Cancelled()
        return line.decode(errors="replace").strip()

    def query(self, cmd: str, timeout: float | None = None, cancel=None) -> str:
        self.write(cmd)
        return self.readline(timeout, cancel)


class ScpiSpectrumAnalyzer(SpectrumAnalyzer):
    """SpectrumAnalyzer implemented with a table of SCPI commands.

    Each ``CMD`` entry is a format string; setters receive the value as ``{v}``.
    ``STATE`` lists the query headers (without '?') saved before and restored after a
    measurement, in restore order. Manual values are restored before their ``...:AUTO``
    flag so that 'auto' wins where it was on."""

    default_port = 5025

    CMD = {
        "idn": "*IDN?",
        "opc": "*OPC?",
        "error": ":SYST:ERR?",
        "continuous_off": ":INIT:CONT OFF",
        "trace_write": ":TRAC1:MODE WRIT",
        "detector_rms": ":SENS:DET:TRAC1 AVER",
        "average_power": ":SENS:AVER:TYPE POW",
        "sweep_time_auto": ":SENS:SWE:TIME:AUTO ON",
        "center": ":SENS:FREQ:CENT {v:.3f}",
        "span": ":SENS:FREQ:SPAN {v:.3f}",
        "rbw": ":SENS:BWID:RES {v:g}",
        "vbw": ":SENS:BWID:VID {v:g}",
        "center?": ":SENS:FREQ:CENT?",
        "span?": ":SENS:FREQ:SPAN?",
        "sweep_time?": ":SENS:SWE:TIME?",
        "ref_level": ":DISP:WIND:TRAC:Y:RLEV {v:.1f}",
        "ref_level?": ":DISP:WIND:TRAC:Y:RLEV?",
        "init": ":INIT:IMM",
        "trace?": ":TRAC:DATA? 1",
        "local": ":SYST:LOC",  # back to front-panel control; None if not supported
    }
    STATE: list[str] = []

    def __init__(self, host: str, port: int | None = None, timeout: float = 10.0):
        self.io = SocketTransport(host, port or self.default_port, timeout)

    # transport shortcuts
    def write(self, cmd):
        self.io.write(cmd)

    def query(self, cmd, timeout=None, cancel=None):
        return self.io.query(cmd, timeout, cancel)

    def cmd(self, key, v=None):
        self.write(self.CMD[key].format(v=v))

    def ask(self, key, timeout=None, cancel=None) -> str:
        return self.query(self.CMD[key], timeout, cancel)

    def askf(self, key) -> float:
        return float(self.ask(key))

    def wait(self, timeout=None, cancel=None):
        self.ask("opc", timeout, cancel)

    # SpectrumAnalyzer
    def connect(self):
        self.io.open()

    def close(self):
        """Return the instrument to local (front panel) control, then disconnect."""
        try:
            if self.io.sock and self.CMD.get("local"):
                # must be the very last command: any later command (even *OPC?) puts the
                # instrument back into remote
                self.cmd("local")
                time.sleep(0.2)
        except Exception:  # noqa: BLE001 - closing must not fail
            pass
        finally:
            self.io.close()

    def identify(self):
        return self.ask("idn")

    def save_state(self):
        return {c: self.query(c + "?") for c in self.STATE}

    def restore_state(self, state):
        for c in self.STATE:
            if c not in state:
                continue
            v = state[c].strip()
            if c.endswith(":AUTO") and v not in ("1", "ON"):
                continue
            self.write(f"{c} {v}")
        self.wait()
        # Re-enabling continuous mode alone leaves the display frozen (seen on the Siglent
        # SSA3000X): start a sweep so it keeps running.
        if any(c.startswith(":INIT:CONT") and state[c].strip() in ("1", "ON") for c in state):
            self.cmd("init")
            self.wait()

    def setup_noise_mode(self):
        for k in ("continuous_off", "trace_write", "detector_rms", "average_power",
                  "sweep_time_auto"):
            self.cmd(k)
        self.wait()

    def configure(self, center, span, rbw, vbw):
        self.cmd("center", center)
        self.cmd("span", span)
        self.cmd("rbw", rbw)
        self.cmd("vbw", vbw)
        self.wait()
        return self.askf("sweep_time?")

    def sweep(self, cancel=None):
        st = self.askf("sweep_time?")
        self.cmd("init")
        self.wait(timeout=3 * st + 30, cancel=cancel)
        return self.parse_trace(self.ask("trace?", timeout=30))

    def parse_trace(self, raw: str) -> np.ndarray:
        """ASCII comma-separated trace (override for binary/other formats)."""
        try:
            return np.array([float(v) for v in raw.strip().strip(",").split(",")])
        except ValueError as e:
            raise AnalyzerError(f"bad trace data: {raw[:80]!r}") from e

    def get_center(self):
        return self.askf("center?")

    def get_span(self):
        return self.askf("span?")

    def get_ref_level(self):
        return self.askf("ref_level?")

    def set_ref_level(self, dbm):
        self.cmd("ref_level", dbm)
        self.wait()

    def errors(self):
        out = []
        for _ in range(20):
            e = self.ask("error")
            if e.lstrip("+").startswith("0"):
                break
            out.append(e)
        return out
