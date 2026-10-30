"""Parsing and formatting of frequencies and times with SI prefixes."""
import math
import re

_PREFIX = {"": 1.0, "m": 1e-3, "u": 1e-6, "µ": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15,
           "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
_RE = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Zµ]?)\s*(?:hz|Hz|HZ|s)?\s*$")


def parse_si(text: str) -> float:
    """'1.5G', '10 kHz', '1580.42 MHz', '3e3' -> float. Raises ValueError."""
    m = _RE.match(text)
    if not m:
        raise ValueError(f"cannot parse {text!r}")
    num, pre = m.groups()
    # 'm' as a frequency prefix is almost always meant as MHz
    if pre == "m":
        pre = "M"
    if pre not in _PREFIX:
        raise ValueError(f"unknown prefix in {text!r}")
    return float(num) * _PREFIX[pre]


def format_si(value: float, unit: str = "Hz", digits: int = 4) -> str:
    """1580420014 -> '1.580420 GHz' (digits = significant digits, trailing zeros trimmed)."""
    if value is None or not math.isfinite(value):
        return "—"
    if value == 0:
        return f"0 {unit}"
    exp = int(math.floor(math.log10(abs(value)) / 3) * 3)
    exp = max(-15, min(12, exp))
    pre = {-15: "f", -12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G", 12: "T"}[exp]
    scaled = value / 10 ** exp
    s = f"{scaled:.{max(digits - 1 - int(math.floor(math.log10(abs(scaled)))), 0)}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return f"{s} {pre}{unit}"


def format_freq(hz: float, digits: int = 4) -> str:
    return format_si(hz, "Hz", digits)
