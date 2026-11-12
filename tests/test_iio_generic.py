"""Generic IIO driver signal processing, without hardware: a synthetic CW plus white noise
(raw 16-bit I/Q, carrier away from the LO) must read the right CW power and noise density
after mixing, decimation and the Welch spectrum."""
import math

import numpy as np
import pytest

from pnscope.analyzers.base import AnalyzerError
from pnscope.analyzers.iio_generic import (GenericIIO, _stage_factors, kaiser_lowpass,
                                           parse_address, smooth_floor, smooth_near)

FS = 61.44e6
LO = 1575.42e6


def make(raw_fn, maxcap=1 << 23):
    sa = GenericIIO(f"ip:0.0.0.0 maxcap={maxcap}")
    sa.fs, sa.lo = FS, LO
    sa._ensure_buffer = lambda n: setattr(sa, "n_req", n)
    sa._capture = lambda: raw_fn(sa.n_req)
    return sa


def cw_noise(f_if, p_cw_dbfs, nd_dbfs_hz, seed=1):
    """Raw interleaved int16 of a CW at f_if plus complex white noise of the given density."""
    def raw(n):
        rng = np.random.default_rng(seed)
        t = np.arange(n)
        a = math.sqrt(10 ** (p_cw_dbfs / 10))
        sigma = math.sqrt(10 ** (nd_dbfs_hz / 10) * FS / 2)
        z = a * np.exp(2j * np.pi * f_if / FS * t)
        z = z + sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
        z *= 32768
        out = np.empty(2 * n, np.int16)
        out[0::2] = np.clip(np.round(z.real), -32768, 32767)
        out[1::2] = np.clip(np.round(z.imag), -32768, 32767)
        return out
    return raw


def test_parse_address():
    assert parse_address("192.168.2.1") == ("ip:192.168.2.1", {})
    uri, o = parse_address("ip:10.0.0.1  ch=3 fs=61.44M lo=<chip>-phy1/altvoltage0")
    assert uri == "ip:10.0.0.1" and o == {"ch": "3", "fs": "61.44M", "lo": "<chip>-phy1/altvoltage0"}
    with pytest.raises(AnalyzerError):
        parse_address("ip:1.2.3.4 foo=1")
    with pytest.raises(AnalyzerError):
        parse_address("ip:1.2.3.4 ch")


def test_smooth_and_stages():
    assert smooth_floor(1000) == 1000 and smooth_floor(1001) == 1000 and smooth_floor(7) == 6
    assert smooth_near(1023) == 1024 and smooth_near(1001) == 1000
    for d in (1, 8, 12288, 24576, 30720):
        st = _stage_factors(d)
        assert math.prod(st) == d and all(s <= 16 for s in st)


def test_lowpass_response():
    h = kaiser_lowpass(1.0, 0.1, 0.2).astype(np.float64)
    H = np.abs(np.fft.rfft(h, 1 << 16))
    f = np.arange(len(H)) / (1 << 16)
    assert np.max(np.abs(20 * np.log10(H[f <= 0.1]))) < 0.01
    assert 20 * np.log10(np.max(H[f >= 0.2])) < -95


@pytest.mark.parametrize("span,rbw", [(2e3, 10.0), (20e3, 30.0), (200e3, 300.0), (2e6, 3e3),
                                      (20e6, 30e3)])
def test_cw_and_noise_levels(span, rbw):
    f_if, p_cw, nd = 5.2e6 + 123.4, -10.0, -150.0
    sa = make(cw_noise(f_if, p_cw, nd))
    sa.configure(LO + f_if, span, rbw, 3 * rbw)
    tr = sa.sweep()
    n = len(tr)
    off = span * (np.arange(n) / (n - 1) - 0.5)
    # CW at the carrier offset (amplitude-normalised window: the peak reads the CW power)
    k = int(np.argmax(tr))
    assert abs(off[k]) <= span / (n - 1) + rbw
    assert tr[k] == pytest.approx(p_cw, abs=0.5)
    # noise density away from the carrier and the band edges; few independent bins when
    # the capture limits the FFT length (narrowest span), so a wider tolerance there
    mask = (np.abs(off) > 0.2 * span) & (np.abs(off) < 0.45 * span)
    nd_meas = 10 * np.log10(np.mean(10 ** (tr[mask] / 10)) / (rbw * sa.enbw_ratio(rbw)))
    assert nd_meas == pytest.approx(nd, abs=0.5 if sa.cfg["nfft"] >= 1000 else 1.5)
    assert not [w for w in sa.errors() if "clipping" in w]


def test_no_alias_of_strong_offband_signal():
    """A strong second tone outside the decimated band must not fold into the span."""
    f_if = 5e6
    base = cw_noise(f_if, -20.0, -170.0)

    def raw(n):
        r = base(n).astype(np.float64)
        t = np.arange(n)
        f2 = f_if + 7.3e3      # outside the ±1 kHz span and the 2.5 kHz decimated band
        z = 0.3 * np.exp(2j * np.pi * f2 / FS * t) * 32768
        r[0::2] += z.real
        r[1::2] += z.imag
        return np.round(r).astype(np.int16)
    sa = make(raw)
    sa.configure(LO + f_if, 2e3, 10.0, 30.0)
    tr = sa.sweep()
    n = len(tr)
    off = 2e3 * (np.arange(n) / (n - 1) - 0.5)
    away = np.abs(off) > 100
    assert np.max(tr[away]) < -20 - 80


def test_clipping_and_band_checks():
    sa = make(cw_noise(5e6, 0.5, -150.0))
    sa.configure(LO + 5e6, 200e3, 300.0, 900.0)
    sa.sweep()
    assert any("clipping" in w for w in sa.errors())
    with pytest.raises(AnalyzerError):
        sa.configure(LO + 40e6, 200e3, 300.0, 900.0)


def test_rbw_limited_by_capture():
    sa = make(cw_noise(5e6, -10.0, -150.0), maxcap=1 << 22)
    sa.configure(LO + 5e6, 2e3, 1.0, 3.0)
    assert any("RBW 1 Hz" in w for w in sa.errors())
    assert sa.enbw_ratio(1.0) > 1.5  # actual noise bandwidth is wider than 1 Hz
    assert sa.cfg["n_in"] <= 1 << 22
    with pytest.raises(AnalyzerError):
        make(cw_noise(5e6, -10.0, -150.0), maxcap=1 << 20).configure(LO + 5e6, 2e3, 1.0, 3.0)


def test_captures_stay_contiguous():
    """Each capture fits one 16 MiB DMA transfer; narrow RBWs average several captures."""
    sa = make(cw_noise(5e6, -10.0, -150.0), maxcap=1 << 25)
    assert sa.capture_limit() == (16 << 20) // 4
    for span, rbw in ((2e3, 30.0), (20e3, 30.0), (200e3, 300.0), (2e6, 3e3)):
        c = sa.plan(LO + 5e6, span, rbw)
        assert c["n_in"] <= sa.capture_limit()
        assert c["segs"] * c["n_caps"] >= 3 or c["n_caps"] * c["n_in"] * 2 > sa.max_capture
    c = sa.plan(LO + 5e6, 20e3, 30.0)
    assert c["n_caps"] > 1
    calls = []
    sa._capture = lambda: calls.append(1) or cw_noise(5e6, -10.0, -150.0)(sa.n_req)
    sa.configure(LO + 5e6, 20e3, 30.0, 90.0)
    sa.sweep()
    assert len(calls) == sa.cfg["n_caps"]
    big = GenericIIO("ip:0.0.0.0 contig=0 maxcap=1e7")
    assert big.capture_limit() == 10_000_000
