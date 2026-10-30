"""Turn raw segments into L(f) and derive figures of merit.

L(f) = P(f) - Pc - 10*log10(ENBW * RBW)   [dBc/Hz]
with an RMS (power-average) detector, so no log-averaging correction applies.
Integration is over both sidebands (DSB): phi_rms = sqrt(2 * int L(f) df)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .model import Measurement

ENBW_GAUSSIAN = 1.065  # noise bandwidth / 3 dB bandwidth of a Gaussian RBW filter
SIDEBANDS = ("Average", "Upper", "Lower")
DEFAULT_SPOTS = [10, 100, 1e3, 10e3, 100e3, 1e6, 10e6]


@dataclass
class PNCurve:
    """L(f) for positive offsets. usb/lsb in dBc/Hz; rbw per point (for spur levels)."""
    f: np.ndarray
    usb: np.ndarray
    lsb: np.ndarray
    rbw: np.ndarray
    enbw: np.ndarray  # noise bandwidth / RBW per point

    @property
    def avg(self):
        return 10 * np.log10((10 ** (self.usb / 10) + 10 ** (self.lsb / 10)) / 2)

    def sideband(self, which: str) -> np.ndarray:
        return {"Average": self.avg, "Upper": self.usb, "Lower": self.lsb}[which]


def stitch(m: Measurement, enbw=None) -> PNCurve:
    """enbw: noise bandwidth / RBW. Default: per segment as stored by the driver,
    else Gaussian."""
    fs, us, ls, rs, es = [], [], [], [], []
    for seg in m.segments:
        off, p = seg.offsets, seg.power
        e = enbw or seg.enbw or ENBW_GAUSSIAN
        L = p - m.carrier_power - 10 * np.log10(e * seg.rbw)
        # half-open: the outermost bin of a trace is often unreliable
        sel = (off >= seg.f_lo) & (off < seg.f_hi)
        fu = off[sel]
        fs.append(fu)
        us.append(L[sel])
        ls.append(np.interp(-fu, off, L))
        rs.append(np.full(fu.shape, seg.rbw))
        es.append(np.full(fu.shape, e))
    if not fs:
        z = np.array([])
        return PNCurve(z, z, z, z, z)
    return PNCurve(*(np.concatenate(x) for x in (fs, us, ls, rs, es)))


# --- spurs ---------------------------------------------------------------------
@dataclass
class Spur:
    offset: float      # Hz
    dbc: float         # total spur power relative to carrier
    excess_db: float   # how far it sticks out above the noise
    background: bool | None = None  # also present with the DUT off? None: not checked


def _running_median(y, half):
    n = len(y)
    if n == 0:
        return y
    pad = np.pad(y, half, mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(pad, 2 * half + 1)
    return np.median(win, axis=1)[:n]


def find_spurs(f, L, rbw, threshold_db=10.0, half_window=15, enbw=ENBW_GAUSSIAN):
    """enbw: scalar or per-point array (noise bandwidth / RBW)."""
    """Points more than threshold above a running median. Returns (spurs, mask, baseline);
    mask covers each spur plus one neighbouring bin either side."""
    if len(f) == 0:
        return [], np.zeros(0, bool), L
    base = _running_median(L, half_window)
    hot = L > base + threshold_db
    spurs = []
    mask = np.zeros(len(L), bool)
    i = 0
    while i < len(L):
        if not hot[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(L) and hot[j + 1]:
            j += 1
        k = i + int(np.argmax(L[i:j + 1]))
        e = enbw[k] if np.ndim(enbw) else enbw
        spurs.append(Spur(float(f[k]), float(L[k] + 10 * np.log10(e * rbw[k])),
                          float(L[k] - base[k])))
        mask[max(i - 1, 0):j + 2] = True
        i = j + 1
    return spurs, mask, base


def classify_spurs(spurs, curve: "PNCurve", L_dut, background: "PNCurve", sideband: str,
                   margin_db=6.0):
    """Mark each spur as present without the carrier (background=True) when the background
    measurement reaches within margin_db of the spur's level at the same offset (searched
    over +-2 RBW, as the spur's exact bin can differ between the runs)."""
    if background is None or len(background.f) == 0:
        return spurs
    Lb = background.sideband(sideband)
    for sp in spurs:
        k = int(np.argmin(np.abs(curve.f - sp.offset)))
        tol = 2 * curve.rbw[k]
        sel = np.abs(background.f - sp.offset) <= tol
        if not sel.any():
            sp.background = None
            continue
        sp.background = bool(np.nanmax(Lb[sel]) >= L_dut[k] - margin_db)
    return spurs


# --- smoothing -------------------------------------------------------------------
def smooth(f, L, fraction_of_decade):
    """Moving average in the power domain over a window of `fraction_of_decade` (e.g. 0.05
    = 5 % of a decade, centred) on a log-frequency axis."""
    if fraction_of_decade <= 0 or len(f) < 3:
        return L
    lf = np.log10(f)
    lin = np.nan_to_num(10 ** (L / 10), nan=0.0)
    valid = np.isfinite(L).astype(float)
    cs = np.concatenate([[0], np.cumsum(lin)])
    cv = np.concatenate([[0], np.cumsum(valid)])
    h = fraction_of_decade / 2
    lo = np.searchsorted(lf, lf - h, "left")
    hi = np.searchsorted(lf, lf + h, "right")
    cnt = cv[hi] - cv[lo]
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 10 * np.log10((cs[hi] - cs[lo]) / cnt)
    out[~np.isfinite(L)] = np.nan
    return out


