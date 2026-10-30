"""Split an offset range into analyzer segments (one span/RBW per decade)."""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict


@dataclass
class SegmentPlan:
    f_lo: float     # offset range this segment contributes
    f_hi: float
    span: float     # centred on the carrier, so both sidebands are measured
    rbw: float
    vbw: float
    averages: int

    def to_dict(self):
        return asdict(self)


@dataclass
class PlanSettings:
    start: float = 100.0         # Hz offset
    stop: float = 1e6
    averages: int = 8
    span_per_rbw: float = 600.0  # target span/RBW; ~ one RBW per trace bin at 751 points
    min_offset_rbw: float = 10.0  # lowest usable offset in a segment, in RBWs (carrier skirt)
    vbw_ratio: float = 3.0
    extra_close_in_averages: bool = True  # narrowest segments are noisier in log-f; average more

    def to_dict(self):
        return asdict(self)


def _floor_step(x, steps):
    ok = [s for s in steps if s <= x * (1 + 1e-9)]
    return ok[-1] if ok else steps[0]


def make_plan(ps: PlanSettings, rbw_steps) -> tuple[list[SegmentPlan], list[str]]:
    """Returns (segments, warnings). Decade boundaries at powers of ten; a partial first
    decade is merged into the next, a partial last decade shorter than half a decade into
    the previous one."""
    warnings = []
    start, stop = float(ps.start), float(ps.stop)
    if not (start > 0 and stop > start * 1.5):
        raise ValueError("stop offset must be larger than start offset")
    edges = [start]
    k = math.floor(math.log10(start)) + 1
    while 10 ** k < stop * (1 - 1e-9):
        edges.append(10.0 ** k)
        k += 1
    edges.append(stop)
    # a partial first decade joins the next one (its RBW then suits the lower edge);
    # a short partial last decade joins the previous one
    if len(edges) > 2 and edges[1] / edges[0] < 10 * (1 - 1e-9):
        del edges[1]
    if len(edges) > 2 and edges[-1] / edges[-2] < 10 ** 0.5:
        del edges[-2]

    segs = []
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
        span = 2 * hi
        rbw = _floor_step(min(span / ps.span_per_rbw, lo / ps.min_offset_rbw), rbw_steps)
        if lo / rbw < ps.min_offset_rbw * (1 - 1e-9):
            warnings.append(f"{lo:g} Hz offset is only {lo / rbw:.1f}×RBW ({rbw:g} Hz, the "
                            f"analyzer's minimum): close-in values may contain carrier skirt.")
        nav = ps.averages
        if ps.extra_close_in_averages and len(edges) > 2:
            # the first decade has few points per log-unit at the low end; give it more
            nav = int(round(ps.averages * (1.25 if i == 0 else 1.0)))
        segs.append(SegmentPlan(lo, hi, span, rbw, ps.vbw_ratio * rbw, max(1, nav)))
    return segs, warnings
