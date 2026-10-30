"""Main window: plot in the centre, measurement dock left, traces/analysis right,
result tables below."""
from __future__ import annotations

import dataclasses
import math
import os
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QTableWidget,
                               QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from .. import __version__
from .. import analysis as A
from .. import export as X
from .. import style
from ..analyzers import create
from ..measure import PhaseNoiseRun, background_settings, find_carrier, plan_from, states_from
from ..model import FILE_SUFFIX, Measurement, load
from ..plan import PlanSettings
from ..units import format_freq, format_si, parse_si
from .measure_panel import FreqEdit, MeasurePanel
from .plot import PNPlot
from .workers import Task

FILE_FILTER = f"PNScope measurement (*{FILE_SUFFIX});;Legacy script output (*_meta.json);;All files (*)"


class Trace:
    _next_key = 0

    def __init__(self, m: Measurement, color: str):
        self.m = m
        self.color = color
        self.visible = True
        self.dirty = m.path is None
        self.live = False   # measurement in progress
        self.curve = A.stitch(m)
        self.processed: A.Processed | None = None
        self.integrated: A.Integrated | None = None
        self.bkg_of: int | None = None  # key of the measurement this is the background of
        Trace._next_key += 1
        self.key = Trace._next_key

    @property
    def is_background(self):
        return "background_for" in self.m.settings

    def belongs_to(self, other: "Trace") -> bool:
        """Is this the background measurement of `other`?"""
        b = self.m.settings.get("background_for")
        return bool(b) and b.get("timestamp") == other.m.timestamp and \
            abs(b.get("carrier_freq", 0) - other.m.carrier_freq) < 1.0

    def update_measurement(self, m):
        self.m = m
        self.curve = A.stitch(m)


def swatch(color, dashed=False):
    pm = QPixmap(14, 14)
    pm.fill(QColor(color))
    return QIcon(pm)


def table(headers, stretch_last=False):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(stretch_last)
    t.setAlternatingRowColors(True)
    return t


def cell(text, align_right=True, color=None, icon=None):
    it = QTableWidgetItem(text)
    it.setTextAlignment((Qt.AlignRight if align_right else Qt.AlignLeft) | Qt.AlignVCenter)
    if icon is not None:
        it.setIcon(icon)
    if color:
        it.setForeground(QColor(color))
    return it


