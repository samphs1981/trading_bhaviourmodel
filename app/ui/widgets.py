"""Shared UI pieces: background tasks, security picker, profile editor, chart and table helpers."""
from __future__ import annotations

import os
import traceback

import pyqtgraph as pg
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .. import data, engine

DISCLAIMER = 'Simulation for research only. Not financial advice. This app places no orders.'

_LIGHT = dict(surface='#fcfcfb', ink='#0b0b0b', secondary='#52514e', muted='#898781', grid='#e1e0d9', axis='#c3c2b7',
              good='#0ca30c', critical='#d03b3b', warning='#b97a00',
              series=['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'])
_DARK = dict(surface='#1a1a19', ink='#f0efec', secondary='#c3c2b7', muted='#898781', grid='#2c2c2a', axis='#383835',
             good='#0ca30c', critical='#e66767', warning='#fab219',
             series=['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'])


def theme() -> dict:
    app = QApplication.instance()
    dark = bool(app) and app.palette().color(QPalette.Window).lightness() < 128
    return _DARK if dark else _LIGHT


def money(v: float, cents: bool = False) -> str:
    return f'${v:,.2f}' if cents else f'${v:,.0f}'


def price_text(v: float) -> str:
    a = abs(v)
    return f'{v:,.2f}' if a >= 100 else (f'{v:,.3f}' if a >= 1 else f'{v:.5f}')


def side_text(direction: int) -> str:
    return 'Long' if direction > 0 else 'Short'


def disclaimer_label() -> QLabel:
    lab = QLabel(DISCLAIMER)
    lab.setStyleSheet(f'color: {theme()["secondary"]}; font-size: 12px; padding: 2px 0;')
    return lab


# ---------- background work ----------
class _TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)


