"""Interactive L(f) plot: log-frequency axis, markers, integration band, hover readout.
pyqtgraph's log mode works in log10 coordinates for items; this class converts."""
from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor

from .. import analysis as A
from .. import style
from ..units import format_freq


class FreqAxis(pg.AxisItem):
    def logTickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            r = round(v)
            out.append(format_freq(10 ** r, 3) if abs(v - r) < 1e-6 else "")
        return out


class Marker:
    def __init__(self, plot: "PNPlot", label: str, freq: float):
        self.plot, self.label = plot, label
        self.line = pg.InfiniteLine(
            pos=math.log10(freq), angle=90, movable=True,
            pen=pg.mkPen(style.MARKER, width=1, style=Qt.DashLine),
            hoverPen=pg.mkPen(style.SERIES[0], width=2),
            label=label, labelOpts=dict(position=0.96, color=style.TEXT, fill=(252, 252, 251, 220),
                                        movable=False, anchors=[(0, 0.5), (0, 0.5)]))
        self.dots = pg.ScatterPlotItem(size=9, pen=pg.mkPen(width=2), brush=pg.mkBrush(style.SURFACE))
        self.dots.setZValue(20)
        self.line.setZValue(15)
        plot.addItem(self.line)
        plot.addItem(self.dots)

    @property
    def freq(self):
        return 10 ** self.line.value()

    def set_freq(self, f):
        self.line.setValue(math.log10(f))

    def remove(self):
        self.plot.removeItem(self.line)
        self.plot.removeItem(self.dots)


