import math
from pathlib import Path

import numpy as np
import pytest

from pnscope import analysis as A
from pnscope.model import Measurement, Segment, load

EX = Path(__file__).resolve().parent.parent / "example"


def flat_measurement(level_dbc_hz=-100.0, fc=1e9, pc=0.0):
    """Synthetic segments whose stitched L(f) is exactly `level_dbc_hz`."""
    segs, lo = [], 10.0
    for hi, rbw in [(1e3, 1.0), (10e3, 10.0), (100e3, 100.0), (1e6, 1e3)]:
        off = np.linspace(-hi, hi, 2001)
        p = np.full(off.shape, pc + level_dbc_hz + 10 * math.log10(A.ENBW_GAUSSIAN * rbw))
        segs.append(Segment(2 * hi, rbw, 3 * rbw, 1, lo, hi, off, p, fc))
        lo = hi
    return Measurement("flat", fc, pc, segs)


def test_flat_integration_matches_closed_form():
    m = flat_measurement(-100.0)
    c = A.stitch(m)
    assert np.allclose(c.usb, -100) and np.allclose(c.lsb, -100) and np.allclose(c.avg, -100)
    r = A.integrate(c.f, c.avg, 100, 1e6, m.carrier_freq)
    expect = math.sqrt(2 * 1e-10 * (1e6 - 100))
    assert r.phase_rad == pytest.approx(expect, rel=1e-3)
    assert r.dbc == pytest.approx(10 * math.log10(2 * 1e-10 * (1e6 - 100)), abs=0.01)
    assert r.jitter_s == pytest.approx(expect / (2 * math.pi * 1e9), rel=1e-3)
    # residual FM: sqrt(2 * L * (f2^3 - f1^3) / 3)
    assert r.fm_hz == pytest.approx(math.sqrt(2 * 1e-10 * (1e18 - 1e6) / 3), rel=1e-2)


def test_spur_detection_and_removal():
    m = flat_measurement(-100.0)
    seg = m.segments[2]
    i = np.argmin(abs(seg.offsets - 37e3))
    seg.power[i] += 30  # 30 dB spur, single bin
    c = A.stitch(m)
    spurs, mask, base = A.find_spurs(c.f, c.usb, c.rbw)
    assert len(spurs) == 1 and spurs[0].offset == pytest.approx(37e3, rel=1e-3)
    # spur level in dBc = L + 10log(ENBW*RBW) = -100 + 30 + 20.27
    assert spurs[0].dbc == pytest.approx(-70 + 10 * math.log10(A.ENBW_GAUSSIAN * 100), abs=0.01)
    p = A.process(c, A.ProcessOptions(sideband="Upper", remove_spurs=True))
    assert np.allclose(p.clean, -100)


def test_smoothing_preserves_flat_and_handles_nan():
    f = np.logspace(1, 6, 500)
    L = np.full_like(f, -90.0)
    L[100] = np.nan
    s = A.smooth(f, L, 0.1)
    assert np.isnan(s[100]) and np.allclose(np.delete(s, 100), -90)


def test_floor_subtraction():
    f = np.logspace(2, 6, 100)
    dut = np.full_like(f, -90.0)
    floor = np.full_like(f, -93.0)
    out = A.subtract_floor(f, dut, f, floor)
    assert np.allclose(out, 10 * np.log10(10 ** -9 - 10 ** -9.3))
    out = A.subtract_floor(f, floor + 0.5, f, floor)
    assert np.all(np.isnan(out))


def test_spot_and_value_at():
    f = np.logspace(1, 6, 501)
    L = -20 * np.log10(f)
    assert A.value_at(f, L, 1e3) == pytest.approx(-60, abs=1e-6)
    assert A.spot(f, L, 1e3) == pytest.approx(-60, abs=0.2)
    assert math.isnan(A.spot(f, L, 1e8))


@pytest.mark.skipif(not (EX / "run1_meta.json").exists(), reason="example data not present")
def test_reproduces_legacy_process_script():
    """example/README.md: run1 100 Hz-10 MHz = 1.30 deg raw, 1.03 deg minus analyzer floor."""
    run1, floor = load(EX / "run1_meta.json"), load(EX / "siggen_meta.json")
    c, fc = A.stitch(run1), A.stitch(floor)
    r = A.integrate(c.f, c.avg, 100, 10e6, run1.carrier_freq)
    assert r.phase_deg == pytest.approx(1.30, abs=0.006)
    p = A.process(c, A.ProcessOptions(subtract_floor=True), fc)
    r2 = A.integrate(p.f, p.clean, 100, 10e6, run1.carrier_freq)
    assert r2.phase_deg == pytest.approx(1.03, abs=0.006)
