"""Measurement data model, file format and import of the legacy script output."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

FILE_FORMAT = "pnscope"
FILE_VERSION = 1
FILE_SUFFIX = ".pnscope"


@dataclass
class Segment:
    """One analyzer sweep setting: raw averaged trace around the carrier."""
    span: float          # Hz
    rbw: float           # Hz (3 dB bandwidth as set on the analyzer)
    vbw: float           # Hz
    averages: int
    f_lo: float          # offset range this segment contributes to the stitched curve
    f_hi: float
    offsets: np.ndarray  # Hz, relative to the carrier, ascending (-span/2 .. +span/2)
    power: np.ndarray    # dBm, power-averaged
    carrier_freq: float = 0.0  # carrier frequency used as center for this segment
    sweep_time: float = 0.0    # s, one sweep
    enbw: float | None = None  # noise bandwidth / RBW of the analyzer at this RBW
    analyzer_state: dict | None = None  # driver settings in effect (e.g. receiver gain)

    def to_dict(self):
        d = asdict(self)
        d["offsets"] = [round(float(x), 4) for x in self.offsets]
        d["power"] = [round(float(x), 4) for x in self.power]
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        d["offsets"] = np.asarray(d["offsets"], float)
        d["power"] = np.asarray(d["power"], float)
        return cls(**d)


@dataclass
class Measurement:
    name: str
    carrier_freq: float   # Hz
    carrier_power: float  # dBm
    segments: list[Segment] = field(default_factory=list)
    timestamp: str = ""
    instrument: str = ""
    notes: str = ""
    settings: dict = field(default_factory=dict)
    path: str | None = None  # where it was loaded from / saved to (not stored)

    def to_dict(self):
        return {
            "format": FILE_FORMAT, "version": FILE_VERSION,
            "name": self.name, "carrier_freq": self.carrier_freq,
            "carrier_power": self.carrier_power, "timestamp": self.timestamp,
            "instrument": self.instrument, "notes": self.notes,
            "settings": self.settings,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, d, path=None):
        if d.get("format") != FILE_FORMAT:
            raise ValueError("not a PNScope measurement file")
        if d.get("version", 1) > FILE_VERSION:
            raise ValueError(f"file version {d['version']} is newer than this program supports")
        return cls(name=d["name"], carrier_freq=d["carrier_freq"],
                   carrier_power=d["carrier_power"], timestamp=d.get("timestamp", ""),
                   instrument=d.get("instrument", ""), notes=d.get("notes", ""),
                   settings=d.get("settings", {}),
                   segments=[Segment.from_dict(s) for s in d["segments"]], path=path)

    def save(self, path):
        path = str(path)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f)
        os.replace(tmp, path)
        self.path = path


def load(path) -> Measurement:
    """Load a .pnscope file, or a legacy '<label>_meta.json' from the example scripts."""
    path = str(path)
    with open(path) as f:
        d = json.load(f)
    if d.get("format") == FILE_FORMAT:
        return Measurement.from_dict(d, path)
    if "segs" in d and "fc" in d:
        return _load_legacy(path, d)
    raise ValueError(f"{path}: unrecognised file")


def _load_legacy(path, meta):
    """The scripts in example/ wrote <label>_meta.json + <label>_segN.csv.
    Segment N covered offsets from the previous segment's span/2 up to its own span/2
    (the first from 10*RBW)."""
    base = Path(path).parent
    name = Path(path).name
    name = name[:-len("_meta.json")] if name.endswith("_meta.json") else Path(path).stem
    segs, lo = [], None
    for s in meta["segs"]:
        p = Path(s["file"])
        if not p.is_absolute():
            p = base / p.name
        nav = 0
        with open(p) as f:
            head = f.readline()
        if head.startswith("#"):
            for tok in head[1:].split():
                k, _, v = tok.partition("=")
                if k == "nav":
                    nav = int(float(v))
        d = np.loadtxt(p, delimiter=",", comments="#")
        span, rbw = float(s["span"]), float(s["rbw"])
        f_lo = 10 * rbw if lo is None else lo
        segs.append(Segment(span=span, rbw=rbw, vbw=3 * rbw, averages=nav, f_lo=f_lo,
                            f_hi=span / 2, offsets=d[:, 0], power=d[:, 1],
                            carrier_freq=meta["fc"]))
        lo = span / 2
    ts = ""
    try:
        import datetime
        ts = datetime.datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
    except OSError:
        pass
    return Measurement(name=name, carrier_freq=meta["fc"], carrier_power=meta["pc"],
                       segments=segs, timestamp=ts, instrument="(imported from legacy script)",
                       path=None)
