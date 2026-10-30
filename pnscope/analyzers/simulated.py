"""A software analyzer that synthesises a carrier with phase noise and spurs.
Lets you try the program without hardware, and is used by the tests."""
from __future__ import annotations

import threading
import time

import numpy as np

from .base import Cancelled, SpectrumAnalyzer, steps_125
from .registry import register

ENBW = 1.065


def default_profile(f):
    """SSB L(f) [dBc/Hz] of the simulated source: close-in 1/f^3 + PLL hump at 50 kHz
    + white floor."""
    f = np.hypot(f, 60.0)  # flatten inside ~60 Hz so the noise power near the carrier stays finite
    lin = (10 ** (-62 / 10) * (100 / f) ** 3        # flicker FM
           + 10 ** (-95 / 10) * (1e3 / f) ** 0.5    # reference
           + 10 ** (-88 / 10) / (1 + ((f - 50e3) / 25e3) ** 2)  # PLL peaking
           + 10 ** (-140 / 10))
    return 10 * np.log10(lin)


@register
class SimulatedAnalyzer(SpectrumAnalyzer):
    model_name = "Simulated analyzer"
    rbw_steps = steps_125(1, 3e6)
    min_freq = 9e3
    max_freq = 7.5e9

    def __init__(self, carrier_freq=1_000_000_123.4, carrier_power=-8.0, points=751,
                 sweep_delay=0.02, floor_dbm_hz=-155.0, drift_hz_per_s=0.0,
                 spurs=((50.0, -55.0), (7.3e3, -62.0), (370e3, -55.0)), seed=None,
                 profile=default_profile):
        self.fc0, self.pc = carrier_freq, carrier_power
        self.points, self.delay = points, sweep_delay
        self.floor = floor_dbm_hz
        self.drift = drift_hz_per_s
        self.spurs = list(spurs)  # (offset Hz, dBc), symmetric pair
        self.profile = profile
        self.rng = np.random.default_rng(seed)
        self.center, self.span, self.rbw, self.vbw = carrier_freq, 7.5e9, 3e6, 3e6
        self.ref = 0.0
        self.t0 = time.monotonic()
        self.connected = False

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    def identify(self):
        return "PNScope,Simulated analyzer,0,1.0"

    def save_state(self):
        return dict(center=self.center, span=self.span, rbw=self.rbw, vbw=self.vbw, ref=self.ref)

    def restore_state(self, s):
        self.center, self.span, self.rbw, self.vbw, self.ref = (
            s["center"], s["span"], s["rbw"], s["vbw"], s["ref"])

    def setup_noise_mode(self):
        pass

    def configure(self, center, span, rbw, vbw):
        self.center, self.span, self.rbw, self.vbw = center, span, rbw, vbw
        return self.sweep_time()

    def sweep_time(self):
        return max(5e-3, 0.08 * self.span / self.rbw ** 2) if self.span < 1e9 else 0.1

    def _fc(self):
        return self.fc0 + self.drift * (time.monotonic() - self.t0)

    def sweep(self, cancel: threading.Event | None = None):
        if self.delay:
            t_end = time.monotonic() + self.delay
            while time.monotonic() < t_end:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                time.sleep(min(0.01, self.delay))
        n = self.points
        f = self.center - self.span / 2 + self.span * np.arange(n) / (n - 1)
        df = f - self._fc()
        bin_w = self.span / (n - 1)
        # RBW filter (Gaussian). Integrate over the bin when bins are wider than the RBW.
        sub = int(min(max(1, np.ceil(2 * bin_w / self.rbw)), 25))
        u = (np.arange(sub) - (sub - 1) / 2) / sub * bin_w
        sigma = self.rbw / 2.3548
        pc_lin = 10 ** (self.pc / 10)
        p = np.zeros(n)
        for du in u:
            d = df[:, None] + du
            p += pc_lin * np.exp(-0.5 * (d[:, 0] / sigma) ** 2)
            for off, dbc in self.spurs:
                for sgn in (1, -1):
                    p += pc_lin * 10 ** (dbc / 10) * np.exp(-0.5 * ((d[:, 0] - sgn * off) / sigma) ** 2)
        p /= sub
        nbw = ENBW * self.rbw
        noise = pc_lin * 10 ** (self.profile(df) / 10) * nbw + 10 ** (self.floor / 10) * nbw
        # RMS detector: average of ~k independent noise samples per bin (gamma distributed)
        k = max(2.0, bin_w / self.rbw * 2)
        noise = noise * self.rng.gamma(k, 1 / k, n)
        if self.span > 1e9:  # LO feed-through at 0 Hz in wide spans
            p += 10 ** (-20 / 10) * np.exp(-0.5 * (f / (self.rbw * 2)) ** 2)
        return 10 * np.log10(p + noise)

    def get_center(self):
        return self.center

    def get_span(self):
        return self.span

    def get_ref_level(self):
        return self.ref

    def set_ref_level(self, dbm):
        self.ref = dbm
