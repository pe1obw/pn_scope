"""ADALM-Pluto / Pluto+ (AD9361 / AD9363 / AD9364) as an FFT 'spectrum analyzer' via libiio.

The SDR captures IQ samples; each 'sweep' is a Welch power spectrum whose window is sized so
its 3 dB bandwidth equals the requested RBW. To keep the carrier away from the receiver's DC
offset correction and LO leakage, narrow spans are received with the carrier at +Fs/4; spans
too wide for that use the carrier at DC (the offsets used there are far from 0 Hz anyway).
Spans wider than the instantaneous bandwidth (full-span carrier search) are covered by
stepping the LO.

Every capture that contains the carrier is normalised to the carrier power of the first one, so
gain and filter differences between sample rates cancel. Levels are therefore relative (shown
as dBm but really 'dB re. the first carrier reading').

Needs libiio (system library) and its Python binding: ``pip install pylibiio`` (0.25 for
libiio 0.2x, 1.0 for libiio 1.x)."""
from __future__ import annotations

import math
import threading
import time

import numpy as np

from .base import AnalyzerError, Cancelled, SpectrumAnalyzer, steps_125
from .registry import register


def _blackman_harris(n):
    k = np.arange(n) * 2 * np.pi / n
    return 0.35875 - 0.48829 * np.cos(k) + 0.14128 * np.cos(2 * k) - 0.01168 * np.cos(3 * k)


def _window_bandwidths(n=4096, pad=16):
    """(3 dB bandwidth, noise bandwidth) of the window in FFT bins."""
    w = _blackman_harris(n)
    enbw = n * np.sum(w ** 2) / np.sum(w) ** 2
    W = np.abs(np.fft.fft(w, n * pad)) ** 2
    W /= W[0]
    k = np.argmax(W < 0.5)
    bw3 = 2 * (k - 1 + (W[k - 1] - 0.5) / (W[k - 1] - W[k])) / pad
    return bw3, enbw


BW3_BINS, ENBW_BINS = _window_bandwidths()


def _smooth_len(n):
    """Smallest integer >= n whose prime factors are 2, 3 and 5 (fast FFT sizes)."""
    n = int(math.ceil(n))
    best = 1 << max(0, (n - 1).bit_length())
    p5 = 1
    while p5 <= best:
        p35 = p5
        while p35 <= best:
            p = p35
            while p < n:
                p *= 2
            best = min(best, p)
            p35 *= 3
        p5 *= 5
    return best


