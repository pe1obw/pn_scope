"""Any IIO receiver with a fixed LO and sample rate as an FFT 'spectrum analyzer' via libiio.

Nothing on the device is changed: no tuning, no gain, no sample rate. The driver captures IQ
samples from a buffer-capable IIO device at whatever LO the device (or another tool) has set,
shifts the carrier to 0 Hz with a numerical oscillator, low-pass filters and decimates to a
rate that suits the span, and computes a Welch spectrum whose 3 dB bandwidth equals the RBW
(same window and detector as the Pluto driver). The carrier must lie within the captured band
(|f - LO| < 0.45 Fs) and preferably well away from the LO (DC offset correction, LO leakage).

Levels are dBFS (shown as dBm): a full-scale complex tone reads 0 dB.

Address: an IIO URI (or just an IP address), optionally followed by space-separated options:

    ip:192.168.2.1 ch=1 fs=61.44M lo=<chip>-phy0/altvoltage0

    dev=NAME      buffer device (name, label or iio:deviceN); default: the first device
                  with input scan elements
    ch=N          complex channel (I/Q pair) number on that device, from 0; default 0
    fs=HZ         sample rate of the samples arriving in the buffer; default: the channel's
                  (or device's) sampling_frequency attribute. Needed when the FPGA decimates
                  without reporting it.
    lo=HZ         LO frequency, so frequencies are RF frequencies; or DEVICE/CHANNEL to read
                  the 'frequency' attribute of that channel (re-read at every configuration);
                  default: an RX_LO / TRX_LO channel if there is exactly one, else 0 (all
                  frequencies relative to the LO)
    contig=MIB    largest gap-free capture in MiB (default 16; 0: no limit). The ADI AXI DMA
                  splits a buffer into 16 MiB transfers and may lose samples between them,
                  so longer captures have phase jumps. This sets the narrowest RBW.
    maxcap=N      samples per sweep, all captures together (default 2^25)

Number values accept k/M/G suffixes."""
from __future__ import annotations

import math
import re
import threading

import numpy as np

from .base import AnalyzerError, Cancelled, SpectrumAnalyzer, steps_125
from .pluto import BW3_BINS, ENBW_BINS, PlutoSDR, welch_psd
from .registry import register

