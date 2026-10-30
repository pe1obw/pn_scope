"""tinySA Ultra (ZS-406 / ZS-407) over its USB serial shell.

Not SCPI: commands are plain text lines ending in CR, answered with an echo, the
response and the prompt 'ch> '. Sweeps use 'scanraw', which returns binary points:
b'{' + n * (b'x' + int16 little endian) + b'}', dBm = value/32 - 174.

Tested: ZS-406, firmware tinySA4_v1.4-109. Its minimum RBW is 200 Hz, so offsets below
~2 kHz are not usable, and its own LO phase noise is much higher than that of a bench
analyzer: measure a clean source as reference trace to see where its floor is."""
from __future__ import annotations

import struct
import threading
import time

import numpy as np

from .base import AnalyzerError, Cancelled, SpectrumAnalyzer
from .registry import register

PROMPT = b"ch> "
USB_VID_PID = (0x0483, 0x5740)  # STM32 virtual COM port as used by the tinySA


def find_port() -> str:
    from serial.tools import list_ports
    ports = [p for p in list_ports.comports() if (p.vid, p.pid) == USB_VID_PID
             or "tinysa" in (p.description or "").lower() or "tinysa" in (p.manufacturer or "").lower()]
    if not ports:
        raise AnalyzerError("no tinySA found on USB; enter the serial port (e.g. /dev/ttyACM0 or COM5)")
    return ports[0].device


class SerialShell:
    def __init__(self, port: str, timeout: float = 5.0):
        self.port, self.timeout = port, timeout
        self.ser = None

    def open(self):
        import serial
        port = find_port() if self.port.strip().lower() in ("", "auto") else self.port.strip()
        try:
            self.ser = serial.Serial(port, 115200, timeout=0.2)
        except serial.SerialException as e:
            raise AnalyzerError(f"cannot open {port}: {e}") from e
        self.ser.reset_input_buffer()
        self.command("")  # sync to the prompt

    def close(self):
        if self.ser:
            self.ser.close()
            self.ser = None

    def raw(self, cmd: str, timeout: float | None = None, cancel: threading.Event | None = None) -> bytes:
        """Send a command, return everything up to the next prompt (echo stripped)."""
        if not self.ser:
            raise AnalyzerError("not connected")
        self.ser.write((cmd + "\r").encode())
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        buf = b""
        cancelled = False
        while not buf.endswith(PROMPT):
            if cancel is not None and cancel.is_set():
                cancelled = True  # keep reading: the scan finishes anyway
            if time.monotonic() > deadline:
                raise AnalyzerError(f"timeout waiting for tinySA ({cmd!r})")
            buf += self.ser.read(self.ser.in_waiting or 1)
        if cancelled:
            raise Cancelled()
        body = buf[:-len(PROMPT)]
        echo = (cmd + "\r\n").encode()
        return body[len(echo):] if body.startswith(echo) else body

    def command(self, cmd: str, timeout=None) -> list[str]:
        text = self.raw(cmd, timeout).decode(errors="replace")
        return [line for line in text.replace("\r", "").split("\n") if line.strip()]


@register
class TinySAUltra(SpectrumAnalyzer):
    model_name = "tinySA Ultra"
    needs_host = True
    address_label = "Serial port"
    default_address = "auto"
    rbw_steps = [200.0, 1e3, 3e3, 10e3, 30e3, 100e3, 300e3, 600e3, 850e3]
    min_freq = 100e3
    max_freq = 5.3e9
    # Noise bandwidth / RBW, measured on a ZS-406 by sweeping a clean CW carrier through
    # each filter. The detector combines ~5-20 readings per point; peak detection is ruled
    # out (noise density agrees within 0.7 dB across RBWs), but averaging in dB (noise reads
    # ~2 dB low) could not be distinguished from power averaging (no error) without an
    # absolute noise reference.
    ENBW = {200.0: 1.223, 1e3: 1.135, 3e3: 1.049, 10e3: 1.072, 30e3: 1.081}
    enbw_factor = 1.08  # RBWs not measured
    max_points = 1500
    ms_per_point = {200.0: 21, 1e3: 5, 3e3: 5}  # measured on a ZS-406; others ~3 ms

    def enbw_ratio(self, rbw):
        return self.ENBW.get(rbw, self.enbw_factor)

    def __init__(self, port: str = "auto"):
        self.sh = SerialShell(port)
        self.center = self.span = self.rbw = 0.0
        self.points = 101

    def connect(self):
        self.sh.open()

    def close(self):
        self.sh.close()

    def identify(self):
        info = self.sh.command("info")
        version = self.sh.command("version")
        model = next((line for line in info if "tinySA" in line), "tinySA")
        return f"{model}, {version[0] if version else ''}, port {self.sh.ser.port}"

    def save_state(self):
        start, stop, points = self.sh.command("sweep")[0].split()[:3]
        return {"sweep": f"{start} {stop} {points}"}

    def restore_state(self, state):
        # the shell only reports the current RBW, not whether it was 'auto'; auto is the default
        self.sh.command("rbw auto")
        self.sh.command(f"sweep {state['sweep']}")
        self.sh.command("resume")

    def setup_noise_mode(self):
        self.sh.command("pause")  # keep the screen sweep from interleaving with our scans

    def configure(self, center, span, rbw, vbw):
        self.center, self.span, self.rbw = center, span, rbw
        # about one point per RBW (as with the 751-point Siglent at span/RBW = 600)
        self.points = int(min(max(round(1.25 * span / rbw), 101), self.max_points))
        self.sh.command(f"rbw {rbw / 1e3:g}")
        return self.points * self.ms_per_point.get(rbw, 3) / 1e3

    def sweep(self, cancel=None):
        start = self.center - self.span / 2
        stop = self.center + self.span / 2
        if start < self.min_freq:
            raise AnalyzerError(f"sweep starts below {self.min_freq / 1e3:g} kHz")
        est = self.points * self.ms_per_point.get(self.rbw, 3) / 1e3
        raw = self.sh.raw(f"scanraw {round(start)} {round(stop)} {self.points}", timeout=5 * est + 10,
                          cancel=cancel)
        try:
            body = raw[raw.index(b"{") + 1:raw.rindex(b"}")]
        except ValueError:
            raise AnalyzerError(f"unexpected scanraw reply: {raw[:60]!r}") from None
        if len(body) != 3 * self.points:
            raise AnalyzerError(f"scanraw returned {len(body) // 3} of {self.points} points")
        vals = struct.unpack("<" + "xh" * self.points, body)
        return np.asarray(vals, float) / 32.0 - 174.0

    def get_center(self):
        start, stop = (float(x) for x in self.sh.command("sweep")[0].split()[:2])
        return (start + stop) / 2

    def get_span(self):
        start, stop = (float(x) for x in self.sh.command("sweep")[0].split()[:2])
        return stop - start

    # the tinySA has no reference level that affects the measurement (attenuation is automatic)
    def get_ref_level(self):
        return 30.0

    def set_ref_level(self, dbm):
        pass
