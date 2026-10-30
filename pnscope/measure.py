"""The measurement procedure: find the carrier, then sweep each segment."""
from __future__ import annotations

import datetime
import threading
from dataclasses import dataclass, asdict
from typing import Callable

import numpy as np

from .analyzers.base import AnalyzerError, Cancelled, SpectrumAnalyzer
from .model import Measurement, Segment
from .plan import PlanSettings, SegmentPlan, make_plan


@dataclass
class MeasureSettings:
    name: str = "measurement"
    carrier_freq: float | None = None  # None: use the analyzer's centre frequency
    search_span: float = 1e6           # look for the carrier within ± search_span/2
    track_carrier: bool = True         # re-centre on the carrier before every segment
    auto_ref_level: bool = True        # reference level = carrier power + ref_margin
    ref_margin: float = 5.0
    restore: bool = True               # put the analyzer back as it was afterwards
    notes: str = ""

    def to_dict(self):
        return asdict(self)


ProgressFn = Callable[[str, float], None]


def _peak(f, t, exclude_below=None):
    """Peak frequency/level with parabolic interpolation over the top three bins."""
    t = np.asarray(t, float)
    if exclude_below is not None:
        t = np.where(f < exclude_below, -np.inf, t)
    i = int(np.argmax(t))
    fi = f[i]
    if 0 < i < len(t) - 1 and np.isfinite(t[i - 1]) and np.isfinite(t[i + 1]):
        a, b, c = t[i - 1], t[i], t[i + 1]
        d = a - 2 * b + c
        if d < 0:
            p = 0.5 * (a - c) / d
            if abs(p) <= 1:
                fi = f[i] + p * (f[1] - f[0])
    return fi, t[i]


