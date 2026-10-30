"""Left dock: analyzer connection, measurement settings, segment plan, start/stop."""
from __future__ import annotations

import datetime

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
                               QProgressBar, QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from ..analyzers import DRIVERS
from ..measure import MeasureSettings
from ..plan import PlanSettings, make_plan
from ..units import format_freq, parse_si


class FreqEdit(QLineEdit):
    """Line edit for frequencies with SI prefixes ('1.5G', '10 kHz')."""

    def __init__(self, value=None, placeholder="", parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        if value is not None:
            self.set_value(value)
        self.textChanged.connect(self._validate)

    def value(self):
        t = self.text().strip()
        if not t:
            return None
        try:
            return parse_si(t)
        except ValueError:
            return None

    def set_value(self, v):
        self.setText("" if v is None else format_freq(v, 10))

    def is_valid(self):
        return not self.text().strip() or self.value() is not None

    def _validate(self):
        self.setStyleSheet("" if self.is_valid() else "QLineEdit { background: #fde2e1; }")


class MeasurePanel(QWidget):
    start_requested = Signal()
    stop_requested = Signal()
    test_requested = Signal()
    find_requested = Signal()
    get_center_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        s = QSettings()
        lay = QVBoxLayout(self)

        # analyzer
        g = QGroupBox("Analyzer")
        f = QFormLayout(g)
        self.driver = QComboBox()
        self.driver.addItems(list(DRIVERS))
        self.driver.setCurrentText(s.value("analyzer/driver", next(iter(DRIVERS))))
        self.host = QLineEdit(s.value("analyzer/host", "192.168.10.108"))
        row = QHBoxLayout()
        row.addWidget(self.host, 1)
        self.test_btn = QPushButton("Test")
        self.test_btn.setToolTip("Connect and read the instrument identification")
        row.addWidget(self.test_btn)
        self.idn = QLabel("")
        self.idn.setWordWrap(True)
        self.idn.setStyleSheet("color: #52514e")
        f.addRow("Model", self.driver)
        f.addRow("Address", row)
        f.addRow(self.idn)
        lay.addWidget(g)

        # carrier & offsets
        g = QGroupBox("Measurement")
        f = QFormLayout(g)
        self.carrier = FreqEdit(None, "analyzer centre frequency")
        self.carrier.setText(s.value("meas/carrier", ""))
        row = QHBoxLayout()
        row.addWidget(self.carrier, 1)
        self.get_btn = QToolButton(text="Get")
        self.get_btn.setToolTip("Use the analyzer's current centre frequency")
        self.find_btn = QToolButton(text="Find")
        self.find_btn.setToolTip("Search the full span for the strongest signal")
        row.addWidget(self.get_btn)
        row.addWidget(self.find_btn)
        f.addRow("Carrier", row)
        self.search = FreqEdit(float(s.value("meas/search", 1e6)))
        self.search.setToolTip("The carrier is searched within ± half this span around the given frequency")
        f.addRow("Search span", self.search)
        self.start = FreqEdit(float(s.value("meas/start", 100)))
        self.stop = FreqEdit(float(s.value("meas/stop", 1e6)))
        f.addRow("Start offset", self.start)
        f.addRow("Stop offset", self.stop)
        self.averages = QSpinBox(minimum=1, maximum=1000, value=int(s.value("meas/avg", 8)))
        self.averages.setToolTip("Power-averaged sweeps per segment (more = smoother, slower)")
        f.addRow("Averages", self.averages)
        self.name = QLineEdit()
        self.name.setPlaceholderText("automatic (date/time)")
        f.addRow("Name", self.name)
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("Notes stored with the measurement (DUT, setup, …)")
        self.notes.setMaximumHeight(52)
        f.addRow("Notes", self.notes)
        lay.addWidget(g)

        # advanced
        self.adv = QGroupBox("Advanced")
        self.adv.setCheckable(True)
        self.adv.setChecked(s.value("meas/adv_open", False, type=bool))
        f = QFormLayout(self.adv)
        self.track = QCheckBox("Re-centre on carrier before each segment")
        self.track.setChecked(s.value("meas/track", True, type=bool))
        self.autoref = QCheckBox("Reference level = carrier +")
        self.autoref.setChecked(s.value("meas/autoref", True, type=bool))
        self.ref_margin = QDoubleSpinBox(minimum=0, maximum=30, value=float(s.value("meas/refmargin", 5)),
                                         suffix=" dB", decimals=0)
        row = QHBoxLayout()
        row.addWidget(self.autoref)
        row.addWidget(self.ref_margin)
        row.addStretch()
        self.restore = QCheckBox("Restore analyzer settings afterwards")
        self.restore.setChecked(s.value("meas/restore", True, type=bool))
        self.span_rbw = QDoubleSpinBox(minimum=50, maximum=5000, value=float(s.value("meas/spanrbw", 600)),
                                       decimals=0)
        self.span_rbw.setToolTip("Target span/RBW per segment. ~600 gives one RBW per trace point at 751 points.")
        self.min_rbw = QDoubleSpinBox(minimum=2, maximum=100, value=float(s.value("meas/minrbw", 10)),
                                      decimals=1, suffix=" × RBW")
        self.min_rbw.setToolTip("Lowest offset used from a segment, in RBWs (keeps the carrier skirt out)")
        self.vbw = QDoubleSpinBox(minimum=1, maximum=100, value=float(s.value("meas/vbw", 3)),
                                  decimals=0, suffix=" × RBW")
        f.addRow(self.track)
        f.addRow(row)
        f.addRow(self.restore)
        f.addRow("Span / RBW", self.span_rbw)
        f.addRow("Min. offset", self.min_rbw)
        f.addRow("VBW", self.vbw)
        self.adv.toggled.connect(self._toggle_adv)
        lay.addWidget(self.adv)

        # plan preview
        g = QGroupBox("Segments")
        v = QVBoxLayout(g)
        self.plan_table = QTableWidget(0, 4)
        self.plan_table.setHorizontalHeaderLabels(["Offsets", "Span", "RBW", "Avg"])
        self.plan_table.verticalHeader().hide()
        self.plan_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.plan_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.plan_table.horizontalHeader().setStretchLastSection(True)
        self.plan_table.setMaximumHeight(170)
        v.addWidget(self.plan_table)
        self.plan_warn = QLabel()
        self.plan_warn.setWordWrap(True)
        self.plan_warn.setStyleSheet("color: #a8551c")
        v.addWidget(self.plan_warn)
        lay.addWidget(g)

        # run
        row = QHBoxLayout()
        self.start_btn = QPushButton("▶  Measure")
        self.start_btn.setDefault(True)
        self.start_btn.setStyleSheet("font-weight: bold; padding: 6px 14px")
        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.setEnabled(False)
        row.addWidget(self.start_btn, 1)
        row.addWidget(self.stop_btn)
        lay.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        lay.addWidget(self.progress)
        self.status = QLabel("Idle")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addStretch(1)

        self.driver.currentTextChanged.connect(self._driver_changed)
        self._driver_changed(self.driver.currentText())
        for w in (self.start, self.stop):
            w.textChanged.connect(self.update_plan)
        for w in (self.averages, self.span_rbw, self.min_rbw, self.vbw):
            w.valueChanged.connect(self.update_plan)
        self.driver.currentTextChanged.connect(self.update_plan)
        self.start_btn.clicked.connect(self.start_requested)
        self.stop_btn.clicked.connect(self.stop_requested)
        self.test_btn.clicked.connect(self.test_requested)
        self.find_btn.clicked.connect(self.find_requested)
        self.get_btn.clicked.connect(self.get_center_requested)
        self._toggle_adv(self.adv.isChecked())
        self.update_plan()

    def _toggle_adv(self, on):
        for w in self.adv.findChildren(QWidget):
            w.setVisible(on)

    def _driver_changed(self, name):
        cls = DRIVERS.get(name)
        self.host.setEnabled(bool(cls and cls.needs_host))
        self.test_btn.setEnabled(True)

    def driver_class(self):
        return DRIVERS[self.driver.currentText()]

    def plan_settings(self) -> PlanSettings:
        return PlanSettings(start=self.start.value() or 100, stop=self.stop.value() or 1e6,
                            averages=self.averages.value(), span_per_rbw=self.span_rbw.value(),
                            min_offset_rbw=self.min_rbw.value(), vbw_ratio=self.vbw.value())

    def measure_settings(self) -> MeasureSettings:
        name = self.name.text().strip() or datetime.datetime.now().strftime("PN %Y-%m-%d %H:%M:%S")
        return MeasureSettings(name=name, carrier_freq=self.carrier.value(),
                               search_span=self.search.value() or 1e6,
                               track_carrier=self.track.isChecked(),
                               auto_ref_level=self.autoref.isChecked(),
                               ref_margin=self.ref_margin.value(), restore=self.restore.isChecked(),
                               notes=self.notes.toPlainText().strip())

    def inputs_valid(self):
        return all(w.is_valid() for w in (self.carrier, self.search, self.start, self.stop))

    def update_plan(self):
        self.plan_table.setRowCount(0)
        try:
            plan, warn = make_plan(self.plan_settings(), self.driver_class().rbw_steps)
        except ValueError as e:
            self.plan_warn.setText(str(e))
            self.start_btn.setEnabled(False)
            return
        self.start_btn.setEnabled(not self.stop_btn.isEnabled())
        for p in plan:
            r = self.plan_table.rowCount()
            self.plan_table.insertRow(r)
            for c, txt in enumerate([f"{format_freq(p.f_lo, 3)} – {format_freq(p.f_hi, 3)}",
                                     format_freq(p.span, 3), format_freq(p.rbw, 3), str(p.averages)]):
                it = QTableWidgetItem(txt)
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter if c else Qt.AlignLeft | Qt.AlignVCenter)
                self.plan_table.setItem(r, c, it)
        self.plan_warn.setText("\n".join(warn))
        self.plan_warn.setVisible(bool(warn))

    def set_running(self, running):
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        for w in (self.find_btn, self.get_btn, self.test_btn, self.driver, self.host):
            w.setEnabled(not running)
        if not running:
            self._driver_changed(self.driver.currentText())

    def save_settings(self):
        s = QSettings()
        s.setValue("analyzer/driver", self.driver.currentText())
        s.setValue("analyzer/host", self.host.text())
        s.setValue("meas/carrier", self.carrier.text())
        for key, w in (("search", self.search), ("start", self.start), ("stop", self.stop)):
            if w.value():
                s.setValue(f"meas/{key}", w.value())
        s.setValue("meas/avg", self.averages.value())
        s.setValue("meas/adv_open", self.adv.isChecked())
        s.setValue("meas/track", self.track.isChecked())
        s.setValue("meas/autoref", self.autoref.isChecked())
        s.setValue("meas/refmargin", self.ref_margin.value())
        s.setValue("meas/restore", self.restore.isChecked())
        s.setValue("meas/spanrbw", self.span_rbw.value())
        s.setValue("meas/minrbw", self.min_rbw.value())
        s.setValue("meas/vbw", self.vbw.value())
