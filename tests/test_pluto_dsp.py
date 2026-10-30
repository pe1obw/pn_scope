"""Pluto driver signal processing, without hardware: a synthetic CW plus white noise must
read the right CW power and noise density through the FFT 'sweep' path."""
import math

import numpy as np
import pytest

from pnscope.analyzers.pluto import BW3_BINS, ENBW_BINS, PlutoSDR, _smooth_len


class FakeChan:
    def __init__(self):
        self.attrs = {"hardwaregain": type("A", (), {"value": "0.000000 dB"})()}


def make(fs, nfft, if_hz, span, rbw, x):
    sa = PlutoSDR("ip:0.0.0.0")
    sa.ch = FakeChan()
    sa.cfg = dict(mode="fft", center=0.0, span=span, rbw=rbw, fs=fs, nfft=nfft, ncap=len(x),
                  if_hz=if_hz, lo=0.0)
    sa._capture = lambda: x
    return sa


def test_smooth_len():
    assert _smooth_len(1000) == 1000 and _smooth_len(1001) == 1024 and _smooth_len(7) == 8


def test_window_bandwidths():
    assert BW3_BINS == pytest.approx(1.90, abs=0.01)
    assert ENBW_BINS == pytest.approx(2.004, abs=0.002)


def test_cw_and_noise_levels():
    rng = np.random.default_rng(1)
    rbw, span = 300.0, 200e3
    nfft = _smooth_len(BW3_BINS * 3 * span / rbw)
    fs = rbw * nfft / BW3_BINS
    n = 2 * nfft
    t = np.arange(n) / fs
    if_hz = fs / 4
    a_cw = 10 ** (-20 / 20)                       # -20 dB CW
    n0 = 10 ** (-120 / 10)                        # noise density per Hz (complex)
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * math.sqrt(n0 * fs / 2)
    x = a_cw * np.exp(2j * np.pi * (if_hz + 37.3) * t) + noise
    sa = make(fs, nfft, if_hz, span, rbw, x.astype(np.complex64))
    tr = sa.sweep()
    off = span * (np.arange(len(tr)) / (len(tr) - 1) - 0.5)
    # CW: grid cells are ~1 RBW wide here, so the averaging detector reads a bit low,
    # exactly like the Siglent's; the peak must be within 2.5 dB
    assert tr.max() == pytest.approx(-20, abs=2.5)
    # noise far from the carrier: N0 * ENBW (= enbw_ratio * RBW)
    far = np.abs(off) > 20 * rbw
    lvl = 10 * np.log10(np.mean(10 ** (tr[far] / 10)))
    assert lvl == pytest.approx(-120 + 10 * math.log10(sa.enbw_ratio(rbw) * rbw), abs=0.3)


def test_normalisation_tracks_gain_change():
    rng = np.random.default_rng(2)
    rbw, span = 3e3, 2e6
    nfft = _smooth_len(BW3_BINS * 3 * span / rbw)
    fs = rbw * nfft / BW3_BINS
    t = np.arange(2 * nfft) / fs

    def sig(amp):
        return (amp * np.exp(2j * np.pi * fs / 4 * t)
                + 1e-4 * (rng.standard_normal(len(t)) + 1j * rng.standard_normal(len(t)))).astype(np.complex64)

    sa = make(fs, nfft, fs / 4, span, rbw, sig(0.3))
    first = sa.sweep()
    sa._capture = lambda: sig(0.03)  # 20 dB less signal (e.g. other gain / filter)
    second = sa.sweep()
    assert second.max() == pytest.approx(first.max(), abs=0.1)