class PhaseNoiseRun:
    """Runs one measurement on an already connected analyzer. Call from a worker thread;
    progress/segment callbacks are invoked from that thread."""

    def __init__(self, analyzer: SpectrumAnalyzer, plan_settings: PlanSettings,
                 settings: MeasureSettings, progress: ProgressFn | None = None,
                 on_segment: Callable[[Measurement], None] | None = None,
                 cancel: threading.Event | None = None):
        self.a = analyzer
        self.ps = plan_settings
        self.ms = settings
        self.progress = progress or (lambda msg, frac: None)
        self.on_segment = on_segment or (lambda m: None)
        self.cancel = cancel or threading.Event()
        self.plan, self.warnings = make_plan(plan_settings, analyzer.rbw_steps)

    def _check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def _sweep_avg(self, n) -> np.ndarray:
        acc = None
        for _ in range(n):
            self._check()
            t = 10 ** (self.a.sweep(self.cancel) / 10)
            acc = t if acc is None else acc + t
        return 10 * np.log10(acc / n)

    def _grid(self, center, span, n):
        return center - span / 2 + span * np.arange(n) / (n - 1)

    MIN_SNR_DB = 25.0

    def locate(self, fc, steps) -> tuple[float, float]:
        """Zoom in on the strongest signal: steps = [(span, rbw), ...]. Raises if the
        strongest signal does not stand out clearly from the noise."""
        level = None
        for i, (span, rbw) in enumerate(steps):
            self._check()
            self.a.configure(fc, span, rbw, 3 * rbw)
            t = self._sweep_avg(1)
            f = self._grid(fc, span, len(t))
            fc_new, level = _peak(f, t, exclude_below=max(self.a.min_freq, 5 * rbw))
            snr = level - float(np.median(t))
            if i == 0 and snr < self.MIN_SNR_DB:
                raise AnalyzerError(
                    f"No carrier found within ±{span / 2e3:g} kHz of {fc / 1e6:.6f} MHz: the strongest "
                    f"signal ({level:.1f} dBm) is only {snr:.0f} dB above the noise. Check the carrier "
                    f"frequency, or use 'Find'.")
            fc = fc_new
        return fc, level

    def locate_steps(self, search_span):
        steps, span = [], search_span
        while span > 2e3 * 1.01:
            steps.append((span, self._rbw_for(span)))
            span = max(2e3, span / 20)
        steps.append((2e3, self._rbw_for(2e3)))
        return steps

    def _rbw_for(self, span):
        ok = [r for r in self.a.rbw_steps if r <= span / 60]
        return ok[-1] if ok else self.a.rbw_steps[0]

    def measure_carrier_power(self, fc) -> float:
        """Wide RBW (many bins wide) so the RMS detector reads the CW level accurately."""
        span = 20e3
        rbw = max([r for r in self.a.rbw_steps if r <= span / 20] or self.a.rbw_steps[:1])
        self.a.configure(fc, span, rbw, 3 * rbw)
        t = self._sweep_avg(2)
        return _peak(self._grid(fc, span, len(t)), t)[1]

    def run(self) -> Measurement:
        a, ms = self.a, self.ms
        idn = a.identify()
        state = a.save_state() if ms.restore else None
        n_seg = len(self.plan)
        try:
            a.setup_noise_mode()
            fc = ms.carrier_freq if ms.carrier_freq else a.get_center()
            self.progress(f"Searching carrier near {fc / 1e6:.6f} MHz", 0.0)
            fc, _ = self.locate(fc, self.locate_steps(ms.search_span))
            pc = self.measure_carrier_power(fc)
            if ms.auto_ref_level:
                ref = float(np.ceil(pc + ms.ref_margin))
                self.progress(f"Reference level {ref:.0f} dBm", 0.02)
                a.set_ref_level(ref)
                pc = self.measure_carrier_power(fc)
            elif pc > a.get_ref_level():
                self.warnings.append("Carrier is above the reference level: risk of overload.")
            self.progress(f"Carrier {fc:.1f} Hz, {pc:.2f} dBm", 0.04)

            m = Measurement(name=ms.name, carrier_freq=fc, carrier_power=pc,
                            timestamp=datetime.datetime.now().isoformat(timespec="seconds"),
                            instrument=idn, notes=ms.notes,
                            settings={"plan": self.ps.to_dict(), "measure": ms.to_dict(),
                                      "warnings": list(self.warnings)})
            # rough cost of each segment for the progress bar
            weights = []
            for sp in self.plan:
                weights.append(sp.averages * (0.5 + sp.span / sp.rbw ** 2 / 200))
            total, done = sum(weights), 0.0
            for k, sp in enumerate(self.plan):
                self._check()
                if ms.track_carrier and k > 0:
                    fc, _ = self.locate(fc, [(20e3, self._rbw_for(20e3)), (2e3, self._rbw_for(2e3))])
                st = a.configure(fc, sp.span, sp.rbw, sp.vbw)
                self.progress(f"Segment {k + 1}/{n_seg}: span {sp.span:g} Hz, RBW {sp.rbw:g} Hz, "
                              f"{sp.averages}× {st:.2f} s", 0.05 + 0.95 * done / total)
                t = self._sweep_avg(sp.averages)
                m.segments.append(Segment(
                    span=sp.span, rbw=sp.rbw, vbw=sp.vbw, averages=sp.averages,
                    f_lo=sp.f_lo, f_hi=sp.f_hi,
                    offsets=-sp.span / 2 + sp.span * np.arange(len(t)) / (len(t) - 1),
                    power=t, carrier_freq=fc, sweep_time=st, enbw=a.enbw_ratio(sp.rbw)))
                done += weights[k]
                self.on_segment(m)
            errs = a.errors()
            if errs:
                m.settings["analyzer_errors"] = errs
            self.progress("Done", 1.0)
            return m
        finally:
            if state is not None:
                try:
                    a.restore_state(state)
                except Exception:  # noqa: BLE001 - never mask the original error
                    pass


def find_carrier(analyzer: SpectrumAnalyzer, cancel=None, progress=None) -> tuple[float, float]:
    """Full-span search for the strongest signal (ignores the 0 Hz LO feed-through)."""
    run = PhaseNoiseRun(analyzer, PlanSettings(), MeasureSettings(), progress=progress, cancel=cancel)
    state = analyzer.save_state()
    try:
        analyzer.setup_noise_mode()
        lo, hi = analyzer.min_freq, analyzer.max_freq
        span = hi - lo
        center = (lo + hi) / 2
        rbw = run._rbw_for(span / 10)
        analyzer.configure(center, span, rbw, 3 * rbw)
        t = run._sweep_avg(1)
        f = run._grid(center, span, len(t))
        fc, level = _peak(f, t, exclude_below=max(lo, 2e6, 10 * rbw))
        if level - float(np.median(t)) < run.MIN_SNR_DB:
            raise AnalyzerError(f"No signal found on the analyzer input: the strongest one "
                                f"({level:.1f} dBm at {fc / 1e6:.3f} MHz) is only "
                                f"{level - float(np.median(t)):.0f} dB above the noise.")
        return run.locate(fc, run.locate_steps(min(span / 50, 200e6)))
    finally:
        analyzer.restore_state(state)