# --- reference (analyzer floor) subtraction ----------------------------------------
def subtract_floor(f, L, f_ref, L_ref, margin_db=1.0):
    """10log(10^(L/10) - 10^(R/10)) where L exceeds the reference by > margin, else NaN
    (indistinguishable from the floor)."""
    R = np.interp(np.log10(f), np.log10(f_ref), L_ref, left=np.nan, right=np.nan)
    ok = L > R + margin_db
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = 10 * np.log10(np.maximum(10 ** (L / 10) - 10 ** (R / 10), 1e-300))
    return np.where(ok, corr, np.nan)


# --- figures of merit ----------------------------------------------------------------
@dataclass
class Integrated:
    f1: float
    f2: float
    phase_rad: float       # rms
    dbc: float             # integrated phase noise (DSB) in dBc
    jitter_s: float
    fm_hz: float           # residual FM, rms

    @property
    def phase_deg(self):
        return math.degrees(self.phase_rad)


def integrate(f, L, f1, f2, carrier_freq) -> Integrated | None:
    """NaN points count as zero noise (e.g. below the floor after subtraction)."""
    if len(f) < 2 or f2 <= f1:
        return None
    f1, f2 = max(f1, f[0]), min(f2, f[-1])
    if f2 <= f1:
        return None
    sel = (f > f1) & (f < f2)
    lin = np.nan_to_num(10 ** (L / 10), nan=0.0)
    e1, e2 = np.interp([np.log10(f1), np.log10(f2)], np.log10(f), lin)
    ff = np.concatenate([[f1], f[sel], [f2]])
    yy = np.concatenate([[e1], lin[sel], [e2]])
    ssb = float(np.trapezoid(yy, ff))
    fm2 = float(np.trapezoid(yy * ff ** 2, ff))
    phi = math.sqrt(2 * ssb)
    return Integrated(f1, f2, phi, 10 * math.log10(2 * ssb) if ssb > 0 else -math.inf,
                      phi / (2 * math.pi * carrier_freq) if carrier_freq else math.nan,
                      math.sqrt(2 * fm2))


def spot(f, L, offset, half=3):
    """Median of the 2*half+1 points nearest to `offset` (NaN if outside the data)."""
    if len(f) == 0 or offset < f[0] * 0.98 or offset > f[-1] * 1.02:
        return math.nan
    i = int(np.argmin(np.abs(f - offset)))
    seg = L[max(i - half, 0):i + half + 1]
    seg = seg[np.isfinite(seg)]
    return float(np.median(seg)) if len(seg) else math.nan


def value_at(f, L, offset):
    """Interpolated value on a log axis (for markers on the displayed curve)."""
    if len(f) == 0 or offset < f[0] * 0.98 or offset > f[-1] * 1.02:
        return math.nan
    return float(np.interp(np.log10(offset), np.log10(f), L))


# --- full processing chain used by GUI and exports ------------------------------------
@dataclass
class ProcessOptions:
    sideband: str = "Average"
    smoothing: float = 0.0          # fraction of a decade
    remove_spurs: bool = False
    spur_threshold: float = 10.0    # dB above local median
    subtract_floor: bool = False
    floor_margin: float = 1.0


@dataclass
class Processed:
    f: np.ndarray
    raw: np.ndarray          # chosen sideband, before anything else
    clean: np.ndarray        # spur-removed (if enabled) and floor-subtracted (if enabled)
    display: np.ndarray      # smoothed; with floor subtraction: smooth(DUT) - smooth(floor)
    spurs: list = field(default_factory=list)
    measured: np.ndarray | None = None  # with floor subtraction: smoothed DUT before subtraction


def process(curve: PNCurve, opt: ProcessOptions, floor: PNCurve | None = None) -> Processed:
    f = curve.f
    raw = curve.sideband(opt.sideband)
    spurs, mask, base = find_spurs(f, raw, curve.rbw, opt.spur_threshold, enbw=curve.enbw)
    pre = np.where(mask, base, raw) if opt.remove_spurs else raw.copy()
    if opt.subtract_floor and floor is not None and len(floor.f):
        fl = floor.sideband(opt.sideband)
        if opt.remove_spurs:
            _, fm, fb = find_spurs(floor.f, fl, floor.rbw, opt.spur_threshold, enbw=floor.enbw)
            fl = np.where(fm, fb, fl)
        clean = subtract_floor(f, pre, floor.f, fl, opt.floor_margin)
        # for display, smooth both before subtracting: far fewer noise-induced gaps
        measured = smooth(f, pre, opt.smoothing)
        display = subtract_floor(f, measured, floor.f, smooth(floor.f, fl, opt.smoothing),
                                 opt.floor_margin)
        return Processed(f, raw, clean, display, spurs, measured)
    return Processed(f, raw, pre, smooth(f, pre, opt.smoothing), spurs)