def fmt(v, spec=".1f"):
    return "—" if v is None or not math.isfinite(v) else format(v, spec)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"PNScope {__version__}")
        self.resize(1500, 950)
        self.traces: list[Trace] = []
        self.floor_key: int | None = None
        self.task: Task | None = None
        self.live_trace: Trace | None = None

        self.plot = PNPlot()
        self.setCentralWidget(self.plot)

        self.mpanel = MeasurePanel()
        sc = QScrollArea()
        sc.setWidget(self.mpanel)
        sc.setWidgetResizable(True)
        sc.setMinimumWidth(330)
        d = QDockWidget("Measure", self)
        d.setObjectName("dock_measure")
        d.setWidget(sc)
        self.addDockWidget(Qt.LeftDockWidgetArea, d)
        self.dock_measure = d

        self._build_right_dock()
        self._build_results_dock()
        self._build_menus()
        self.statusBar().showMessage("Ready")

        self.mpanel.start_requested.connect(self.start_measurement)
        self.mpanel.background_requested.connect(self.start_background)
        self.mpanel.background_enabled.connect(lambda on: (self.bkg_action.setVisible(on),
                                                           self.update_tables()))
        self.mpanel.stop_requested.connect(self.stop_task)
        self.mpanel.test_requested.connect(self.test_connection)
        self.mpanel.find_requested.connect(self.find_carrier)
        self.mpanel.get_center_requested.connect(self.get_center)
        self.plot.region_changed.connect(self._region_from_plot)
        self.plot.markers_changed.connect(self._schedule_markers)
        self.plot.hover.connect(lambda f, y: self.statusBar().showMessage(
            f"{format_freq(f)}   {y:.1f} dBc/Hz (cursor)"))

        self._refresh_timer = QTimer(self, singleShot=True, interval=30)
        self._refresh_timer.timeout.connect(self.refresh)
        self._marker_timer = QTimer(self, singleShot=True, interval=30)
        self._marker_timer.timeout.connect(self.update_marker_table)

        s = QSettings()
        if s.value("window/geometry"):
            self.restoreGeometry(s.value("window/geometry"))
        if s.value("window/state"):
            self.restoreState(s.value("window/state"))
        self._sync_region_fields()
        self.refresh()

    # ------------------------------------------------------------------------------ UI
    def _build_right_dock(self):
        w = QWidget()
        v = QVBoxLayout(w)
        s = QSettings()

        g = QGroupBox("Traces")
        gv = QVBoxLayout(g)
        self.trace_table = QTableWidget(0, 4)
        self.trace_table.setHorizontalHeaderLabels(["Name", "Carrier", "Ref.", "Power"])
        self.trace_table.verticalHeader().hide()
        self.trace_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.trace_table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self.trace_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.trace_table.setToolTip("Tick to show; double-click the name to rename.\n"
                                    "'Ref.' marks the analyzer-floor reference trace.")
        self.trace_table.itemChanged.connect(self._trace_item_changed)
        self.trace_table.itemSelectionChanged.connect(self.update_info)
        self.trace_table.setMinimumHeight(150)
        gv.addWidget(self.trace_table)
        row = QHBoxLayout()
        for text, slot, tip in (("Open…", self.open_files, "Open measurement files"),
                                ("Save…", self.save_selected, "Save the selected trace"),
                                ("Remove", self.remove_selected, "Remove the selected trace")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        gv.addLayout(row)
        v.addWidget(g)

        g = QGroupBox("Display")
        f = QFormLayout(g)
        self.sideband = QComboBox()
        self.sideband.addItems(A.SIDEBANDS)
        self.sideband.setCurrentText(s.value("disp/sideband", "Average"))
        self.sideband.setToolTip("Average: power mean of both sidebands. Upper/Lower: one sideband only.")
        f.addRow("Sideband", self.sideband)
        self.smoothing = QDoubleSpinBox(minimum=0, maximum=50, decimals=1, singleStep=1,
                                        value=float(s.value("disp/smooth", 2)), suffix=" % of decade")
        self.smoothing.setToolTip("Moving average on the log axis (display and markers only;\n"
                                  "integration and spot noise use unsmoothed data)")
        f.addRow("Smoothing", self.smoothing)
        self.show_raw = QCheckBox("Show unsmoothed data faintly")
        self.show_raw.setChecked(s.value("disp/raw", False, type=bool))
        f.addRow(self.show_raw)
        self.title = QLineEdit(s.value("disp/title", ""))
        self.title.setPlaceholderText("plot title for export")
        f.addRow("Title", self.title)
        v.addWidget(g)

        g = QGroupBox("Corrections")
        f = QFormLayout(g)
        self.spur_remove = QCheckBox("Remove spurs, threshold")
        self.spur_remove.setChecked(s.value("disp/spurs", False, type=bool))
        self.spur_thr = QDoubleSpinBox(minimum=3, maximum=40, decimals=1,
                                       value=float(s.value("disp/spur_thr", 10)), suffix=" dB")
        self.spur_thr.setToolTip("A point counts as a spur when it is this far above the local median")
        row = QHBoxLayout()
        row.addWidget(self.spur_remove)
        row.addWidget(self.spur_thr)
        f.addRow(row)
        self.floor_sub = QCheckBox("Subtract reference (analyzer floor)")
        self.floor_sub.setChecked(s.value("disp/floor", False, type=bool))
        self.floor_sub.setToolTip("Mark a trace as 'Ref.' (e.g. a clean generator measured the same way).\n"
                                  "Points less than the margin above it are treated as zero noise.")
        f.addRow(self.floor_sub)
        self.floor_margin = QDoubleSpinBox(minimum=0, maximum=10, decimals=1,
                                           value=float(s.value("disp/floor_margin", 1)), suffix=" dB")
        f.addRow("Margin", self.floor_margin)
        v.addWidget(g)

        g = QGroupBox("Integration")
        f = QFormLayout(g)
        self.int_show = QCheckBox("Show band on plot (drag to change)")
        self.int_show.setChecked(s.value("int/show", True, type=bool))
        f.addRow(self.int_show)
        self.int_f1 = FreqEdit(float(s.value("int/f1", 1e3)))
        self.int_f2 = FreqEdit(float(s.value("int/f2", 1e6)))
        row = QHBoxLayout()
        row.addWidget(self.int_f1)
        row.addWidget(QLabel("to"))
        row.addWidget(self.int_f2)
        f.addRow("Range", row)
        row = QHBoxLayout()
        for label in ("Full", "1k–1M", "10k–1M", "12k–20M"):
            b = QToolButton(text=label)
            b.clicked.connect(lambda _=False, l=label: self._preset_range(l))
            row.addWidget(b)
        row.addStretch()
        f.addRow(row)
        v.addWidget(g)

        g = QGroupBox("Markers")
        row = QHBoxLayout(g)
        for text, slot, tip in (("Add", lambda: self.plot.add_marker(), "Add a marker (or double-click the plot)"),
                                ("To peak", self.marker_to_peak, "Move the last marker to the highest point "
                                                                 "of the selected trace in view"),
                                ("Clear", self.plot.clear_markers, "Remove all markers")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        self.delta = QCheckBox("Δ to M1")
        self.delta.toggled.connect(self.update_marker_table)
        row.addWidget(self.delta)
        v.addWidget(g)
        v.addStretch(1)

        for wdg in (self.sideband,):
            wdg.currentTextChanged.connect(self.schedule_refresh)
        for wdg in (self.smoothing, self.spur_thr, self.floor_margin):
            wdg.valueChanged.connect(self.schedule_refresh)
        for wdg in (self.show_raw, self.spur_remove, self.floor_sub, self.int_show):
            wdg.toggled.connect(self.schedule_refresh)
        for wdg in (self.int_f1, self.int_f2):
            wdg.editingFinished.connect(self._sync_region_fields)

        sc = QScrollArea()
        sc.setWidget(w)
        sc.setWidgetResizable(True)
        sc.setMinimumWidth(320)
        d = QDockWidget("Traces && analysis", self)
        d.setObjectName("dock_analysis")
        d.setWidget(sc)
        self.addDockWidget(Qt.RightDockWidgetArea, d)
        self.dock_analysis = d

    def _build_results_dock(self):
        self.tabs = QTabWidget()
        self.int_table = table(["Trace", "Range", "φ rms [°]", "φ rms [mrad]", "Integrated [dBc]",
                                "Jitter", "Residual FM"])
        self.tabs.addTab(self.int_table, "Integrated noise")

        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Offsets:"))
        self.spot_edit = QLineEdit(QSettings().value("spot/offsets", "10, 100, 1k, 10k, 100k, 1M, 10M"))
        self.spot_edit.setToolTip("Comma separated, SI prefixes allowed")
        self.spot_edit.editingFinished.connect(self.update_tables)
        row.addWidget(self.spot_edit, 1)
        v.addLayout(row)
        self.spot_table = table(["Trace"])
        v.addWidget(self.spot_table)
        self.tabs.addTab(w, "Spot noise")

        self.marker_table = table(["Marker", "Offset"])
        self.marker_table.setToolTip("Double-click a row to delete that marker. Drag markers on the plot.")
        self.marker_table.cellDoubleClicked.connect(self._marker_table_clicked)
        self.tabs.addTab(self.marker_table, "Markers")

        self.spur_table = table(["Trace", "Offset", "Level [dBc]", "Above noise [dB]",
                                 "Without carrier"])
        self.spur_table.setToolTip("Double-click to put a marker on the spur.\n'Without carrier': "
                                   "also present in the background measurement (DUT off), so it "
                                   "comes from the surroundings or the analyzer, not from the DUT.")
        self.spur_table.cellDoubleClicked.connect(self._spur_clicked)
        self.tabs.addTab(self.spur_table, "Spurs")

        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        self.info_label = QLabel("Select a trace")
        self.info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.info_label.setWordWrap(True)
        v.addWidget(self.info_label)
        self.seg_table = table(["Offsets", "Span", "RBW", "VBW", "Avg", "Sweep", "Centre"])
        v.addWidget(self.seg_table)
        self.tabs.addTab(w, "Info")

        self.seg_table.horizontalHeader().setStretchLastSection(True)
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(5000)
        self.tabs.addTab(self.log, "Log")

        d = QDockWidget("Results", self)
        d.setObjectName("dock_results")
        d.setWidget(self.tabs)
        self.addDockWidget(Qt.BottomDockWidgetArea, d)
        self.dock_results = d

    def _build_menus(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")

        def act(menu, text, slot, shortcut=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(shortcut)
            menu.addAction(a)
            return a

        act(m, "&Open…", self.open_files, QKeySequence.Open)
        act(m, "&Save selected trace…", self.save_selected, QKeySequence.Save)
        act(m, "Save &all unsaved…", self.save_all)
        m.addSeparator()
        act(m, "Export &plot (PNG/SVG/PDF)…", self.export_plot, "Ctrl+E")
        act(m, "Export selected trace as &CSV…", self.export_csv)
        act(m, "Export &report (Markdown)…", self.export_report)
        act(m, "&Copy plot to clipboard", self.copy_plot, "Ctrl+Shift+C")
        m.addSeparator()
        act(m, "&Quit", self.close, QKeySequence.Quit)

        m = mb.addMenu("&Measure")
        act(m, "&Start", self.start_measurement, "F5")
        act(m, "S&top", self.stop_task, "Esc")
        act(m, "&Find carrier", self.find_carrier)
        self.bkg_action = act(m, "&Background (DUT off) for selected trace", self.start_background)
        self.bkg_action.setVisible(self.mpanel.bkg_enable.isChecked())

        m = mb.addMenu("&View")
        act(m, "&Auto-scale", lambda: self.plot.getPlotItem().enableAutoRange(), "Ctrl+0")
        m.addSeparator()
        for d in (self.dock_measure, self.dock_analysis, self.dock_results):
            m.addAction(d.toggleViewAction())

        m = mb.addMenu("&Help")
        act(m, "&About", self.about)

    # --------------------------------------------------------------------------- traces
    def _next_color(self):
        used = {t.color for t in self.traces}
        for c in style.SERIES:
            if c not in used:
                return c
        return style.SERIES[len(self.traces) % len(style.SERIES)]

    def add_trace(self, m: Measurement) -> Trace:
        t = Trace(m, self._next_color())
        self.traces.append(t)
        self._link_backgrounds()
        self.rebuild_trace_table()
        self.schedule_refresh()
        return t

    def _link_backgrounds(self):
        """A background trace takes the colour of the measurement it belongs to."""
        for b in self.traces:
            if not b.is_background:
                continue
            b.bkg_of = None
            for t in self.traces:
                if t is not b and not t.is_background and b.belongs_to(t):
                    b.bkg_of, b.color = t.key, t.color
                    break

    def background_of(self, t: Trace) -> Trace | None:
        return next((b for b in self.traces if b.bkg_of == t.key), None)

    def selected_trace(self) -> Trace | None:
        rows = self.trace_table.selectionModel().selectedRows()
        if rows and rows[0].row() < len(self.traces):
            return self.traces[rows[0].row()]
        return self.traces[-1] if len(self.traces) == 1 else None

    def rebuild_trace_table(self):
        sel = self.selected_trace()
        tb = self.trace_table
        tb.blockSignals(True)
        tb.setRowCount(len(self.traces))
        for r, t in enumerate(self.traces):
            name = t.m.name + (" *" if t.dirty else "") + (" (measuring…)" if t.live else "")
            it = QTableWidgetItem(name)
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
            it.setCheckState(Qt.Checked if t.visible else Qt.Unchecked)
            it.setIcon(swatch(style.FLOOR if t.key == self.floor_key else t.color))
            it.setData(Qt.UserRole, t.key)
            it.setToolTip(t.m.path or "not saved")
            tb.setItem(r, 0, it)
            tb.setItem(r, 1, cell(f"{t.m.carrier_freq / 1e6:.4f} MHz"))
            ref = QTableWidgetItem()
            ref.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            ref.setCheckState(Qt.Checked if t.key == self.floor_key else Qt.Unchecked)
            tb.setItem(r, 2, ref)
            tb.setItem(r, 3, cell(f"{t.m.carrier_power:.1f} dBm"))
        if sel in self.traces:
            tb.selectRow(self.traces.index(sel))
        tb.blockSignals(False)

    def _trace_item_changed(self, it: QTableWidgetItem):
        r = it.row()
        if r >= len(self.traces):
            return
        t = self.traces[r]
        if it.column() == 0:
            t.visible = it.checkState() == Qt.Checked
            text = it.text().replace(" (measuring…)", "")
            if text.endswith(" *"):
                text = text[:-2]
            if text.strip() and text != t.m.name:
                t.m.name = text.strip()
                t.dirty = True
                QTimer.singleShot(0, self.rebuild_trace_table)
        elif it.column() == 2:
            on = it.checkState() == Qt.Checked
            self.floor_key = t.key if on else (None if self.floor_key == t.key else self.floor_key)
            if on and not self.floor_sub.isChecked():
                self.floor_sub.setChecked(True)
            QTimer.singleShot(0, self.rebuild_trace_table)
        self.schedule_refresh()

    def remove_selected(self):
        t = self.selected_trace()
        if not t or t.live:
            return
        if t.dirty and QMessageBox.question(self, "Remove trace",
                                            f"'{t.m.name}' is not saved. Remove anyway?") != QMessageBox.Yes:
            return
        self.traces.remove(t)
        if self.floor_key == t.key:
            self.floor_key = None
        self._link_backgrounds()
        self.rebuild_trace_table()
        self.schedule_refresh()

    # ------------------------------------------------------------------------- analysis
    def options(self) -> A.ProcessOptions:
        return A.ProcessOptions(sideband=self.sideband.currentText(),
                                smoothing=self.smoothing.value() / 100,
                                remove_spurs=self.spur_remove.isChecked(),
                                spur_threshold=self.spur_thr.value(),
                                subtract_floor=self.floor_sub.isChecked() and self.floor_key is not None,
                                floor_margin=self.floor_margin.value())

    def floor_trace(self):
        return next((t for t in self.traces if t.key == self.floor_key), None)

    def int_range(self):
        f1, f2 = self.int_f1.value(), self.int_f2.value()
        if f1 and f2 and f2 > f1:
            return f1, f2
        return None

    def schedule_refresh(self, *_):
        self._refresh_timer.start()

    def refresh(self):
        opt = self.options()
        floor = self.floor_trace()
        rng = self.int_range()
        series = []
        for t in self.traces:
            if len(t.curve.f) == 0:
                t.processed = None
                continue
            is_floor = t is floor
            plain = is_floor or t.is_background  # no floor subtraction for these
            o = dataclasses.replace(opt, subtract_floor=opt.subtract_floor and not plain)
            t.processed = A.process(t.curve, o, floor.curve if (floor and not plain) else None)
            t.integrated = (A.integrate(t.processed.f, t.processed.clean, *rng, t.m.carrier_freq)
                            if rng and not t.is_background else None)
            bkg = self.background_of(t)
            if bkg is not None and len(bkg.curve.f):
                A.classify_spurs(t.processed.spurs, t.curve, t.processed.raw, bkg.curve, opt.sideband)
            if t.visible:
                kind = "floor" if is_floor else "background" if t.is_background else "normal"
                legend = t.m.name + (" (reference)" if is_floor else "")
                if t.integrated and not is_floor:
                    legend += f" — {t.integrated.phase_deg:.3g}° rms"
                series.append((t.key, t.m.name, t.color, t.processed, kind, legend))
        self.plot.set_traces(series, self.show_raw.isChecked())
        if rng:
            self.plot.set_region(*rng, visible=self.int_show.isChecked())
        else:
            self.plot.region.setVisible(False)
        self.update_tables()

    def visible_traces(self):
        return [t for t in self.traces if t.visible and t.processed is not None]

    def _region_from_plot(self, f1, f2):
        self.int_f1.set_value(float(f"{f1:.3g}"))
        self.int_f2.set_value(float(f"{f2:.3g}"))
        self.schedule_refresh()

    def _sync_region_fields(self):
        self.schedule_refresh()

    def _preset_range(self, label):
        if label == "Full":
            fs = [t.curve.f for t in self.visible_traces() if len(t.curve.f)]
            if not fs:
                return
            f1, f2 = min(f[0] for f in fs), max(f[-1] for f in fs)
        else:
            a, b = label.split("–")
            f1, f2 = parse_si(a), parse_si(b)
        self.int_f1.set_value(f1)
        self.int_f2.set_value(f2)
        self.schedule_refresh()

    # --------------------------------------------------------------------------- tables
    def update_tables(self):
        vis = self.visible_traces()
        floor = self.floor_trace()
        # integrated
        tb = self.int_table
        tb.setRowCount(0)
        for t in vis:
            r = t.integrated
            row = tb.rowCount()
            tb.insertRow(row)
            name = t.m.name + (" (reference)" if t is floor else "")
            tb.setItem(row, 0, cell(name, False, icon=swatch(style.FLOOR if t is floor else t.color)))
            if r:
                vals = [f"{format_freq(r.f1, 3)} – {format_freq(r.f2, 3)}", f"{r.phase_deg:.4g}",
                        f"{r.phase_rad * 1e3:.4g}", f"{r.dbc:.1f}", format_si(r.jitter_s, "s", 4),
                        format_si(r.fm_hz, "Hz", 4)]
            else:
                vals = ["—"] * 6
            for c, v in enumerate(vals, 1):
                tb.setItem(row, c, cell(v))
        # spot noise
        try:
            spots = [parse_si(x) for x in self.spot_edit.text().replace(";", ",").split(",") if x.strip()]
            self.spot_edit.setStyleSheet("")
        except ValueError:
            spots = []
            self.spot_edit.setStyleSheet("QLineEdit { background: #fde2e1; }")
        self._spots = spots
        tb = self.spot_table
        tb.setColumnCount(1 + len(spots))
        tb.setHorizontalHeaderLabels(["Trace"] + [format_freq(s, 3) for s in spots])
        tb.setRowCount(0)
        for t in vis:
            row = tb.rowCount()
            tb.insertRow(row)
            tb.setItem(row, 0, cell(t.m.name, False, icon=swatch(style.FLOOR if t is floor else t.color)))
            for c, s in enumerate(spots, 1):
                tb.setItem(row, c, cell(fmt(A.spot(t.processed.f, t.processed.clean, s))))
        # spurs
        tb = self.spur_table
        tb.setRowCount(0)
        for t in vis:
            if t.is_background:
                continue
            for sp in t.processed.spurs:
                row = tb.rowCount()
                tb.insertRow(row)
                tb.setItem(row, 0, cell(t.m.name, False, icon=swatch(t.color)))
                tb.setItem(row, 1, cell(format_freq(sp.offset, 5)))
                tb.item(row, 1).setData(Qt.UserRole, sp.offset)
                tb.setItem(row, 2, cell(f"{sp.dbc:.1f}"))
                tb.setItem(row, 3, cell(f"{sp.excess_db:.1f}"))
                origin = {True: "yes — external", False: "no — from DUT", None: "—"}[sp.background]
                tb.setItem(row, 4, cell(origin, False, color="#a8551c" if sp.background else None))
        self.tabs.setTabText(3, f"Spurs ({tb.rowCount()})")
        tb.setColumnHidden(4, not (self.mpanel.bkg_enable.isChecked()
                                   or any(t.is_background for t in self.traces)))
        self.update_marker_table()
        self.update_info()

    def _schedule_markers(self):
        self._marker_timer.start()

    def update_marker_table(self):
        vis = self.visible_traces()
        tb = self.marker_table
        tb.setColumnCount(2 + len(vis))
        tb.setHorizontalHeaderLabels(["Marker", "Offset"] + [t.m.name for t in vis])
        for c, t in enumerate(vis, 2):
            tb.horizontalHeaderItem(c).setIcon(swatch(t.color))
        tb.setRowCount(len(self.plot.markers))
        delta = self.delta.isChecked() and self.plot.markers
        ref = self.plot.markers[0] if delta else None
        for r, m in enumerate(self.plot.markers):
            tb.setItem(r, 0, cell(m.label + (" (Δ)" if delta and m is not ref else ""), False))
            f0 = m.freq
            tb.setItem(r, 1, cell(format_freq(f0, 5)))
            for c, t in enumerate(vis, 2):
                v = A.value_at(t.processed.f, t.processed.display, f0)
                if delta and m is not ref:
                    v -= A.value_at(t.processed.f, t.processed.display, ref.freq)
                    tb.setItem(r, c, cell(fmt(v, "+.1f") + " dB"))
                else:
                    tb.setItem(r, c, cell(fmt(v) + " dBc/Hz"))

    def _marker_table_clicked(self, row, col):
        if row < len(self.plot.markers):
            self.plot.remove_marker(self.plot.markers[row])

    def _spur_clicked(self, row, col):
        f0 = self.spur_table.item(row, 1).data(Qt.UserRole)
        self.plot.add_marker(f0)

    def marker_to_peak(self):
        t = self.selected_trace() or (self.visible_traces()[0] if self.visible_traces() else None)
        if not t or t.processed is None:
            return
        m = self.plot.markers[-1] if self.plot.markers else self.plot.add_marker()
        (x0, x1), _ = self.plot.viewRange()
        p = t.processed
        sel = (p.f >= 10 ** x0) & (p.f <= 10 ** x1)
        if sel.any():
            import numpy as np
            y = np.where(sel & np.isfinite(p.display), p.display, -np.inf)
            m.set_freq(float(p.f[int(np.argmax(y))]))

    def update_info(self):
        t = self.selected_trace()
        tb = self.seg_table
        tb.setRowCount(0)
        if not t:
            self.info_label.setText("Select a trace to see its details.")
            return
        m = t.m
        warn = m.settings.get("warnings", []) + m.settings.get("analyzer_errors", [])
        txt = (f"<b>{m.name}</b> — {m.timestamp}<br>"
               f"Carrier {m.carrier_freq:,.1f} Hz at {m.carrier_power:.2f} dBm · {m.instrument}<br>"
               f"File: {m.path or '<i>not saved</i>'}")
        if m.notes:
            txt += f"<br>Notes: {m.notes}"
        if warn:
            txt += "<br><span style='color:#a8551c'>" + "<br>".join(warn) + "</span>"
        self.info_label.setText(txt)
        for s in m.segments:
            r = tb.rowCount()
            tb.insertRow(r)
            vals = [f"{format_freq(s.f_lo, 3)} – {format_freq(s.f_hi, 3)}", format_freq(s.span, 3),
                    format_freq(s.rbw, 3), format_freq(s.vbw, 3), str(s.averages),
                    f"{s.sweep_time:.3g} s" if s.sweep_time else "—", f"{s.carrier_freq:,.1f} Hz"]
            for c, v in enumerate(vals):
                tb.setItem(r, c, cell(v, c > 0))

    # ------------------------------------------------------------------------ instrument
    def logmsg(self, msg):
        self.log.appendPlainText(msg)

    def _analyzer(self):
        p = self.mpanel
        return create(p.driver.currentText(), p.host.text().strip())

    def _run_task(self, fn, on_done, label):
        if self.task is not None:
            return
        self.mpanel.save_settings()
        self.task = Task(fn, self)
        self.task.progress.connect(self._task_progress)
        self.task.done.connect(on_done)
        self.task.failed.connect(self._task_failed)
        self.task.cancelled.connect(self._task_cancelled)
        self.task.finished.connect(self._task_finished)
        self.mpanel.set_running(True)
        self.mpanel.status.setText(label)
        self.logmsg(label)
        self.task.start()

    def _task_progress(self, msg, frac):
        self.mpanel.status.setText(msg)
        self.mpanel.progress.setValue(int(frac * 1000))
        self.logmsg(msg)

    def _task_failed(self, msg):
        self.mpanel.status.setText("Error: " + msg)
        self.logmsg("ERROR: " + msg)
        self._finish_live(keep_partial=True)
        QMessageBox.warning(self, "PNScope", msg)

    def _task_cancelled(self):
        self.mpanel.status.setText("Stopped")
        self.logmsg("Stopped by user")
        self._finish_live(keep_partial=True)

    def _task_finished(self):
        self.task = None
        self.mpanel.set_running(False)
        self.mpanel.progress.setValue(0)
        self.mpanel.update_plan()

    def stop_task(self):
        if self.task:
            self.task.cancel()
            self.mpanel.status.setText("Stopping after the current sweep…")

    def test_connection(self):
        def fn(progress, partial, cancel):
            with self._analyzer() as a:
                return a.identify()

        def done(idn):
            self.mpanel.idn.setText(idn)
            self.mpanel.status.setText("Connected")
            self.logmsg(idn)
        self._run_task(fn, done, "Connecting…")

    def get_center(self):
        def fn(progress, partial, cancel):
            with self._analyzer() as a:
                return a.get_center()
        self._run_task(fn, lambda f: (self.mpanel.carrier.set_value(f),
                                      self.mpanel.status.setText(f"Centre {format_freq(f, 10)}")),
                       "Reading centre frequency…")

    def find_carrier(self):
        def fn(progress, partial, cancel):
            with self._analyzer() as a:
                return find_carrier(a, cancel=cancel, progress=progress)

        def done(r):
            fc, lvl = r
            self.mpanel.carrier.set_value(round(fc, 1))
            msg = f"Found carrier at {format_freq(fc, 10)}, {lvl:.1f} dBm"
            self.mpanel.status.setText(msg)
            self.logmsg(msg)
        self._run_task(fn, done, "Searching full span for the strongest signal…")

    def start_measurement(self):
        if self.task is not None:
            return
        p = self.mpanel
        if not p.inputs_valid():
            QMessageBox.warning(self, "PNScope", "Please correct the highlighted fields.")
            return
        ps, ms = p.plan_settings(), p.measure_settings()

        def fn(progress, partial, cancel):
            with self._analyzer() as a:
                run = PhaseNoiseRun(a, ps, ms, progress=progress, cancel=cancel,
                                    on_segment=lambda m: partial(
                                        dataclasses.replace(m, segments=list(m.segments))))
                for w in run.warnings:
                    progress("Warning: " + w, 0.0)
                return run.run()

        self.live_trace = None
        self._run_task(fn, self._measurement_done, f"Measuring '{ms.name}'…")
        self.task.partial.connect(self._measurement_partial)

    def start_background(self):
        """Repeat the selected measurement with the DUT's RF off."""
        if self.task is not None:
            return
        t = self.selected_trace()
        if t is None or t.is_background or t.live or not t.m.segments:
            QMessageBox.information(self, "Background", "Select the measurement the background "
                                    "belongs to (a completed trace) first.")
            return
        r = QMessageBox.question(
            self, "Background measurement",
            f"Switch the RF of the DUT off now (leave all cables connected).\n\n"
            f"PNScope repeats the segments of '{t.m.name}' at {format_freq(t.m.carrier_freq, 10)} "
            f"with the {self.mpanel.driver.currentText()}, without carrier.\nSpurs that are also "
            f"there come from the surroundings or the analyzer, not from the DUT.",
            QMessageBox.Ok | QMessageBox.Cancel)
        if r != QMessageBox.Ok:
            return
        ms = background_settings(t.m, restore=self.mpanel.restore.isChecked())
        plan, states = plan_from(t.m), states_from(t.m)

        def fn(progress, partial, cancel):
            with self._analyzer() as a:
                run = PhaseNoiseRun(a, PlanSettings(), ms, progress=progress, cancel=cancel, plan=plan,
                                    segment_states=states,
                                    on_segment=lambda m: partial(
                                        dataclasses.replace(m, segments=list(m.segments))))
                return run.run()

        self.live_trace = None
        self._run_task(fn, self._measurement_done, f"Measuring background for '{t.m.name}'…")
        self.task.partial.connect(self._measurement_partial)

    def _measurement_partial(self, m):
        if self.live_trace is None:
            self.live_trace = self.add_trace(m)
            self.live_trace.live = True
        else:
            self.live_trace.update_measurement(m)
        self.rebuild_trace_table()
        self.schedule_refresh()

    def _measurement_done(self, m):
        if self.live_trace is None:
            self.live_trace = self.add_trace(m)
        self.live_trace.update_measurement(m)
        self.live_trace.live = False
        self.live_trace.dirty = True
        self.live_trace = None
        if "background_for" in m.settings:
            self.mpanel.status.setText("Background done — switch the DUT on again. The spur "
                                       "table shows which spurs are also there without carrier.")
            self.tabs.setCurrentWidget(self.spur_table)
        else:
            self.mpanel.status.setText(f"Done: carrier {format_freq(m.carrier_freq, 10)}, "
                                       f"{m.carrier_power:.2f} dBm")
            self.mpanel.name.clear()
        for e in m.settings.get("analyzer_errors", []):
            self.logmsg("Analyzer error queue: " + e)
        self.rebuild_trace_table()
        self.schedule_refresh()

    def _finish_live(self, keep_partial):
        t = self.live_trace
        self.live_trace = None
        if t is None:
            return
        t.live = False
        if keep_partial and t.m.segments:
            t.m.name += " (partial)"
            t.dirty = True
        else:
            self.traces.remove(t)
        self.rebuild_trace_table()
        self.schedule_refresh()

    # ----------------------------------------------------------------------------- files
    def _dir(self):
        return QSettings().value("files/dir", os.getcwd())

    def _set_dir(self, path):
        QSettings().setValue("files/dir", str(Path(path).parent))

    def open_files(self, paths=None):
        if not paths:
            paths, _ = QFileDialog.getOpenFileNames(self, "Open measurements", self._dir(), FILE_FILTER)
        for p in paths:
            try:
                m = load(p)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "Open", f"{p}:\n{e}")
                continue
            t = self.add_trace(m)
            t.dirty = m.path is None  # imported legacy data is not yet in our format
            self._set_dir(p)
            self.logmsg(f"Opened {p}")
        self.rebuild_trace_table()

    def _safe_name(self, name):
        return "".join(c if c.isalnum() or c in "-_. " else "_" for c in name).strip() or "measurement"

    def save_trace(self, t: Trace, ask=True):
        path = t.m.path
        if ask or not path:
            default = path or str(Path(self._dir()) / (self._safe_name(t.m.name) + FILE_SUFFIX))
            path, _ = QFileDialog.getSaveFileName(self, f"Save '{t.m.name}'", default,
                                                  f"PNScope measurement (*{FILE_SUFFIX})")
            if not path:
                return False
            if not path.endswith(FILE_SUFFIX):
                path += FILE_SUFFIX
        t.m.save(path)
        t.dirty = False
        self._set_dir(path)
        self.logmsg(f"Saved {path}")
        self.rebuild_trace_table()
        return True

    def save_selected(self):
        t = self.selected_trace()
        if t is None:
            QMessageBox.information(self, "Save", "Select a trace first.")
            return
        self.save_trace(t)

    def save_all(self):
        for t in self.traces:
            if t.dirty and not t.live:
                if not self.save_trace(t, ask=not t.m.path):
                    return

    def figure_spec(self):
        vis = self.visible_traces()
        floor = self.floor_trace()
        views = [X.TraceView(t.m.name, t.color, t.m, t.processed, t is floor,
                             t.integrated if self.int_show.isChecked() else None,
                             t.is_background) for t in vis]
        (x0, x1), (y0, y1) = self.plot.viewRange()
        rng = self.int_range() if self.int_show.isChecked() else None
        opt = self.options()
        foot = (f"PNScope · sideband: {opt.sideband} · smoothing {opt.smoothing * 100:g} % decade"
                + (" · spurs removed" if opt.remove_spurs else "")
                + (" · reference subtracted" if opt.subtract_floor else "")
                + (f" · integration {format_freq(rng[0], 3)}–{format_freq(rng[1], 3)}" if rng else ""))
        return X.FigureSpec(views, self.title.text(), (10 ** x0, 10 ** x1), (y0, y1), rng,
                            [(m.label, m.freq) for m in self.plot.markers],
                            self.show_raw.isChecked(), foot)

    def export_plot(self):
        if not self.visible_traces():
            return
        path, flt = QFileDialog.getSaveFileName(self, "Export plot", self._dir(),
                                                "PNG image (*.png);;SVG vector (*.svg);;PDF (*.pdf)")
        if not path:
            return
        ext = {"PNG": ".png", "SVG": ".svg", "PDF": ".pdf"}[flt.split()[0]]
        if not Path(path).suffix:
            path += ext
        X.save_figure(self.figure_spec(), path)
        self._set_dir(path)
        self.logmsg(f"Exported plot {path}")

    def export_csv(self):
        t = self.selected_trace()
        if not t or t.processed is None:
            QMessageBox.information(self, "Export CSV", "Select a trace first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV",
                                              str(Path(self._dir()) / (self._safe_name(t.m.name) + ".csv")),
                                              "CSV (*.csv)")
        if path:
            X.save_csv(path, X.TraceView(t.m.name, t.color, t.m, t.processed))
            self._set_dir(path)
            self.logmsg(f"Exported {path}")

    def export_report(self):
        vis = self.visible_traces()
        if not vis:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export report", self._dir(), "Markdown (*.md)")
        if not path:
            return
        if not path.endswith(".md"):
            path += ".md"
        floor = self.floor_trace()
        views = [X.TraceView(t.m.name, t.color, t.m, t.processed, t is floor, t.integrated,
                             t.is_background) for t in vis]
        text = X.report(views, self.options(), self.int_range(), self._spots,
                        [(m.label, m.freq) for m in self.plot.markers])
        png = str(Path(path).with_suffix(".png"))
        X.save_figure(self.figure_spec(), png)
        text = text.replace("# Phase noise report\n", f"# Phase noise report\n\n![plot]({Path(png).name})\n", 1)
        Path(path).write_text(text)
        self._set_dir(path)
        self.logmsg(f"Exported report {path} (+ {png})")

    def copy_plot(self):
        QGuiApplication.clipboard().setPixmap(self.plot.grab())
        self.statusBar().showMessage("Plot copied to clipboard", 3000)

    def about(self):
        QMessageBox.about(self, "About PNScope",
                          f"<b>PNScope {__version__}</b><br>Phase-noise measurements with a "
                          "spectrum analyzer that has no phase-noise option.<br><br>"
                          "Method: RMS detector, power-averaged sweeps per offset decade, "
                          "L(f) = P − Pc − 10·log10(1.065·RBW).<br>"
                          "Readings close to the analyzer's own phase noise are an upper bound; "
                          "measure a clean source as a reference trace to see the floor.")

    # ----------------------------------------------------------------------------- close
    def closeEvent(self, ev):
        if self.task is not None:
            if QMessageBox.question(self, "Quit", "A measurement is running. Stop it and quit?") != QMessageBox.Yes:
                ev.ignore()
                return
            self.task.cancel()
            self.task.wait(15000)
        unsaved = [t for t in self.traces if t.dirty]
        if unsaved:
            r = QMessageBox.question(
                self, "Unsaved measurements",
                "Not saved:\n  " + "\n  ".join(t.m.name for t in unsaved) + "\n\nSave them now?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                ev.ignore()
                return
            if r == QMessageBox.Save:
                for t in unsaved:
                    if not self.save_trace(t, ask=not t.m.path):
                        ev.ignore()
                        return
        s = QSettings()
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("window/state", self.saveState())
        self.mpanel.save_settings()
        s.setValue("disp/sideband", self.sideband.currentText())
        s.setValue("disp/smooth", self.smoothing.value())
        s.setValue("disp/raw", self.show_raw.isChecked())
        s.setValue("disp/title", self.title.text())
        s.setValue("disp/spurs", self.spur_remove.isChecked())
        s.setValue("disp/spur_thr", self.spur_thr.value())
        s.setValue("disp/floor", self.floor_sub.isChecked())
        s.setValue("disp/floor_margin", self.floor_margin.value())
        s.setValue("int/show", self.int_show.isChecked())
        if self.int_range():
            s.setValue("int/f1", self.int_range()[0])
            s.setValue("int/f2", self.int_range()[1])
        s.setValue("spot/offsets", self.spot_edit.text())
        ev.accept()
