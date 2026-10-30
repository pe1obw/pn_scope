"""Exports: publication-quality figure (matplotlib), CSV data and a text report."""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field

import numpy as np

from . import analysis as A
from . import style
from .units import format_freq, format_si


@dataclass
class TraceView:
    """What is needed to draw/report one trace."""
    name: str
    color: str
    measurement: object
    processed: A.Processed
    is_floor: bool = False
    integrated: A.Integrated | None = None
    is_background: bool = False


@dataclass
class FigureSpec:
    traces: list[TraceView]
    title: str = ""
    xlim: tuple | None = None
    ylim: tuple | None = None
    region: tuple | None = None          # integration range
    markers: list = field(default_factory=list)  # [(label, offset)]
    show_raw: bool = False                # draw unsmoothed curve faintly behind
    footnote: str = ""


def figure(spec: FigureSpec, size=(10, 6)):
    import matplotlib
    matplotlib.use("Agg", force=False)
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter, LogLocator

    fig = Figure(figsize=size, facecolor=style.SURFACE)
    ax = fig.add_subplot(111)
    ax.set_facecolor(style.SURFACE)
    for t in spec.traces:
        p = t.processed
        if spec.show_raw or p.measured is not None:
            ax.semilogx(p.f, p.raw if spec.show_raw else p.measured, color=t.color, lw=0.6, alpha=0.3)
        label = t.name + (" (reference)" if t.is_floor else "")
        if t.integrated is not None and not t.is_floor:
            label += f" — {t.integrated.phase_deg:.3g}° rms, {format_si(t.integrated.jitter_s, 's', 3)}"
        ax.semilogx(p.f, p.display, color=style.FLOOR if t.is_floor else t.color,
                    lw=1.0 if (t.is_floor or t.is_background) else 1.4,
                    ls="--" if t.is_floor else ":" if t.is_background else "-", label=label)
    if spec.region:
        ax.axvspan(*spec.region, color=style.SERIES[0], alpha=0.08, lw=0)
    for label, f0 in spec.markers:
        ax.axvline(f0, color=style.MARKER, lw=0.8, ls=":")
        for t in spec.traces:
            v = A.value_at(t.processed.f, t.processed.display, f0)
            if math.isfinite(v):
                ax.plot([f0], [v], "o", ms=5, mfc=style.SURFACE,
                        mec=style.FLOOR if t.is_floor else t.color, mew=1.5)
                ax.annotate(f"{label}: {v:.1f}", (f0, v), xytext=(6, 4), textcoords="offset points",
                            fontsize=8, color=style.TEXT)
        ax.annotate(f"{label} {format_freq(f0)}", (f0, 1), xycoords=("data", "axes fraction"),
                    xytext=(3, -12), textcoords="offset points", fontsize=8, color=style.TEXT_2)
    ax.xaxis.set_major_locator(LogLocator(10))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: format_freq(v, 3)))
    ax.grid(True, which="major", color=style.GRID, lw=0.8)
    ax.grid(True, which="minor", color=style.GRID, lw=0.5, alpha=0.7)
    for s in ax.spines.values():
        s.set_color(style.GRID)
    ax.tick_params(colors=style.TEXT_2, labelsize=9)
    ax.set_xlabel("Offset frequency", color=style.TEXT_2)
    ax.set_ylabel("L(f) [dBc/Hz]", color=style.TEXT_2)
    if spec.xlim:
        ax.set_xlim(*spec.xlim)
    if spec.ylim:
        ax.set_ylim(*spec.ylim)
    if spec.title:
        ax.set_title(spec.title, color=style.TEXT, fontsize=12, loc="left")
    if len(spec.traces) > 1 or (spec.traces and spec.traces[0].integrated):
        leg = ax.legend(fontsize=8, frameon=True, loc="best")
        leg.get_frame().set_edgecolor(style.GRID)
        for txt in leg.get_texts():
            txt.set_color(style.TEXT)
    if spec.footnote:
        fig.text(0.01, 0.005, spec.footnote, fontsize=7, color=style.TEXT_2, va="bottom")
    fig.tight_layout(rect=(0, 0.02 if spec.footnote else 0, 1, 1))
    return fig


def save_figure(spec: FigureSpec, path: str, dpi=150):
    fig = figure(spec)
    fig.savefig(path, dpi=dpi, facecolor=fig.get_facecolor())


