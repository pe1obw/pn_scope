import json
import threading

import numpy as np
import pytest

from pnscope import analysis as A
from pnscope.analyzers.base import Cancelled
from pnscope.analyzers.simulated import SimulatedAnalyzer, default_profile
from pnscope.measure import MeasureSettings, PhaseNoiseRun, find_carrier
from pnscope.model import Measurement, load
from pnscope.plan import PlanSettings, make_plan


def test_plan_reproduces_example_script_segments():
    a = SimulatedAnalyzer()
    plan, warn = make_plan(PlanSettings(start=30, stop=10e6), a.rbw_steps)
    assert [(p.span, p.rbw) for p in plan] == [
        (2e3, 3), (20e3, 30), (200e3, 300), (2e6, 3e3), (20e6, 30e3)]
    assert plan[0].f_lo == 30 and plan[-1].f_hi == 10e6 and not warn


def test_plan_warns_when_rbw_limit_reached():
    plan, warn = make_plan(PlanSettings(start=3, stop=1e3), [1, 3, 10])
    assert plan[0].rbw == 1 and warn


def test_simulated_measurement_recovers_profile(tmp_path):
    a = SimulatedAnalyzer(sweep_delay=0, seed=1)
    a.connect()
    ms = MeasureSettings(name="sim", carrier_freq=a.fc0 + 3e3)
    got = []
    run = PhaseNoiseRun(a, PlanSettings(start=100, stop=1e6, averages=4), ms,
                        on_segment=lambda m: got.append(len(m.segments)))
    m = run.run()
    assert abs(m.carrier_freq - a.fc0) < 1.0
    assert m.carrier_power == pytest.approx(a.pc, abs=0.3)
    assert got == list(range(1, len(run.plan) + 1))
    c = A.stitch(m)
    p = A.process(c, A.ProcessOptions(remove_spurs=True, smoothing=0.1))
    for off in (300, 3e3, 30e3, 300e3):
        assert A.value_at(p.f, p.display, off) == pytest.approx(float(default_profile(off)), abs=1.5)
    assert any(abs(s.offset - 7.3e3) < 50 and abs(s.dbc + 62) < 1.5 for s in p.spurs)
    # analyzer restored
    assert a.span == 7.5e9
    # round trip
    m.save(tmp_path / "x.pnscope")
    m2 = load(tmp_path / "x.pnscope")
    assert np.allclose(A.stitch(m2).avg, c.avg, atol=1e-3)


def test_cancel_restores_state():
    a = SimulatedAnalyzer(sweep_delay=0.01)
    ev = threading.Event()
    run = PhaseNoiseRun(a, PlanSettings(), MeasureSettings(carrier_freq=a.fc0), cancel=ev,
                        on_segment=lambda m: ev.set())
    with pytest.raises(Cancelled):
        run.run()
    assert a.span == 7.5e9


def test_find_carrier_full_span():
    a = SimulatedAnalyzer(sweep_delay=0, seed=2)
    fc, lvl = find_carrier(a)
    assert abs(fc - a.fc0) < 2 and lvl == pytest.approx(a.pc, abs=1.5)


def test_no_carrier_is_an_error():
    from pnscope.analyzers.base import AnalyzerError
    a = SimulatedAnalyzer(sweep_delay=0, seed=3)
    run = PhaseNoiseRun(a, PlanSettings(), MeasureSettings(carrier_freq=2e9))
    with pytest.raises(AnalyzerError, match="No carrier"):
        run.run()
    assert a.span == 7.5e9