@register
class PlutoSDR(SpectrumAnalyzer):
    model_name = "PlutoSDR (AD9361)"
    needs_host = True
    address_label = "IIO URI / IP"
    default_address = "ip:192.168.2.1"
    rbw_steps = steps_125(1, 10e6)
    min_freq = 70e6
    max_freq = 6e9
    enbw_factor = ENBW_BINS / BW3_BINS  # Blackman-Harris: ~1.055

    FS_MIN = 2.083334e6
    FS_MAX = 61.44e6
    MAX_CAPTURE = 1 << 22        # samples per capture (network transfer ~1 Msample/s)
    # ADC peak level the gain is set for: the strongest signal in the band (not only the
    # carrier) at about -4.4 dBFS. For inputs above ~-45 dBm the receiver's noise floor is
    # a fixed ~-128 dBFS/Hz, so this level sets the lowest measurable L(f) (~-123 dBc/Hz).
    TARGET_PEAK = 0.6
    POINTS = 751

    def __init__(self, uri: str = "ip:192.168.2.1", rx: int = 1):
        uri = uri.strip() or self.default_address
        self.uri = uri if ":" in uri else "ip:" + uri
        self.rx = rx
        self.ctx = None
        self.cfg = None
        self.buf = None
        self.carrier_ref = None
        self.carrier_expected = True
        self.fixed_offset = None   # background runs: dBFS -> output offset of the measurement
        self.last_offset = None
        self.adc_peak = None

    def segment_state(self):
        return {"gain": float(self._get(self.ch, "hardwaregain").split()[0]),
                "offset": None if self.last_offset is None else round(float(self.last_offset), 3),
                "adc_peak": None if self.adc_peak is None else round(float(self.adc_peak), 3)}

    def apply_segment_state(self, state):
        """Background run: same gain, and the same dBFS -> dBc scale as the measurement."""
        if "gain" in state and self.cfg and self.cfg.get("mode") == "fft":
            self._set(self.ch, "hardwaregain", state["gain"])
            self._make_buffer(self.cfg["ncap"])  # drop samples taken with the old gain
            off = state.get("offset")
            self.fixed_offset = -state["gain"] if off is None else off

    def set_carrier_expected(self, on):
        # without carrier: absolute (input-referred) levels, the same scale as the carrier
        # reference of the measurement the background belongs to
        self.carrier_expected = on

    # --- connection -----------------------------------------------------------------
    def connect(self):
        try:
            import iio
        except ImportError as e:
            raise AnalyzerError("Python module 'iio' missing: pip install pylibiio "
                                "(and install libiio)") from e
        self._iio = iio
        try:
            self.ctx = iio.Context(self.uri)
        except OSError as e:
            raise AnalyzerError(f"cannot connect to {self.uri}: {e}") from e
        self.ctx.set_timeout(30000)
        self.phy = self.ctx.find_device("ad9361-phy")
        self.dev = self.ctx.find_device("cf-ad9361-lpc")
        if self.phy is None or self.dev is None:
            raise AnalyzerError(f"{self.uri} is not an AD936x (Pluto) device")
        self.lo = self.phy.find_channel("altvoltage0", True)
        self.ch = self.phy.find_channel(f"voltage{self.rx - 1}", False)
        lo_range = self.lo.attrs["frequency_available"].value.strip("[]").split()
        self.min_freq, self.max_freq = float(lo_range[0]), float(lo_range[-1])
        i_ch, q_ch = f"voltage{2 * (self.rx - 1)}", f"voltage{2 * (self.rx - 1) + 1}"
        for c in self.dev.channels:
            if c.scan_element:
                c.enabled = c.id in (i_ch, q_ch)

    def close(self):
        self.buf = None
        self.ctx = None

    def identify(self):
        a = self.ctx.attrs
        return f"{a.get('hw_model', 'Pluto')}, fw {a.get('fw_version', '?')}, {self.uri}, RX{self.rx}"

    def _get(self, chan, attr):
        return chan.attrs[attr].value

    def _set(self, chan, attr, value):
        chan.attrs[attr].value = str(value)

    # --- state ------------------------------------------------------------------------
    def save_state(self):
        return {"lo": self._get(self.lo, "frequency"),
                "fs": self._get(self.ch, "sampling_frequency"),
                "bw": self._get(self.ch, "rf_bandwidth"),
                "gain_mode": self._get(self.ch, "gain_control_mode"),
                "gain": self._get(self.ch, "hardwaregain").split()[0]}

    def restore_state(self, s):
        self.buf = None
        self._set(self.ch, "sampling_frequency", s["fs"])
        self._set(self.ch, "rf_bandwidth", s["bw"])
        self._set(self.lo, "frequency", s["lo"])
        self._set(self.ch, "gain_control_mode", s["gain_mode"])
        if s["gain_mode"] == "manual":
            self._set(self.ch, "hardwaregain", s["gain"])

    def setup_noise_mode(self):
        self._set(self.ch, "gain_control_mode", "manual")
        self.carrier_ref = None

    # --- configuration ---------------------------------------------------------------------
    def configure(self, center, span, rbw, vbw):
        """Pick sample rate, FFT length and LO so the window's 3 dB bandwidth equals rbw."""
        if span <= self.FS_MAX / 3:
            fs_min, if_off = max(self.FS_MIN, 3 * span), 0.25   # carrier at +Fs/4
        elif span <= 0.8 * self.FS_MAX:
            fs_min, if_off = max(self.FS_MIN, span / 0.8), 0.0  # carrier at DC
        else:
            fs_min, if_off = None, None                         # hop the LO
        if fs_min is None:
            self.cfg = dict(mode="hop", center=center, span=span, rbw=rbw)
            return self._hop_plan(center, span)[1]
        nfft = max(64, _smooth_len(BW3_BINS * fs_min / rbw))
        fs = rbw * nfft / BW3_BINS
        if fs > self.FS_MAX:
            nfft = int(BW3_BINS * self.FS_MAX / rbw)
            fs = rbw * nfft / BW3_BINS
        if nfft > self.MAX_CAPTURE:
            raise AnalyzerError(f"RBW {rbw:g} Hz too narrow for this span on the Pluto")
        ncap = min(2 * nfft, self.MAX_CAPTURE)  # 3 half-overlapping segments when it fits
        lo = center - if_off * fs
        self.fixed_offset = None
        self.cfg = dict(mode="fft", center=center, span=span, rbw=rbw, fs=fs, nfft=nfft,
                        ncap=ncap, if_hz=if_off * fs, lo=lo)
        self._tune(lo, fs)
        self._make_buffer(1 << 16)  # small buffer for setting the gain
        self._autogain()
        self._make_buffer(ncap)
        return ncap / fs + ncap / 1.5e6  # capture + transfer estimate

    def _tune(self, lo, fs):
        self._set(self.ch, "sampling_frequency", int(round(fs)))
        self._set(self.ch, "rf_bandwidth", int(min(56e6, max(200e3, fs))))
        self._set(self.lo, "frequency", int(round(lo)))
        # the driver may round the sample rate; use what it really is
        if self.cfg and self.cfg.get("mode") == "fft":
            self.cfg["fs_actual"] = float(self._get(self.ch, "sampling_frequency"))

    def _make_buffer(self, n):
        self.buf = None
        self.buf = self._iio.Buffer(self.dev, int(n), False)
        self.buf.refill()  # discard: may contain samples from before the retune

    def _capture(self) -> np.ndarray:
        self.buf.refill()
        x = np.frombuffer(self.buf.read(), dtype=np.int16).astype(np.float32)
        return (x[0::2] + 1j * x[1::2]) / 2048.0

    def _autogain(self):
        """Set the gain so the strongest signal in the band peaks at TARGET_PEAK. A clipped
        reading says nothing about how far over it is, so then step down 20 dB at a time.
        A fresh buffer after every change: queued samples would still have the old gain."""
        g = float(self._get(self.ch, "hardwaregain").split()[0])
        for _ in range(10):
            peak = float(np.max(np.abs(self._capture()))) or 1e-6
            if peak >= 0.95:
                step = -20.0
            else:
                step = 20 * math.log10(self.TARGET_PEAK / peak)
                if abs(step) < 2:
                    break
            new = float(np.clip(round(g + step), -1, 71))
            if new == g:
                break  # at the end of the range
            g = new
            self._set(self.ch, "hardwaregain", g)
            self._make_buffer(1 << 16)
        self.adc_peak = peak

    # --- spectra --------------------------------------------------------------------------------
    def _psd(self, x, nfft):
        """Welch average of amplitude-normalised spectra: a CW at a bin centre reads its power."""
        w = _blackman_harris(nfft).astype(np.float32)
        step = nfft // 2
        acc, n = None, 0
        for s in range(0, len(x) - nfft + 1, step):
            X = np.fft.fft(x[s:s + nfft] * w)
            p = (X.real ** 2 + X.imag ** 2)
            acc = p if acc is None else acc + p
            n += 1
        return np.fft.fftshift(acc / n) / np.sum(w) ** 2

    def sweep(self, cancel: threading.Event | None = None):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        c = self.cfg
        if c is None:
            raise AnalyzerError("not configured")
        if c["mode"] == "hop":
            return self._hop_sweep(cancel)
        fs = c.get("fs_actual", c["fs"])
        nfft = c["nfft"]
        S = self._psd(self._capture(), nfft)
        f_if = (np.arange(nfft) - nfft // 2) * fs / nfft  # bin frequencies relative to LO
        grid = c["if_hz"] + c["span"] * (np.arange(self.POINTS) / (self.POINTS - 1) - 0.5)
        out = self._to_grid(f_if, S, grid)
        out_db = 10 * np.log10(np.maximum(out, 1e-30))  # dBFS
        if self.fixed_offset is not None:  # background run: the measurement's own scale
            return out_db + self.fixed_offset
        self.last_offset = self._offset(f_if, S, c)
        return out_db + self.last_offset

    @staticmethod
    def _to_grid(f, S, grid):
        """Like an averaging detector: mean of the bins within each grid cell; interpolate where
        cells are narrower than a bin."""
        df = f[1] - f[0]
        cell = grid[1] - grid[0]
        if cell <= 1.5 * df:
            return np.interp(grid, f, S)
        edges = np.concatenate([[grid[0] - cell / 2], (grid[:-1] + grid[1:]) / 2, [grid[-1] + cell / 2]])
        idx = np.clip(np.searchsorted(f, edges), 0, len(f))
        # per-cell sums (not a running sum: next to a strong carrier the noise would be lost
        # in the rounding error of the cumulative total). A zero appended so the last cell
        # ends at idx[-1] and not at the end of the array.
        S64 = np.concatenate([S.astype(np.float64), [0.0]])
        sums = np.add.reduceat(S64, idx)[:-1]
        cnt = idx[1:] - idx[:-1]
        empty = cnt <= 0
        out = sums / np.maximum(cnt, 1)
        out[empty] = np.interp(grid[empty], f, S)
        return out

    def _offset(self, f_if, S, c):
        """dB to add to dBFS. Normally -gain (input referred). With a carrier in the capture:
        such that the carrier power equals that of the first capture of the run, so gain and
        filter differences between segments cancel."""
        gain = float(self._get(self.ch, "hardwaregain").split()[0])
        near = np.abs(f_if - c["if_hz"]) <= max(0.05 * c["span"], 5 * c["rbw"])
        if not near.any() or not self.carrier_expected:
            return -gain
        k = int(np.flatnonzero(near)[np.argmax(S[near])])
        if 10 * np.log10(S[k] / np.median(S)) < 30:
            return -gain  # no clear carrier in this capture
        p_carrier = 10 * np.log10(np.sum(S[max(k - 4, 0):k + 5]) / ENBW_BINS)
        if self.carrier_ref is None:
            self.carrier_ref = p_carrier - gain
        return self.carrier_ref - p_carrier

    # --- wide spans: step the LO ------------------------------------------------------------------
    def _hop_plan(self, center, span):
        """LO positions half a usable bandwidth apart, so that every frequency is seen from
        two LO positions (at different IF frequencies)."""
        width = 0.7 * self.FS_MAX
        step = width / 2
        n = int(math.ceil(span / step)) + 1
        start = center - span / 2
        centers = [start + step * (i - 0.5) + width / 2 - step / 2 for i in range(n)]
        return centers, n * 0.03

    def _hop_sweep(self, cancel):
        """Peak-detected overview of a wide span. Each grid cell is seen from two LO positions;
        taking the lower of the two readings removes mirror images of strong signals (they
        move with the LO, real signals don't) that appear while the AD9361's quadrature
        correction settles after a large LO step."""
        c = self.cfg
        centers, _ = self._hop_plan(c["center"], c["span"])
        grid = c["center"] + c["span"] * (np.arange(self.POINTS) / (self.POINTS - 1) - 0.5)
        cell = grid[1] - grid[0]
        nfft = 1024
        self._set(self.ch, "hardwaregain", 10)
        self.cfg = None
        self._tune(min(max(centers[0], self.min_freq), self.max_freq), self.FS_MAX)
        self._make_buffer(nfft * 2)
        fs = float(self._get(self.ch, "sampling_frequency"))
        f_if = (np.arange(nfft) - nfft // 2) * fs / nfft
        use = (np.abs(f_if) > 4 * fs / nfft) & (np.abs(f_if) < 0.36 * fs)  # skip DC and band edges
        obs = []  # per LO position: peak per grid cell (NaN where not covered)
        for lo in centers:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            lo = min(max(lo, self.min_freq), self.max_freq)
            self._set(self.lo, "frequency", int(round(lo)))
            # a new buffer: libiio queues several blocks, so samples taken before the retune
            # would otherwise still come out of the old one
            self._make_buffer(nfft * 2)
            S = self._psd(self._capture(), nfft)
            k = np.round((lo + f_if[use] - grid[0]) / cell).astype(int)
            ok = (k >= 0) & (k < self.POINTS)
            o = np.full(self.POINTS, np.nan)
            np.fmax.at(o, k[ok], S[use][ok])
            obs.append(o)
        self.cfg = c
        obs = np.array(obs)
        seen = np.isfinite(obs)
        n_seen = seen.sum(axis=0)
        lowest = np.nanmin(np.where(seen, obs, np.nan), axis=0, initial=np.inf,
                           where=seen) if obs.size else np.full(self.POINTS, np.nan)
        out = np.where(n_seen >= 2, lowest, np.where(n_seen == 1, np.nanmax(np.where(seen, obs, -np.inf), axis=0), np.nan))
        floor = np.nanmedian(out) if np.isfinite(out).any() else 1e-30
        out = np.where(np.isfinite(out), out, floor)
        return 10 * np.log10(np.maximum(out, 1e-30)) - 10

    # --- misc ----------------------------------------------------------------------------------------
    def get_center(self):
        return float(self._get(self.lo, "frequency"))

    def get_span(self):
        return float(self._get(self.ch, "sampling_frequency"))

    has_ref_level = False

    def get_ref_level(self):
        return 30.0

    def set_ref_level(self, dbm):
        pass  # gain is set automatically for every configuration