class _Task(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = _TaskSignals()
        self.setAutoDelete(False)

    def run(self):
        try:
            result = self.fn(self.signals.progress.emit)
        except ValueError as ex:
            self.signals.failed.emit(str(ex))
        except Exception as ex:                       # never let a worker crash the app
            traceback.print_exc()
            self.signals.failed.emit(f'Unexpected error: {ex}')
        else:
            self.signals.done.emit(result)


_running: set = set()


def run_async(fn, on_done, on_error, on_progress=None) -> None:
    """Run fn(progress) on a worker thread; callbacks arrive on the UI thread."""
    task = _Task(fn)
    _running.add(task)                                # keep it alive until it reports back

    def finish(callback, value):
        _running.discard(task)
        callback(value)

    task.signals.done.connect(lambda r: finish(on_done, r))
    task.signals.failed.connect(lambda m: finish(on_error, m))
    if on_progress:
        task.signals.progress.connect(on_progress)
    QThreadPool.globalInstance().start(task)


# ---------- security picker ----------
class SecurityPicker(QWidget):
    """Dropdown of known securities, a free-text Yahoo symbol, and a CSV fallback."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._csv = None
        self.combo = QComboBox()
        for sym, name in data.SECURITIES:
            self.combo.addItem(f'{name} ({sym})', sym)
        self.custom = QLineEdit()
        self.custom.setPlaceholderText('or Yahoo symbol, e.g. WES.AX')
        self.custom.setClearButtonEnabled(True)
        self.csv_btn = QPushButton('Load CSV…')
        self.csv_btn.setToolTip('Use a date,close CSV file instead of downloading from Yahoo')
        self.csv_btn.clicked.connect(self._pick_csv)
        self.csv_label = QLabel('')
        self.csv_label.setWordWrap(True)
        self.csv_clear = QPushButton('Use Yahoo instead')
        self.csv_clear.clicked.connect(lambda: self.set_csv(None))
        row = QHBoxLayout()
        row.addWidget(self.csv_btn)
        row.addWidget(self.csv_clear)
        row.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.combo)
        lay.addWidget(self.custom)
        lay.addLayout(row)
        lay.addWidget(self.csv_label)
        self.set_csv(None)

    def _pick_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Load a date,close CSV', os.getcwd(), 'CSV files (*.csv);;All files (*)')
        if path:
            self.set_csv(path)

    def set_csv(self, path):
        self._csv = path or None
        self.csv_label.setText(f'Prices from file: {os.path.basename(path)}' if path else '')
        self.csv_label.setVisible(bool(path))
        self.csv_clear.setVisible(bool(path))
        self.combo.setEnabled(not path)
        self.custom.setEnabled(not path)

    def symbol(self) -> str:
        return self.custom.text().strip().upper() or self.combo.currentData()

    def csv_path(self):
        return self._csv

    def display_name(self) -> str:
        if self._csv:
            return os.path.splitext(os.path.basename(self._csv))[0]
        return data.security_name(self.symbol())


# ---------- behaviour switches and profile editor ----------
class SwitchBoxes(QWidget):
    """The extra behaviours as independent checkboxes."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.boxes = {}
        for key, label, tip in engine.ALL_SWITCHES:
            box = QCheckBox(label)
            box.setToolTip(tip[0].upper() + tip[1:])
            box.toggled.connect(lambda *_: self.changed.emit())
            lay.addWidget(box)
            self.boxes[key] = box

    def values(self) -> dict:
        return {k: b.isChecked() for k, b in self.boxes.items()}

    def set_values(self, profile: engine.Profile):
        for k, b in self.boxes.items():
            b.setChecked(getattr(profile, k))


class ProfileEditor(QWidget):
    """Pick a preset or build a custom profile from the switches."""

    def __init__(self, with_switches: bool = True, parent=None):
        super().__init__(parent)
        self._loading = False
        self.preset = QComboBox()
        for p in engine.PRESETS:
            self.preset.addItem(p.name)
        self.preset.addItem('Custom')
        self.tp = QDoubleSpinBox(decimals=2, minimum=0.05, maximum=100, singleStep=0.5, suffix=' %')
        self.sl = QDoubleSpinBox(decimals=2, minimum=0.05, maximum=100, singleStep=0.5, suffix=' %')
        self.no_stop = QCheckBox('No stop-loss')
        self.lev = QDoubleSpinBox(decimals=1, minimum=0.1, maximum=100, singleStep=1, suffix=' x')
        self.revenge = QCheckBox('Revenge sizing (double after a loss, max 4x)')
        self.overconf = QCheckBox('Overconfidence sizing (double after 2 wins in a row)')
        self.switches = SwitchBoxes() if with_switches else None
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow('Profile', self.preset)
        form.addRow('Take-profit', self.tp)
        stop_row = QHBoxLayout()
        stop_row.addWidget(self.sl)
        stop_row.addWidget(self.no_stop)
        form.addRow('Stop-loss', stop_row)
        form.addRow('Leverage', self.lev)
        form.addRow('', self.revenge)
        form.addRow('', self.overconf)
        if self.switches:
            form.addRow('While a trade is open', self.switches)
        self.preset.currentIndexChanged.connect(self._preset_chosen)
        for w in (self.tp, self.sl, self.lev):
            w.valueChanged.connect(self._edited)
        for w in (self.no_stop, self.revenge, self.overconf):
            w.toggled.connect(self._edited)
        self._preset_chosen(0)

    def _preset_chosen(self, i):
        if i < len(engine.PRESETS):
            self._load(engine.PRESETS[i])

    def _load(self, p: engine.Profile):
        self._loading = True
        self.tp.setValue(p.tp * 100)
        self.no_stop.setChecked(p.sl is None)
        self.sl.setValue((p.sl or 0.04) * 100)
        self.sl.setEnabled(p.sl is not None)
        self.lev.setValue(p.lev)
        self.revenge.setChecked(p.revenge)
        self.overconf.setChecked(p.overconf)
        self._loading = False

    def _edited(self, *_):
        self.sl.setEnabled(not self.no_stop.isChecked())
        if self._loading:
            return
        cur = self._base()
        match = next((i for i, p in enumerate(engine.PRESETS)
                      if (p.tp, p.sl, p.lev, p.revenge, p.overconf) == (cur.tp, cur.sl, cur.lev, cur.revenge, cur.overconf)),
                     len(engine.PRESETS))
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(match)
        self.preset.blockSignals(False)

    def _base(self) -> engine.Profile:
        sl = None if self.no_stop.isChecked() else round(self.sl.value() / 100, 6)
        return engine.Profile('Custom', round(self.tp.value() / 100, 6), sl, round(self.lev.value(), 2),
                              self.revenge.isChecked(), self.overconf.isChecked())

    def profile(self) -> engine.Profile:
        p = self._base()
        i = self.preset.currentIndex()
        if i < len(engine.PRESETS):
            p = engine.PRESETS[i]                     # exact preset values, not spin-box round trips
        else:
            p = p.with_switches(name=f'Custom ({p.describe()})')
        return p.with_switches(**self.switches.values()) if self.switches else p

    def set_profile(self, p: engine.Profile):
        self._load(p)
        self._edited()
        if self.switches:
            self.switches.set_values(p)


# ---------- charts ----------
class DateAxis(pg.AxisItem):
    """Bottom axis over session indices, labelled with the session dates."""

    def __init__(self):
        super().__init__(orientation='bottom')
        self.dates: list = []

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            i = int(round(v))
            out.append(self.dates[i] if abs(v - i) < 1e-6 and 0 <= i < len(self.dates) else '')
        return out


class FormatAxis(pg.AxisItem):
    def __init__(self, fmt, orientation='left'):
        super().__init__(orientation=orientation)
        self.fmt = fmt

    def tickStrings(self, values, scale, spacing):
        return [self.fmt(v) for v in values]


def make_plot(value_fmt, height: int = 200) -> pg.PlotWidget:
    """A plot over session indices with recessive axes and a light horizontal grid."""
    t = theme()
    plot = pg.PlotWidget(axisItems={'bottom': DateAxis(), 'left': FormatAxis(value_fmt)}, background=t['surface'])
    plot.setMinimumHeight(height)
    plot.setMenuEnabled(False)
    plot.setMouseEnabled(x=True, y=False)
    plot.hideButtons()
    plot.showGrid(x=False, y=True, alpha=0.18)
    for name in ('bottom', 'left'):
        ax = plot.getAxis(name)
        ax.setPen(pg.mkPen(t['axis']))
        ax.setTextPen(pg.mkPen(t['muted']))
    plot.getAxis('left').setWidth(76)
    return plot


def set_dates(plot: pg.PlotWidget, dates: list) -> None:
    plot.getAxis('bottom').dates = list(dates)


class Crosshair(QObject):
    """Vertical line that follows the cursor and reports the session index under it (-1 when outside)."""
    moved = Signal(int)

    def __init__(self, plot: pg.PlotWidget):
        super().__init__(plot)
        self.plot = plot
        self.count = 0
        self.line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(theme()['muted'], width=1))
        plot.scene().sigMouseMoved.connect(self._on_move)

    def attach(self, count: int):
        """Call after plot.clear(): re-adds the line and sets how many sessions are plotted."""
        self.count = count
        self.line.hide()
        self.plot.addItem(self.line, ignoreBounds=True)

    def show_at(self, i: int):
        if i < 0:
            self.line.hide()
        else:
            self.line.setPos(i)
            self.line.show()

    def _on_move(self, pos):
        if self.count and self.plot.sceneBoundingRect().contains(pos):
            x = self.plot.getPlotItem().vb.mapSceneToView(pos).x()
            self.moved.emit(min(max(int(round(x)), 0), self.count - 1))
        else:
            self.moved.emit(-1)


def swatch(color: str) -> str:
    return f'<span style="color:{color}">&#9632;</span>'


# ---------- tables ----------
def make_table() -> QTableWidget:
    table = QTableWidget()
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectRows)
    table.setSelectionMode(QTableWidget.SingleSelection)
    table.setAlternatingRowColors(True)
    table.verticalHeader().hide()
    table.horizontalHeader().setHighlightSections(False)
    return table


def fill_table(table: QTableWidget, headers: list, rows: list, right: tuple = ()) -> None:
    table.blockSignals(True)
    table.clear()
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            item = QTableWidgetItem(str(val))
            if c in right:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            table.setItem(r, c, item)
    for c in right:
        head = table.horizontalHeaderItem(c)
        if head:
            head.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    table.resizeColumnsToContents()
    table.resizeRowsToContents()
    table.blockSignals(False)
