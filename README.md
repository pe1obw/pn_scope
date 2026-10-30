# PNScope

A utility for spectrum analyzers to make phase noise plots and do analysis on them —
for analyzers that have no phase-noise option.

Supported analyzers:

- Siglent SSA3000X series (tested: SSA3075X-R, firmware 3.2.2.6)
- A built-in simulated analyzer, to try the program without hardware

Adding another analyzer (e.g. Rigol RSA3000/5000) is a small driver class, see
[Writing a driver](#writing-a-driver).

## Install and run

```sh
python3 -m venv venv
venv/bin/pip install -e .          # numpy, PySide6, pyqtgraph, matplotlib
venv/bin/pnscope [files…]          # or: venv/bin/python -m pnscope
```

Run the tests with `venv/bin/pip install pytest && venv/bin/python -m pytest`.

## Functionality

The tool makes a plot with logarithmic frequency scale, showing the phase noise L(f) in dBc/Hz.

**Measuring** (left panel)

- Carrier frequency: type it (`1.5805 GHz`, `1580.42M`, …), take the analyzer's centre
  frequency (**Get**), or search the full span for the strongest signal (**Find**). Leave it empty
  to use the analyzer's centre. The carrier is then located precisely within the *search span*,
  and the measurement refuses to start if no clear carrier is there.
- Start / stop offset (e.g. 100 Hz … 1 MHz, or 30 Hz … 10 MHz). The range is split into one
  segment per decade; the table shows span, RBW and averages of each.
- Averages per segment (power averaging of sweeps).
- Advanced: re-centre on the carrier before each segment (tracks slow drift), set the reference
  level to carrier + 5 dB (best dynamic range), restore all analyzer settings afterwards,
  span/RBW ratio, minimum offset in RBWs, VBW ratio.
- The curve builds up live, segment by segment. **Stop** ends after the current sweep and keeps
  the finished segments.

Plots can only be made for CW signals. The analyzer's own phase noise limits what can be
measured: measure a clean generator the same way and use it as the reference trace.

**Analysing** (right panel and results tabs)

- Several traces overlaid; show/hide, rename (double-click), save, remove.
- Sideband: average of both sidebands, upper or lower only.
- Smoothing (moving average on the log axis, display only).
- Spur detection (points more than *threshold* above the local median), listed with level in dBc;
  optional removal (replaced by the local median) before integration.
- Reference subtraction: mark a trace as **Ref.** (analyzer floor) and subtract it from the others.
  Points within the margin of the floor count as zero noise, so results are a lower bound there.
- Integrated phase noise over a range (type it, use a preset, or drag the band on the plot):
  rms phase in degrees and mrad, integrated dBc, rms jitter, residual FM.
- Spot noise table at chosen offsets (default each decade).
- Markers: double-click the plot or **Add**, drag them, **To peak**; values for every trace, or Δ to M1.
  Double-click a spur to put a marker on it.
- Info tab: carrier, instrument, notes, segment settings, analyzer warnings.

**Export** (File menu)

- Plot as PNG, SVG or PDF (title, legend with integrated results, markers, integration band).
- CSV of the selected trace (both sidebands, average, processed curve, RBW per point).
- Markdown report with all tables and the plot.
- Copy the plot to the clipboard.

Measurements are saved as `.pnscope` files (JSON, raw averaged traces of every segment, so all
analysis can be redone later). The `<label>_meta.json` + `_segN.csv` output of the earlier
scripts (`example/`) opens directly.

## Background

Using the spectrum analyzer's spectrum analysis mode is the most typical, direct and widely
used method to measure phase noise.

The procedure is:

- Measure the carrier power (Pc) in dBm.
- Set a delta marker to a specific frequency offset from the carrier, i.e. a point in the phase noise sideband.
- Measure the noise power (Pn) at that offset, using the noise marker function to normalize the noise measured by the RBW filter to 1 Hz bandwidth.
- Repeat the process at different offsets from the carrier.

What doesn't help, is that in some Siglent firmware versions the delta noise marker function is broken.

Measuring phase noise with very small offsets from the carrier requires a very narrow resolution
bandwidth, to avoid measuring the carrier power instead.

### What PNScope does instead of the noise marker

1. Locate the carrier by zooming in (parabolic interpolation on the peak), and measure Pc with
   a wide RBW so the reading is not affected by the trace-point spacing.
2. For each offset decade, sweep a span of ±(top of the decade) around the carrier with
   RBW ≈ span/600 (≈ one RBW per trace point), RMS (average) detector, power averaging,
   VBW = 3 × RBW, and average N sweeps in the power domain.
3. Keep from each segment only offsets ≥ 10 × RBW (outside the carrier skirt), and stitch:

   `L(f) = P(f) − Pc − 10·log10(1.065 · RBW)`   [dBc/Hz]

   1.065 is the noise bandwidth of a Gaussian RBW filter relative to its 3 dB bandwidth. With an
   RMS detector no log-averaging correction (2.5 dB) is needed.
4. Integration is double-sideband: φ_rms = √(2 ∫ L(f) df), jitter = φ_rms / (2π·fc),
   residual FM = √(2 ∫ L(f)·f² df).

Spur levels are read as P − Pc at the spur's peak (they are not per Hz).

## Code layout

```text
pnscope/
  analyzers/base.py      SpectrumAnalyzer: the abstract interface the measurement uses
  analyzers/scpi.py      SocketTransport + ScpiSpectrumAnalyzer (command table driven)
  analyzers/siglent.py   Siglent SSA3000X driver
  analyzers/simulated.py software analyzer (synthetic carrier, noise and spurs)
  analyzers/registry.py  @register decorator; the GUI lists all registered drivers
  plan.py                offset range -> segments (span, RBW, averages)
  measure.py             PhaseNoiseRun: carrier search, segments, restore; find_carrier()
  model.py               Measurement/Segment, .pnscope files, legacy import
  analysis.py            stitching, sidebands, spurs, smoothing, floor subtraction,
                         integration, spot noise
  export.py              matplotlib figure, CSV, Markdown report
  gui/                   PySide6 + pyqtgraph user interface
```

The measurement and analysis code has no GUI dependency, so it can be scripted:

```python
from pnscope.analyzers import create
from pnscope.measure import PhaseNoiseRun, MeasureSettings
from pnscope.plan import PlanSettings
from pnscope import analysis as A

with create("Siglent SSA3000X", "192.168.10.108") as sa:
    m = PhaseNoiseRun(sa, PlanSettings(start=100, stop=10e6),
                      MeasureSettings(name="dut", carrier_freq=1e9)).run()
m.save("dut.pnscope")
c = A.stitch(m)
print(A.integrate(c.f, c.avg, 1e3, 1e6, m.carrier_freq).phase_deg)
```

## Writing a driver

The measurement needs only what `SpectrumAnalyzer` in `analyzers/base.py` declares: set
centre/span/RBW/VBW, single sweep with an RMS detector and power averaging, read the trace,
reference level, and save/restore the instrument state.

For a SCPI instrument reachable over LAN, subclass `ScpiSpectrumAnalyzer` and override the
command table where the dialect differs. Create `pnscope/analyzers/<vendor>.py`:

```python
from .base import steps_125
from .registry import register
from .scpi import ScpiSpectrumAnalyzer


@register
class RigolRSA(ScpiSpectrumAnalyzer):
    model_name = "Rigol RSA3000/5000"     # shown in the GUI
    needs_host = True
    rbw_steps = steps_125(1, 10e6)        # RBW values the instrument accepts
    min_freq, max_freq = 9e3, 3.2e9

    # Only the entries that differ from the base table (scpi.py). The values below are an
    # UNTESTED sketch — check every command against the programming guide.
    CMD = dict(ScpiSpectrumAnalyzer.CMD,
               detector_rms=":SENS:DET:TRAC RMS",
               average_power=":SENS:AVER:TYPE RMS",
               trace_write=":TRAC1:MODE WRIT",
               rbw=":SENS:BAND:RES {v:g}",
               vbw=":SENS:BAND:VID {v:g}",
               ref_level=":DISP:WIND:TRAC:Y:SCAL:RLEV {v:.1f}",
               **{"ref_level?": ":DISP:WIND:TRAC:Y:SCAL:RLEV?",
                  "trace?": ":TRAC:DATA? TRACE1"})

    # queried with '?' before the measurement and written back afterwards, in this order
    STATE = [":SENS:FREQ:CENT", ":SENS:FREQ:SPAN", ":DISP:WIND:TRAC:Y:SCAL:RLEV",
             ":SENS:BAND:RES", ":SENS:BAND:RES:AUTO", ":SENS:BAND:VID", ":SENS:BAND:VID:AUTO",
             ":SENS:DET:TRAC", ":SENS:AVER:TYPE", ":INIT:CONT"]
```

Then add the module to the import line in `analyzers/__init__.py`. If the trace comes back in
another format (binary block, different separator) override `parse_trace()`; if the instrument
needs a different sweep/wait sequence, override `sweep()`. Instruments without SCPI over a
socket (USB-TMC, VISA, serial) can subclass `SpectrumAnalyzer` directly, or replace
`SocketTransport` with another class offering `open/close/write/readline/query`.

Points to check when bringing up a new analyzer:

- The detector must average power (RMS/"average" with power scale), not log/video average,
  otherwise noise reads ~2.5 dB low.
- Is the RBW filter Gaussian (noise bandwidth 1.065 × RBW)? If not, adjust `ENBW_GAUSSIAN`.
- Number of trace points: a span/RBW ratio of ~600 suits 751 points; with more points a
  higher ratio is possible (Advanced → Span / RBW).
- On disconnect the driver sends the `local` command (Siglent: `:SYST:LOC`) so the front panel
  works again; set it to the instrument's go-to-local command, or `None` if there is none.
- Compare a clean generator against the simulated / known results, and check that
  `restore_state` leaves the instrument as it was (`save_state()` before == after).

## Limitations / ideas

- Only CW signals. Future: signal tracking for unstable signals (now: re-centre per segment).
- The analyzer's own phase noise and LO spurs set the floor; readings near it are upper bounds.
- Not yet: limit lines/masks, cross-correlation, AM noise.