def save_csv(path: str, t: TraceView):
    """One trace: stitched L(f) for both sidebands plus the processed curve."""
    m, p = t.measurement, t.processed
    c = A.stitch(m)
    with open(path, "w", newline="") as fh:
        fh.write(f"# PNScope export: {m.name}\n# carrier {m.carrier_freq:.3f} Hz, {m.carrier_power:.2f} dBm\n")
        fh.write(f"# measured {m.timestamp} on {m.instrument}\n")
        w = csv.writer(fh)
        w.writerow(["offset_Hz", "L_upper_dBcHz", "L_lower_dBcHz", "L_avg_dBcHz",
                    "L_processed_dBcHz", "L_display_dBcHz", "rbw_Hz"])
        for row in zip(c.f, c.usb, c.lsb, c.avg, p.clean, p.display, c.rbw):
            w.writerow([f"{row[0]:.3f}"] + [("" if not np.isfinite(v) else f"{v:.3f}") for v in row[1:6]]
                       + [f"{row[6]:g}"])


def report(traces: list[TraceView], opt: A.ProcessOptions, region, spots, markers) -> str:
    """Markdown summary of everything shown in the result tables."""
    out = ["# Phase noise report", ""]
    out.append(f"Sideband: {opt.sideband}; spur removal: {'on' if opt.remove_spurs else 'off'}"
               f" (threshold {opt.spur_threshold:g} dB); floor subtraction: "
               f"{'on' if opt.subtract_floor else 'off'}; smoothing {opt.smoothing * 100:g} % of a decade "
               f"(display only).")
    out.append("")
    out.append("| Trace | Carrier | Power | Measured | Instrument |")
    out.append("|---|---|---|---|---|")
    for t in traces:
        m = t.measurement
        out.append(f"| {t.name} | {m.carrier_freq / 1e6:.6f} MHz | {m.carrier_power:.2f} dBm | "
                   f"{m.timestamp} | {m.instrument} |")
    if region:
        out += ["", f"## Integrated phase noise, {format_freq(region[0])} – {format_freq(region[1])}", "",
                "| Trace | φ rms | φ rms | Integrated | Jitter | Residual FM |",
                "|---|---|---|---|---|---|"]
        for t in traces:
            r = t.integrated
            if r:
                out.append(f"| {t.name} | {r.phase_deg:.4g}° | {r.phase_rad * 1e3:.4g} mrad | "
                           f"{r.dbc:.1f} dBc | {format_si(r.jitter_s, 's', 4)} | "
                           f"{format_si(r.fm_hz, 'Hz', 4)} |")
    if spots:
        out += ["", "## Spot noise [dBc/Hz]", "",
                "| Trace | " + " | ".join(format_freq(s) for s in spots) + " |",
                "|---|" + "---|" * len(spots)]
        for t in traces:
            vals = [A.spot(t.processed.f, t.processed.clean, s) for s in spots]
            out.append(f"| {t.name} | " + " | ".join("—" if math.isnan(v) else f"{v:.1f}" for v in vals) + " |")
    if markers:
        out += ["", "## Markers [dBc/Hz, displayed curve]", "",
                "| Marker | Offset | " + " | ".join(t.name for t in traces) + " |",
                "|---|---|" + "---|" * len(traces)]
        for label, f0 in markers:
            vals = [A.value_at(t.processed.f, t.processed.display, f0) for t in traces]
            out.append(f"| {label} | {format_freq(f0)} | "
                       + " | ".join("—" if math.isnan(v) else f"{v:.1f}" for v in vals) + " |")
    spur_rows = [(t.name, s) for t in traces if not t.is_background for s in t.processed.spurs]
    if spur_rows:
        out += ["", "## Spurs", "", "| Trace | Offset | Level | Above noise | Without carrier |",
                "|---|---|---|---|---|"]
        for name, s in spur_rows:
            origin = {True: "yes (external)", False: "no (DUT)", None: "not measured"}[s.background]
            out.append(f"| {name} | {format_freq(s.offset, 5)} | {s.dbc:.1f} dBc | {s.excess_db:.1f} dB "
                       f"| {origin} |")
    out += ["", "Method: spectrum-analyzer method, RMS detector, power-averaged sweeps, "
            "L(f) = P − Pc − 10·log10(1.065·RBW). Integration is DSB: φ = √(2∫L(f)df).", ""]
    return "\n".join(out)