_SUFFIX = {"k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}


def _num(s: str) -> float:
    s = s.strip()
    if s and s[-1] in _SUFFIX:
        return float(s[:-1]) * _SUFFIX[s[-1]]
    return float(s)


def parse_address(address: str) -> tuple[str, dict]:
    """'ip:1.2.3.4 ch=1 fs=61.44M' -> ('ip:1.2.3.4', {'ch': '1', 'fs': '61.44M'})"""
    parts = [p for p in re.split(r"[\s;]+", address.strip()) if p]
    if not parts or "=" in parts[0]:
        raise AnalyzerError("address must start with an IIO URI or IP address")
    uri, opts = parts[0], {}
    for p in parts[1:]:
        k, sep, v = p.partition("=")
        if not sep or not v:
            raise AnalyzerError(f"bad option '{p}' (expected key=value)")
        k = k.lower()
        if k not in ("dev", "ch", "fs", "lo", "maxcap", "contig"):
            raise AnalyzerError(f"unknown option '{k}' (dev, ch, fs, lo, maxcap, contig)")
        opts[k] = v
    if ":" not in uri:
        uri = "ip:" + uri
    return uri, opts


def _smooth_numbers(limit):
    """All integers <= limit whose prime factors are 2, 3 and 5, sorted."""
    out, p5 = [], 1
    while p5 <= limit:
        p35 = p5
        while p35 <= limit:
            p = p35
            while p <= limit:
                out.append(p)
                p *= 2
            p35 *= 3
        p5 *= 5
    return sorted(out)


def smooth_floor(n):
    """Largest 5-smooth integer <= n (n >= 1)."""
    n = max(1, int(n))
    s = _smooth_numbers(n)
    return s[-1]


def smooth_near(n):
    """5-smooth integer closest to n (fast FFT length)."""
    n = max(1, int(round(n)))
    s = _smooth_numbers(2 * n)
    return min(s, key=lambda v: (abs(v - n), v))


def _stage_factors(d):
    """Split decimation factor d (5-smooth) into stages of at most 16."""
    primes = []
    for p in (5, 3, 2):
        while d % p == 0:
            primes.append(p)
            d //= p
    stages = []
    for p in primes:
        if stages and stages[-1] * p <= 16:
            stages[-1] *= p
        else:
            stages.append(p)
    return sorted(stages, reverse=True)


def kaiser_lowpass(fin, fpass, fstop, atten=100.0):
    """Windowed-sinc low-pass (Kaiser), unity DC gain; frequencies in Hz at rate fin."""
    dw = 2 * math.pi * (fstop - fpass) / fin
    n = int(math.ceil((atten - 8) / (2.285 * dw))) + 1
    n |= 1  # odd: symmetric around a sample
    beta = 0.1102 * (atten - 8.7)
    fc = 0.5 * (fpass + fstop) / fin
    k = np.arange(n) - (n - 1) / 2
    h = 2 * fc * np.sinc(2 * fc * k) * np.kaiser(n, beta)
    return (h / h.sum()).astype(np.float32)


def fir_decimate(x, h, d):
    """y[m] = sum_k h[k] x[m d + k] for all m where the filter fits."""
    L = len(h)
    m = (len(x) - L) // d + 1
    if m <= 0:
        return np.zeros(0, np.complex64)
    y = np.zeros(m, np.complex64)
    span = d * (m - 1) + 1
    for k in range(L):
        y += h[k] * x[k:k + span:d]
    return y


@register
class GenericIIO(SpectrumAnalyzer):
    model_name = "Generic IIO SDR"
    needs_host = True
    address_label = "IIO URI [options]"
    default_address = "ip:192.168.2.1"
    rbw_steps = steps_125(1, 10e6)
    has_ref_level = False
    enbw_factor = ENBW_BINS / BW3_BINS

    MAX_CAPTURE = 1 << 25        # samples per sweep (all captures together)
    CONTIG_MIB = 16              # contiguous DMA transfer (ADI AXI DMAC: 24-bit length)
    TRANSFER_RATE = 90e6         # bytes/s over the network (estimate for the sweep time)
    CHUNK = 1 << 21              # input samples per DSP block (memory use)
    POINTS = 751

    def __init__(self, address: str = "ip:192.168.2.1"):
        self.uri, self.opts = parse_address(address or self.default_address)
        self.max_capture = int(_num(self.opts["maxcap"])) if "maxcap" in self.opts else self.MAX_CAPTURE
        self.ctx = None
        self.buf = None
        self.buf_len = 0
        self.dev = None
        self.ch_ids = None
        self.fs = None
        self.lo = 0.0
        self.lo_chan = None        # iio channel whose 'frequency' attribute is the LO
        self.lo_desc = "0 (unknown: frequencies relative to the LO)"
        self.dtype = np.dtype("<i2")
        self.shift = 0
        self.full_scale = 32768.0
        self.cfg = None
        self.adc_peak = None
        self.warnings: list[str] = []

    # --- connection -------------------------------------------------------------------------
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
        self.dev = self._find_buffer_device()
        pairs = self._iq_pairs(self.dev)
        k = int(self.opts.get("ch", 0))
        if not 0 <= k < len(pairs):
            raise AnalyzerError(f"ch={k}: {self._dev_name(self.dev)} has {len(pairs)} complex "
                                f"channels (ch=0..{len(pairs) - 1})")
        i_ch, q_ch = pairs[k]
        self.ch_index = k
        self.ch_ids = (i_ch.id, q_ch.id)
        for c in self.dev.channels:
            if c.scan_element and not c.output:
                c.enabled = c.id in self.ch_ids
        fmt = i_ch.data_format
        if fmt.length not in (16, 32) or fmt.repeat not in (0, 1):
            raise AnalyzerError(f"unsupported sample format ({fmt.length} bit storage)")
        self.dtype = np.dtype(("<" if not fmt.is_be else ">") + ("i" if fmt.is_signed else "u")
                              + str(fmt.length // 8))
        self.shift = fmt.shift
        self.full_scale = float(2 ** ((fmt.bits or fmt.length) - 1))
        self.fs = self._sample_rate(i_ch)
        self._setup_lo()
        self.min_freq = self.lo - 0.45 * self.fs
        self.max_freq = self.lo + 0.45 * self.fs

    def close(self):
        self.buf = None
        self.buf_len = 0
        self.ctx = None

    @staticmethod
    def _dev_name(d):
        label = d.attrs["label"].value if "label" in d.attrs else getattr(d, "label", None)
        return label or d.name or d.id

    def _get_device(self, name):
        for d in self.ctx.devices:
            names = {d.id, d.name}
            try:
                names.add(getattr(d, "label", None) or "")
            except Exception:  # noqa: BLE001 - older bindings
                pass
            if "label" in d.attrs:
                try:
                    names.add(d.attrs["label"].value)
                except OSError:
                    pass
            if name in names:
                return d
        raise AnalyzerError(f"no IIO device '{name}' on {self.uri}")

    def _find_buffer_device(self):
        if "dev" in self.opts:
            d = self._get_device(self.opts["dev"])
            if not self._iq_pairs(d):
                raise AnalyzerError(f"{self.opts['dev']} has no input channels with a buffer")
            return d
        cands = [d for d in self.ctx.devices if self._iq_pairs(d)]
        if not cands:
            raise AnalyzerError(f"{self.uri}: no IIO device with buffered input channels")
        key = lambda d: int(d.id.split("device")[-1]) if d.id.split("device")[-1].isdigit() else 1 << 30
        return sorted(cands, key=key)[0]

    @staticmethod
    def _iq_pairs(dev):
        """Complex channels as (I, Q) channel pairs: 'voltageN_i'/'voltageN_q' by name,
        otherwise consecutive scan elements (AD936x style: voltage0 = I, voltage1 = Q)."""
        chans = sorted([c for c in dev.channels if c.scan_element and not c.output
                        and c.id.startswith("voltage")], key=lambda c: c.index)
        by_id = {c.id: c for c in chans}
        pairs = []
        if any(c.id.endswith("_i") for c in chans):
            for c in chans:
                if c.id.endswith("_i") and c.id[:-2] + "_q" in by_id:
                    pairs.append((c, by_id[c.id[:-2] + "_q"]))
        else:
            pairs = [(chans[i], chans[i + 1]) for i in range(0, len(chans) - 1, 2)]
        return pairs

    def _sample_rate(self, chan):
        if "fs" in self.opts:
            return _num(self.opts["fs"])
        for attrs in (chan.attrs, self.dev.attrs):
            if "sampling_frequency" in attrs:
                try:
                    fs = float(attrs["sampling_frequency"].value)
                except (OSError, ValueError):
                    continue
                if fs > 0:
                    return fs
        raise AnalyzerError(f"{self._dev_name(self.dev)} does not report its sample rate: "
                            "add fs=<rate> to the address")

    def _setup_lo(self):
        spec = self.opts.get("lo")
        if spec is not None:
            try:
                self.lo = _num(spec)
                self.lo_desc = f"{self.lo / 1e6:.6f} MHz (given)"
                return
            except ValueError:
                pass
            dev_name, _, ch_name = spec.rpartition("/")
            if not dev_name:
                raise AnalyzerError(f"lo={spec}: give a frequency or DEVICE/CHANNEL")
            d = self._get_device(dev_name)
            ch = d.find_channel(ch_name, True)
            if ch is None or "frequency" not in ch.attrs:
                raise AnalyzerError(f"lo={spec}: no output channel '{ch_name}' with a "
                                    f"frequency attribute on {dev_name}")
            self.lo_chan = ch
            self.lo_desc = f"{spec}"
        else:
            found = []
            for d in self.ctx.devices:
                for c in d.channels:
                    if c.output and c.id.startswith("altvoltage") and "frequency" in c.attrs:
                        name = (c.name or "").upper()
                        if name in ("RX_LO", "TRX_LO"):
                            found.append((d, c))
            if len(found) == 1:
                self.lo_chan = found[0][1]
                self.lo_desc = f"{self._dev_name(found[0][0])}/{found[0][1].id} ({found[0][1].name})"
            else:
                return
        self._read_lo()

    def _read_lo(self):
        if self.lo_chan is not None:
            self.lo = float(self.lo_chan.attrs["frequency"].value)
            self.min_freq = self.lo - 0.45 * self.fs
            self.max_freq = self.lo + 0.45 * self.fs

    def identify(self):
        return (f"Generic IIO {self._dev_name(self.dev)} {'/'.join(self.ch_ids)} (ch={self.ch_index}), "
                f"fs {self.fs / 1e6:g} MS/s, LO {self.lo_desc}"
                + (f" = {self.lo / 1e6:.6f} MHz" if self.lo_chan is not None else "")
                + f", {self.uri}")

    # --- state: nothing on the device is changed ----------------------------------------------
    def save_state(self):
        return {}

    def restore_state(self, state):
        pass

    def setup_noise_mode(self):
        self.warnings = []

    def _warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def errors(self):
        return list(self.warnings)

    def segment_state(self):
        return {"adc_peak": None if self.adc_peak is None else round(float(self.adc_peak), 3)}

    # --- configuration ------------------------------------------------------------------------
    def capture_limit(self):
        """Samples per capture: one contiguous DMA transfer, at most maxcap."""
        contig = float(self.opts.get("contig", self.CONTIG_MIB))
        if contig <= 0:
            return self.max_capture
        return min(self.max_capture, int(contig * 2 ** 20) // (2 * self.dtype.itemsize))

    def plan(self, center, span, rbw):
        """Decimation stages, FFT length, capture length and number of captures."""
        fs = self.fs
        f_if = center - self.lo
        if abs(f_if) > 0.5 * fs:
            raise AnalyzerError(
                f"{center / 1e6:.6f} MHz is outside the captured band (LO {self.lo / 1e6:.6f} MHz "
                f"± {fs / 2e6:g} MHz)")
        # decimated rate >= 2 x span: wide transition bands, so short filters (the filters'
        # settling time costs capture length, which limits the narrowest RBW)
        d_total = smooth_floor(fs / (2 * span)) if span < fs / 2 else 1
        fs_dec = fs / d_total
        stages, fin = [], fs
        for d in _stage_factors(d_total):
            fo = fin / d
            # pass |f| <= span/2; reject everything that would fold into it
            stages.append((d, kaiser_lowpass(fin, 0.5 * span, fo - 0.5 * span)))
            fin = fo
        # input samples spanned by one output sample's impulse response
        l_tot, step = 1, 1
        for d, h in stages:
            l_tot += (len(h) - 1) * step
            step *= d
        nfft = max(64, smooth_near(BW3_BINS * fs_dec / rbw))
        limit = self.capture_limit()
        avail = (limit - l_tot - 64) // d_total   # decimated samples one capture can give
        if avail < 64:
            raise AnalyzerError(f"span {span:g} Hz needs longer contiguous captures "
                                f"({limit} samples; see contig=/maxcap=)")
        if nfft > avail:
            nfft = smooth_floor(avail)
            self._warn(f"RBW {rbw:g} Hz needs longer contiguous captures than {limit} samples "
                       f"({limit / fs * 1e3:.0f} ms); measured with {BW3_BINS * fs_dec / nfft:.3g} Hz "
                       "(noise bandwidth corrected, but carrier skirt is wider)")
        ncap = min(avail, 2 * nfft)
        segs = 1 + (ncap - nfft) // (nfft // 2)  # half-overlapping Welch segments per capture
        n_in = -(-((ncap - 1) * d_total + l_tot) // 64) * 64
        # at least three segments per sweep, from several captures if one is not enough
        n_caps = max(1, min(-(-3 // segs), self.max_capture // n_in))
        return dict(center=center, span=span, rbw=rbw, f_if=f_if, stages=stages,
                    d_total=d_total, fs_dec=fs_dec, nfft=nfft, ncap=ncap, segs=segs,
                    n_in=n_in, n_caps=n_caps)

    def configure(self, center, span, rbw, vbw):
        self._read_lo()
        c = self.plan(center, span, rbw)
        if abs(c["f_if"]) < 0.5 * span + 5 * rbw:
            self._warn(f"offsets beyond {abs(c['f_if']) / 1e3:.0f} kHz include the LO frequency "
                       "(DC offset / LO leakage): move the LO further from the carrier")
        self.cfg = c
        self._ensure_buffer(c["n_in"])
        n = c["n_in"]
        return c["n_caps"] * (n / self.fs + n * 2 * self.dtype.itemsize / self.TRANSFER_RATE
                              + n * 3e-8)

    def enbw_ratio(self, rbw):
        """Noise bandwidth of the actual window / nominal RBW (the FFT length is rounded to a
        fast size, and may be limited by the capture length)."""
        c = self.cfg
        if c is None or abs(c["rbw"] - rbw) > 1e-9 * rbw:
            return self.enbw_factor
        return ENBW_BINS * c["fs_dec"] / c["nfft"] / rbw

    def _ensure_buffer(self, n):
        if self.buf is not None and self.buf_len == n:
            return
        self.buf = None
        try:
            self.buf = self._iio.Buffer(self.dev, int(n), False)
        except OSError as e:
            raise AnalyzerError(f"cannot create a {n}-sample buffer: {e} (try contig=)") from e
        self.buf_len = n

    def _capture(self) -> np.ndarray:
        """Raw interleaved I/Q integers."""
        self.buf.refill()
        return np.frombuffer(self.buf.read(), dtype=self.dtype)

    # --- signal processing --------------------------------------------------------------------
    def _baseband(self, raw, c, cancel=None):
        """Mix the carrier to 0 Hz and decimate: complex64 at fs_dec."""
        raw = raw[:2 * (len(raw) // 2)]
        n = len(raw) // 2
        peak = int(np.max(np.abs(raw.astype(np.int32)))) >> self.shift if n else 0
        self.adc_peak = peak / self.full_scale
        if self.adc_peak > 0.98:
            self._warn(f"ADC clipping (peak {self.adc_peak:.2f} of full scale): reduce the input level or gain")
        stages = c["stages"]
        d1, h1 = stages[0] if stages else (1, np.ones(1, np.float32))
        L1 = len(h1)
        cyc = c["f_if"] / self.fs          # NCO frequency in cycles/sample
        scale = 1.0 / (self.full_scale * (1 << self.shift))
        chunk = max(d1, self.CHUNK // d1 * d1)
        out = []
        for s in range(0, n - L1 + 1, chunk):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            e = min(n, s + chunk + L1 - 1)
            seg = raw[2 * s:2 * e].astype(np.float32)
            z = (seg[0::2] + 1j * seg[1::2]).astype(np.complex64)
            k = np.arange(s, e, dtype=np.float64)
            ph = (cyc * k) % 1.0           # float64 phase: no NCO phase noise
            z *= (np.exp(-2j * np.pi * ph) * scale).astype(np.complex64)
            out.append(fir_decimate(z, h1, d1))
        y = np.concatenate(out) if out else np.zeros(0, np.complex64)
        for d, h in stages[1:]:
            y = fir_decimate(y, h, d)
        return y

    def sweep(self, cancel: threading.Event | None = None):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        c = self.cfg
        if c is None:
            raise AnalyzerError("not configured")
        nfft = c["nfft"]
        S = None
        for _ in range(c["n_caps"]):  # every capture is contiguous; average their spectra
            y = self._baseband(self._capture(), c, cancel)
            if len(y) < c["ncap"]:
                raise AnalyzerError(f"capture too short ({len(y)} < {c['ncap']} samples after decimation)")
            p = welch_psd(y[:c["ncap"]], nfft)
            S = p if S is None else S + p
        S /= c["n_caps"]
        f = (np.arange(nfft) - nfft // 2) * c["fs_dec"] / nfft
        grid = c["span"] * (np.arange(self.POINTS) / (self.POINTS - 1) - 0.5)
        out = PlutoSDR._to_grid(f, S, grid)
        return 10 * np.log10(np.maximum(out, 1e-30))

    # --- misc ---------------------------------------------------------------------------------
    def get_center(self):
        self._read_lo()
        return self.cfg["center"] if self.cfg else self.lo

    def get_span(self):
        return self.fs

    def get_ref_level(self):
        return 0.0

    def set_ref_level(self, dbm):
        pass  # gain is not touched
