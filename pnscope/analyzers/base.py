"""Spectrum analyzer interface used by the measurement engine."""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod

import numpy as np


class Cancelled(Exception):
    pass


class AnalyzerError(Exception):
    pass


def steps_125(lo, hi, mantissas=(1, 3)):
    """1, 3, 10, 30, ... between lo and hi."""
    out, dec = [], 1e-3
    while dec <= hi:
        for m in mantissas:
            v = m * dec
            if lo <= v * (1 + 1e-9) and v <= hi * (1 + 1e-9):
                out.append(round(v, 6))
        dec *= 10
    return out


class SpectrumAnalyzer(ABC):
    """Minimal swept-analyzer abstraction. All frequencies in Hz, powers in dBm.

    The phase-noise method needs: set center/span/RBW/VBW, a single sweep with an
    RMS (power-average) detector, and the trace as an array."""

    model_name = "generic"
    needs_host = False  # True: constructor takes an address (host name/IP, serial port, ...)
    address_label = "Address"
    default_address = ""
    # noise bandwidth of the RBW filter relative to the RBW setting (Gaussian: 1.065)
    enbw_factor = 1.065

    def enbw_ratio(self, rbw: float) -> float:
        """Noise bandwidth / RBW for this RBW setting (override if it depends on the RBW)."""
        return self.enbw_factor
    rbw_steps: list[float] = steps_125(1, 1e6)
    min_freq = 0.0
    max_freq = 1e12

    @abstractmethod
    def connect(self): ...

    @abstractmethod
    def close(self): ...

    @abstractmethod
    def identify(self) -> str: ...

    @abstractmethod
    def save_state(self) -> dict: ...

    @abstractmethod
    def restore_state(self, state: dict): ...

    @abstractmethod
    def setup_noise_mode(self):
        """Single sweep, clear/write trace, average (RMS) detector, power averaging."""

    @abstractmethod
    def configure(self, center, span, rbw, vbw) -> float:
        """Apply settings, wait until applied, return the sweep time of one sweep [s]."""

    @abstractmethod
    def sweep(self, cancel: threading.Event | None = None) -> np.ndarray:
        """Trigger one sweep, wait for it to finish, return the trace in dBm."""

    @abstractmethod
    def get_center(self) -> float: ...

    @abstractmethod
    def get_span(self) -> float: ...

    @abstractmethod
    def get_ref_level(self) -> float: ...

    @abstractmethod
    def set_ref_level(self, dbm: float): ...

    def errors(self) -> list[str]:
        return []

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
