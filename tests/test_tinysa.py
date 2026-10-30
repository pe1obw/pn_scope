import math
import struct

import numpy as np
import pytest

from pnscope import analysis as A
from pnscope.analyzers.base import AnalyzerError
from pnscope.analyzers.tinysa import TinySAUltra
from pnscope.model import Measurement, Segment


class FakeShell:
    def __init__(self, dbm):
        self.dbm = dbm
        self.sent = []

    def raw(self, cmd, timeout=None, cancel=None):
        self.sent.append(cmd)
        n = int(cmd.split()[3])
        vals = np.round((np.resize(self.dbm, n) + 174) * 32).astype(int)
        return b"{" + b"".join(b"x" + struct.pack("<h", v) for v in vals) + b"}"

    def command(self, cmd, timeout=None):
        self.sent.append(cmd)
        return []


def test_scanraw_decoding_and_points():
    sa = TinySAUltra()
    sa.sh = FakeShell(np.array([-10.0, -80.5, -120.25]))
    sa.configure(100e6, 20e3, 200.0, 600.0)
    assert sa.points == 125 and sa.sh.sent[-1] == "rbw 0.2"
    t = sa.sweep()
    assert len(t) == 125 and np.allclose(t[:3], [-10.0, -80.5, -120.25])
    assert sa.sh.sent[-1] == "scanraw 99990000 100010000 125"


def test_sweep_below_min_freq_is_refused():
    sa = TinySAUltra()
    sa.sh = FakeShell(np.array([-100.0]))
    sa.configure(50e3, 200e3, 1e3, 3e3)
    with pytest.raises(AnalyzerError):
        sa.sweep()


def test_per_segment_enbw_is_used():
    off = np.linspace(-1e3, 1e3, 201)
    seg = Segment(2e3, 200.0, 600.0, 1, 100, 1e3, off, np.full(off.shape, -50.0), 1e8,
                  enbw=TinySAUltra().enbw_ratio(200.0))
    c = A.stitch(Measurement("t", 1e8, 0.0, [seg]))
    assert np.allclose(c.usb, -50 - 10 * math.log10(1.223 * 200))
    seg.enbw = None  # older files: Gaussian
    assert np.allclose(A.stitch(Measurement("t", 1e8, 0.0, [seg])).usb, -50 - 10 * math.log10(1.065 * 200))