class PNPlot(pg.PlotWidget):
    markers_changed = Signal()
    region_changed = Signal(float, float)
    hover = Signal(float, float)  # offset Hz, dBc/Hz at cursor

    def __init__(self, parent=None):
        super().__init__(parent, background=style.SURFACE, axisItems={"bottom": FreqAxis("bottom")})
        pi = self.getPlotItem()
        pi.setLogMode(x=True, y=False)
        pi.showGrid(x=True, y=True, alpha=0.25)
        for ax in ("left", "bottom"):
            a = pi.getAxis(ax)
            a.setPen(pg.mkPen(style.GRID))
            a.setTextPen(pg.mkPen(style.TEXT_2))
        pi.setLabel("bottom", "Offset frequency")
        pi.setLabel("left", "L(f)", units=None)
        pi.getAxis("left").setLabel("L(f) [dBc/Hz]")
        self.legend = pi.addLegend(offset=(-10, 10), labelTextColor=style.TEXT,
                                   brush=pg.mkBrush(252, 252, 251, 230), pen=pg.mkPen(style.GRID))
        self.curves: dict[int, tuple] = {}  # key -> (display item, raw item)
        self.markers: list[Marker] = []
        self._series: list[tuple[str, str, A.Processed]] = []  # for marker dots / hover
        self.region = pg.LinearRegionItem(values=(2, 6), orientation="vertical",
                                          brush=pg.mkBrush(*style.REGION),
                                          hoverBrush=pg.mkBrush(*style.REGION[:3], 50),
                                          pen=pg.mkPen(style.SERIES[0], width=1))
        self.region.setZValue(-10)
        self.region.sigRegionChangeFinished.connect(self._region_moved)
        self.region.sigRegionChanged.connect(self._region_moving)
        self.addItem(self.region)
        self._region_lock = False
        self.hover_text = pg.TextItem(color=style.TEXT, anchor=(0, 1), fill=(252, 252, 251, 200))
        self.hover_text.setZValue(30)
        self.addItem(self.hover_text, ignoreBounds=True)
        self.hover_text.hide()
        self.scene().sigMouseMoved.connect(self._mouse_moved)

    # --- curves ---------------------------------------------------------------------
    def set_traces(self, series, show_raw=False):
        """series: list of (key, name, color, Processed, is_floor, legend_label)."""
        for disp, raw in self.curves.values():
            self.removeItem(disp)
            if raw is not None:
                self.removeItem(raw)
        self.curves.clear()
        self.legend.clear()
        self._series = []
        for key, name, color, p, is_floor, legend in series:
            ok = np.isfinite(p.display)
            col = style.FLOOR if is_floor else color
            pen = pg.mkPen(col, width=1.5 if is_floor else 2, style=Qt.DashLine if is_floor else Qt.SolidLine)
            raw_item = None
            if show_raw or p.measured is not None:
                c = QColor(col)
                c.setAlpha(70)
                raw_item = self.plot(p.f, p.raw if show_raw else p.measured, pen=pg.mkPen(c, width=1))
            disp = self.plot(p.f, np.where(ok, p.display, np.nan), pen=pen, name=legend,
                             connect="finite")
            self.curves[key] = (disp, raw_item)
            self._series.append((name, col, p))
        self.legend.setVisible(len(series) > 0)
        self.update_marker_dots()

    # --- markers --------------------------------------------------------------------
    def add_marker(self, freq=None):
        if freq is None:
            (x0, x1), _ = self.viewRange()
            freq = 10 ** ((x0 + x1) / 2)
        used = {m.label for m in self.markers}
        n = 1
        while f"M{n}" in used:
            n += 1
        m = Marker(self, f"M{n}", freq)
        m.line.sigPositionChanged.connect(lambda *_: self._marker_moved(m))
        self.markers.append(m)
        self.markers.sort(key=lambda mk: int(mk.label[1:]))
        self._marker_moved(m)
        return m

    def remove_marker(self, m: Marker):
        m.remove()
        self.markers.remove(m)
        self.markers_changed.emit()

    def clear_markers(self):
        for m in list(self.markers):
            m.remove()
        self.markers.clear()
        self.markers_changed.emit()

    def _marker_moved(self, m):
        m.line.label.setFormat(f"{m.label} {format_freq(m.freq)}")
        self._update_dots(m)
        self.markers_changed.emit()

    def _update_dots(self, m):
        pts = []
        for name, col, p in self._series:
            v = A.value_at(p.f, p.display, m.freq)
            if math.isfinite(v):
                pts.append(dict(pos=(m.line.value(), v), pen=pg.mkPen(col, width=2),
                                brush=pg.mkBrush(style.SURFACE)))
        m.dots.setData(pts)

    def update_marker_dots(self):
        for m in self.markers:
            self._update_dots(m)

    def mouseDoubleClickEvent(self, ev):
        pos = self.getPlotItem().vb.mapSceneToView(self.mapToScene(ev.position().toPoint()))
        self.add_marker(10 ** pos.x())
        ev.accept()

    # --- integration band -------------------------------------------------------------
    def set_region(self, f1, f2, visible=True):
        self._region_lock = True
        self.region.setRegion((math.log10(f1), math.log10(f2)))
        self.region.setVisible(visible)
        self._region_lock = False

    def _region_moving(self):
        pass

    def _region_moved(self):
        if self._region_lock:
            return
        a, b = self.region.getRegion()
        self.region_changed.emit(10 ** a, 10 ** b)

    # --- hover readout ---------------------------------------------------------------------
    def _mouse_moved(self, pos):
        vb = self.getPlotItem().vb
        if not self.sceneBoundingRect().contains(pos) or not vb.sceneBoundingRect().contains(pos):
            self.hover_text.hide()
            return
        p = vb.mapSceneToView(pos)
        f = 10 ** p.x()
        self.hover.emit(f, p.y())
        lines = [format_freq(f)]
        for name, col, pr in self._series:
            v = A.value_at(pr.f, pr.display, f)
            if math.isfinite(v):
                lines.append(f"<span style='color:{col}'>■</span> {name}: {v:.1f}")
        self.hover_text.setHtml("<div style='font-size:9pt'>" + "<br>".join(lines) + "</div>")
        (x0, x1), (y0, y1) = vb.viewRange()
        self.hover_text.setAnchor((1 if p.x() > (x0 + x1) / 2 else 0, 0 if p.y() > (y0 + y1) / 2 else 1))
        self.hover_text.setPos(p.x(), p.y())
        self.hover_text.show()

    def leaveEvent(self, ev):
        self.hover_text.hide()
        super().leaveEvent(ev)
