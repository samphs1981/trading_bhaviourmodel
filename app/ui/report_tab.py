"""Report tab: replay every behaviour profile from one entry (security, date, long/short)."""
from __future__ import annotations

import base64
import bisect
import html

import pyqtgraph as pg
from PySide6.QtCore import QBuffer, QByteArray, QDate, QIODevice, Qt
from PySide6.QtWidgets import (QComboBox, QDateEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter, QVBoxLayout, QWidget)

from .. import data, engine
from .widgets import (DISCLAIMER, Crosshair, ProfileEditor, SecurityPicker, SwitchBoxes, disclaimer_label,
                      fill_table, make_plot, make_table, money, price_text, run_async, set_dates, side_text,
                      swatch, theme)

MAX_CURVES = 8
FOLLOW = [('random_one', "Random long/short: one trader's story"), ('same', 'Same side as my entry'),
          ('trend', 'Follow the 20-day trend'), ('random', f'Random long/short: median of {engine.MC_RUNS} traders')]
HORIZONS = [(252, '1 year (252 days)'), (126, '6 months (126 days)'), (63, '3 months (63 days)')]


def entry_index(dates: list, date: str) -> int:
    """Index of the entry date, or a clear error when it cannot be used."""
    if date not in dates:
        i = bisect.bisect_left(dates, date)
        near = [d for d in (dates[i - 1] if i > 0 else None, dates[i] if i < len(dates) else None) if d]
        raise ValueError(f'{date} is not a trading day in the data ({dates[0]} to {dates[-1]}). '
                         f'Nearest trading day{"s" if len(near) > 1 else ""}: {" and ".join(near)}.')
    i = dates.index(date)
    if i >= len(dates) - 1:
        raise ValueError(f'{date} is the latest close, so there is nothing after it to replay yet. Pick an earlier date.')
    return i


class ReportTab(QWidget):
    def __init__(self, cache_dir: str, parent=None):
        super().__init__(parent)
        self.cache_dir = cache_dir
        self.busy = False
        self.report = None            # last run: dict(rep=..., name=..., start=..., shown=[...])
        self.custom_profile = None
        self._build()

    # ---------- layout ----------
    def _build(self):
        self.security = SecurityPicker()
        self.date = QDateEdit(QDate.currentDate().addYears(-1))
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat('yyyy-MM-dd')
        self.side = QComboBox()
        self.side.addItem('Long', 'long')
        self.side.addItem('Short', 'short')
        self.follow = QComboBox()
        for key, label in FOLLOW:
            self.follow.addItem(label, key)
        self.follow.setCurrentIndex(self.follow.findData('random_one'))  # default: one trader's story, random long/short
        self.trader = QSpinBox(minimum=1, maximum=engine.MC_RUNS, value=1)
        self.trader.setToolTip('Each number is a different trader: a different sequence of long/short coin flips after your '
                               'first trade. They are the same traders that make up the median view.')
        self.follow.currentIndexChanged.connect(lambda *_: self.trader.setEnabled(self.follow.currentData() == 'random_one'))
        self.start = QDoubleSpinBox(decimals=0, minimum=100, maximum=1e9, singleStep=1000, prefix='$ ')
        self.start.setGroupSeparatorShown(True)
        self.start.setValue(10000)
        self.horizon = QComboBox()
        for days, label in HORIZONS:
            self.horizon.addItem(label, days)

        form = QFormLayout()
        form.addRow('Security', self.security)
        form.addRow('Entry date', self.date)
        form.addRow('Side', self.side)
        form.addRow('Later trades', self.follow)
        form.addRow('Trader number', self.trader)
        form.addRow('Starting account', self.start)
        form.addRow('Replay length', self.horizon)

        self.switches = SwitchBoxes()
        for box in self.switches.boxes.values():
            box.setChecked(True)                 # default: all four extra behaviours on
        sw_box = QGroupBox('Extra behaviours (applied to every profile)')
        QVBoxLayout(sw_box).addWidget(self.switches)

        self.curves = QListWidget()
        self.curves.setMinimumHeight(335)
        for p in engine.PRESETS:
            self._add_curve_item(p.name, p.name in engine.DEFAULT_SHOWN, f'{p.name}: {p.describe()}')
        self.curves.itemChanged.connect(self._curve_toggled)
        self.custom_btn = QPushButton('Add a custom profile…')
        self.custom_btn.clicked.connect(self._edit_custom)
        curve_box = QGroupBox(f'Curves to draw (up to {MAX_CURVES})')
        cl = QVBoxLayout(curve_box)
        cl.addWidget(self.curves)
        cl.addWidget(self.custom_btn)

        self.run_btn = QPushButton('Run model')
        self.run_btn.setDefault(True)
        self.run_btn.clicked.connect(self.run)
        self.export_btn = QPushButton('Export report…')
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.export)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel('Pick a security, your entry date and side, then run the model.')
        self.status.setWordWrap(True)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addLayout(form)
        ll.addWidget(sw_box)
        ll.addWidget(curve_box)
        ll.addWidget(self.run_btn)
        ll.addWidget(self.export_btn)
        ll.addWidget(self.progress)
        ll.addWidget(self.status)
        ll.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(370)

        self.title = QLabel('Account value by behaviour profile')
        self.title.setStyleSheet('font-size: 16px; font-weight: 600;')
        self.summary = QLabel('')
        self.summary.setWordWrap(True)
        self.equity_plot = make_plot(lambda v: f'${v:,.0f}', 200)
        self.readout = QLabel(' ')
        self.readout.setTextFormat(Qt.RichText)
        self.readout.setWordWrap(True)
        self.readout.setMinimumHeight(38)
        self.readout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.price_title = QLabel('Price')
        self.price_title.setStyleSheet('font-weight: 600;')
        self.lower_view = QComboBox()
        self.lower_view.addItem('Price chart', 'price')
        self.lower_view.addItem('Opposite positioning (mirror image: short when the profile is long, and vice versa)', 'opposite')
        self.lower_view.setToolTip('Opposite positioning: the account of someone who takes the other side of every trade the '
                                   'profile makes (same days, same dollar position), so it gains what the profile loses. '
                                   'A wiped-out profile leaves the opposite side near double the start.')
        self.lower_view.setCurrentIndex(self.lower_view.findData('opposite'))   # default: opposite positioning
        self.lower_view.currentIndexChanged.connect(self._lower_changed)
        self.opp_readout = QLabel('')
        self.opp_readout.setTextFormat(Qt.RichText)
        self.opp_readout.setWordWrap(True)
        self.opp_readout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.opp_readout.setVisible(False)
        self.price_plot = make_plot(price_text, 120)
        self.price_plot.setXLink(self.equity_plot)
        self.cross_eq = Crosshair(self.equity_plot)
        self.cross_px = Crosshair(self.price_plot)
        self.cross_eq.moved.connect(self._hover)
        self.cross_px.moved.connect(self._hover)

        charts = QWidget()
        cv = QVBoxLayout(charts)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.addWidget(self.title)
        cv.addWidget(self.summary)
        cv.addWidget(self.equity_plot, 3)
        cv.addWidget(self.readout)
        lower_row = QHBoxLayout()
        lower_row.addWidget(QLabel('Lower chart'))
        lower_row.addWidget(self.lower_view)
        lower_row.addWidget(self.price_title, 1)
        cv.addLayout(lower_row)
        cv.addWidget(self.price_plot, 2)
        cv.addWidget(self.opp_readout)

        self.results_table = make_table()
        self.results_table.itemSelectionChanged.connect(self._show_trades)
        self.trades_title = QLabel('Trade log')
        self.trades_title.setStyleSheet('font-weight: 600;')
        self.trades_table = make_table()
        res_w = QWidget()
        rl = QVBoxLayout(res_w)
        rl.setContentsMargins(0, 0, 0, 0)
        res_title = QLabel('Results for all profiles (select a row to see its trades)')
        res_title.setStyleSheet('font-weight: 600;')
        rl.addWidget(res_title)
        rl.addWidget(self.results_table)
        tr_w = QWidget()
        tl = QVBoxLayout(tr_w)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.trades_title)
        tl.addWidget(self.trades_table)
        tables = QSplitter(Qt.Horizontal)
        tables.addWidget(res_w)
        tables.addWidget(tr_w)
        tables.setStretchFactor(0, 3)
        tables.setStretchFactor(1, 2)
        tables.setSizes([720, 370])

        split = QSplitter(Qt.Vertical)
        split.addWidget(charts)
        split.addWidget(tables)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([560, 300])

        right = QVBoxLayout()
        right.addWidget(split, 1)
        right.addWidget(disclaimer_label())
        lay = QHBoxLayout(self)
        lay.addWidget(scroll)
        lay.addLayout(right, 1)

    def _add_curve_item(self, name, checked, tip=''):
        item = QListWidgetItem(name)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        item.setToolTip(tip)
        self.curves.addItem(item)
        return item

    def _checked_names(self) -> list:
        return [self.curves.item(i).text() for i in range(self.curves.count())
                if self.curves.item(i).checkState() == Qt.Checked]

    def _curve_toggled(self, item):
        if item.checkState() == Qt.Checked and len(self._checked_names()) > MAX_CURVES:
            self.curves.blockSignals(True)
            item.setCheckState(Qt.Unchecked)
            self.curves.blockSignals(False)
            self.status.setText(f'At most {MAX_CURVES} curves can be drawn at once. Untick one first.')
            return
        if self.report and not self.busy:
            self._draw()

    def _edit_custom(self):
        dlg = QDialog(self)
        dlg.setWindowTitle('Custom profile')
        editor = ProfileEditor(with_switches=False)
        if self.custom_profile:
            editor.set_profile(self.custom_profile)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        note = QLabel('The custom profile is replayed alongside the presets on the next run.')
        note.setWordWrap(True)
        lay = QVBoxLayout(dlg)
        lay.addWidget(editor)
        lay.addWidget(note)
        lay.addWidget(buttons)
        if dlg.exec() != QDialog.Accepted:
            return
        base = editor.profile()
        prof = base.with_switches(name=f'Custom: {base.describe()}')
        self.curves.blockSignals(True)
        if self.custom_profile:
            self.curves.takeItem(self.curves.count() - 1)
        self.custom_profile = prof
        self._add_curve_item(prof.name, len(self._checked_names()) < MAX_CURVES, prof.describe())
        self.curves.blockSignals(False)
        self.custom_btn.setText('Edit the custom profile…')
        self.status.setText('Custom profile added. Run the model to replay it.')

    # ---------- running ----------
    def run(self):
        if self.busy:
            return
        symbol, csv_path, name = self.security.symbol(), self.security.csv_path(), self.security.display_name()
        date = self.date.date().toString('yyyy-MM-dd')
        side, follow = self.side.currentData(), self.follow.currentData()
        horizon, start, trader = self.horizon.currentData(), self.start.value(), self.trader.value()
        switches = self.switches.values()
        profiles = [p.with_switches(**switches) for p in engine.PRESETS + ([self.custom_profile] if self.custom_profile else [])]
        cache_dir = self.cache_dir

        def job(progress):
            series = data.get_prices(symbol, csv_path, cache_dir, force=True)   # always live from Yahoo unless a CSV is loaded
            i0 = entry_index(series.dates, date)
            rep = engine.run_report(series.closes, series.dates, i0, side, follow, horizon, profiles, progress=progress, trader=trader)
            source = {'yahoo': 'Live prices from Yahoo Finance', 'csv': 'Prices from the CSV file',
                      'cache': 'Saved prices (Yahoo could not be reached)'}[series.source]
            note = f'{series.note} ' if series.note else ''
            return dict(rep=rep, name=name, symbol=series.symbol, start=start, source=series.source,
                        note=f'{note}{source}; latest completed session {series.dates[-1]}.')

        self._set_busy(True, f'Loading {name} prices and replaying {len(profiles)} profiles…')
        run_async(job, self._done, self._failed, self._progress)

    def _set_busy(self, busy, text=None):
        self.busy = busy
        self.run_btn.setEnabled(not busy)
        self.export_btn.setEnabled(not busy and self.report is not None)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
        if text is not None:
            self.status.setStyleSheet('')
            self.status.setText(text)

    def _progress(self, done, total):
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def _failed(self, message):
        self._set_busy(False)
        self.status.setStyleSheet(f'color: {theme()["critical"]}; font-weight: 600;')
        self.status.setText(f'Could not run: {message}')

    def _done(self, result):
        self.report = result
        self._set_busy(False, result['note'] or 'Done.')
        self._draw()
        self._fill_results()

    # ---------- drawing ----------
    def _shown(self) -> list:
        """(result, colour) for each ticked profile, colours in fixed slot order."""
        names = self._checked_names()
        colors = theme()['series']
        picked = [r for r in self.report['rep']['results'] if r['name'] in names][:MAX_CURVES]
        return [(r, colors[n]) for n, r in enumerate(picked)]

    def _draw(self):
        t = theme()
        rep, S, name = self.report['rep'], self.report['start'], self.report['name']
        dates, price = rep['dates'], rep['price']
        side = side_text(rep['direction'])
        rnd = rep['follow'] == 'random'
        follow = {'same': 'same side as the entry', 'trend': 'following the 20-day trend',
                  'random_one': f'random long/short, one trader (number {rep["trader"]})',
                  'random': f'random long/short, median of {rep["runs"]} runs'}[rep['follow']]
        extras = ', '.join(s.lower() for s in rep['results'][0]['profile'].switch_names()) or 'none'
        self.title.setText(f'{name}: {side.lower()} from {dates[0]} — account value by behaviour profile')
        self.summary.setText(
            f'{len(dates) - 1} trading days replayed, {dates[0]} to {dates[-1]}. Price moved {price[-1] / price[0] - 1:+.1%}. '
            f'Later trades: {follow}. Extra behaviours: {extras}. Starting account {money(S)}.'
            + (' Each line is the median account across the runs; the first trade is the same in every run.' if rnd else ''))

        eq = self.equity_plot
        eq.clear()
        set_dates(eq, dates)                     # the legend is the readout line under the chart (name, colour, value)
        base = pg.InfiniteLine(pos=S, angle=0, movable=False, pen=pg.mkPen(t['muted'], width=1, style=Qt.DashLine),
                               label=f'Start {money(S)}', labelOpts=dict(position=0.97, color=t['muted'], anchors=[(1, 1), (1, 1)]))
        eq.addItem(base)
        xs = list(range(len(dates)))
        for r, col in self._shown():
            eq.plot(xs, [v * S for v in r['curve']], pen=pg.mkPen(col, width=2), name=r['name'])
        self.cross_eq.attach(len(dates))
        eq.enableAutoRange()

        self._draw_lower()
        self._hover(-1)

    def _lower_changed(self, *_):
        if self.report and not self.busy:
            self._draw_lower()
            self._hover(-1)

    def _opposite_mode(self) -> bool:
        return self.lower_view.currentData() == 'opposite'

    def _draw_lower(self):
        """Lower chart: the security's price, or the account of the exact opposite positioning."""
        t = theme()
        rep, S, name = self.report['rep'], self.report['start'], self.report['name']
        dates, price = rep['dates'], rep['price']
        xs = list(range(len(dates)))
        px = self.price_plot
        px.clear()
        set_dates(px, dates)
        opposite = self._opposite_mode()
        self.opp_readout.setVisible(opposite)
        if opposite:
            px.getAxis('left').fmt = lambda v: f'${v:,.0f}'
            median = ' (median of the runs)' if rep['follow'] == 'random' else ''
            self.price_title.setText(f'Account value if you took the opposite side of every trade{median}')
            px.addItem(pg.InfiniteLine(pos=S, angle=0, movable=False, pen=pg.mkPen(t['muted'], width=1, style=Qt.DashLine),
                                       label=f'Start {money(S)}',
                                       labelOpts=dict(position=0.97, color=t['muted'], anchors=[(1, 1), (1, 1)])))
            for r, col in self._shown():
                px.plot(xs, [v * S for v in r['opp_curve']], pen=pg.mkPen(col, width=2))
        else:
            px.getAxis('left').fmt = price_text
            self.price_title.setText(f'{name} daily close, with your entry marked')
            px.plot(xs, price, pen=pg.mkPen(t['secondary'], width=2))
            side = side_text(rep['direction'])
            mark = pg.ScatterPlotItem([0], [price[0]], symbol='t1' if rep['direction'] > 0 else 't', size=14,
                                      brush=pg.mkBrush(t['series'][0]), pen=pg.mkPen(t['surface'], width=2))
            px.addItem(mark)
            label = pg.TextItem(f'Entry: {side.lower()} at {price_text(price[0])}', color=t['ink'], anchor=(0, 1.3))
            label.setPos(0, price[0])
            px.addItem(label, ignoreBounds=True)
        self.cross_px.attach(len(dates))
        px.enableAutoRange()

    def _hover(self, i):
        self.cross_eq.show_at(i)
        self.cross_px.show_at(i)
        if not self.report:
            return
        rep, S = self.report['rep'], self.report['start']
        if i < 0:
            i = len(rep['dates']) - 1
            lead = f'Latest ({rep["dates"][i]})'
        else:
            lead = rep['dates'][i]
        parts = [f'<b>{lead}</b>'] + [f'{swatch(col)} {html.escape(r["name"])} <b>{money(r["curve"][i] * S)}</b>'
                                       for r, col in self._shown()]
        parts.append(f'Close <b>{price_text(rep["price"][i])}</b>')
        self.readout.setText(' &nbsp;·&nbsp; '.join(parts))
        if self._opposite_mode():
            dd = 'median max drawdown' if rep['follow'] == 'random' else 'max drawdown'
            opp = [f'<b>Opposite side, {lead}</b>'] + [
                f'{swatch(col)} {html.escape(r["name"])} <b>{money(r["opp_curve"][i] * S)}</b> ({dd} <b>{r["opp_dd"]:.1%}</b>)'
                for r, col in self._shown()]
            self.opp_readout.setText(' &nbsp;·&nbsp; '.join(opp))

    def _fill_results(self):
        rep, S = self.report['rep'], self.report['start']
        rnd = rep['follow'] == 'random'
        rows = []
        for r in rep['results']:
            ft = r['first']
            first = f'{engine.reason_label(ft.reason, ft.stop_moved)} on {ft.exit_date}'
            if rnd:
                rows.append([r['name'], first, f'{ft.move:+.2%}', f'{r["ntr"]:.0f}', money(r['final'] * S),
                             f'{r["lose"]:.0%}', f'{r["wiped"]:.0%}', money(r['opp_final'] * S), f'{r["opp_dd"]:.1%}'])
            else:
                rows.append([r['name'], first, f'{ft.move:+.2%}', r['ntr'], r['wins'], money(r['final'] * S),
                             f'{r["final"] - 1:+.1%}', f'{r["dd"]:.1%}', 'Yes' if r['wiped'] else 'No',
                             money(r['opp_final'] * S), f'{r["opp_final"] - 1:+.1%}', f'{r["opp_dd"]:.1%}'])
        if rnd:
            heads = ['Profile', 'Your first trade ended', 'First-trade move', 'Trades (average)', 'Median final account',
                     'Runs losing', 'Runs wiped out', 'Opposite: median final', 'Opposite: median max drawdown']
            right = (2, 3, 4, 5, 6, 7, 8)
        else:
            heads = ['Profile', 'Your first trade ended', 'First-trade move', 'Trades', 'Wins', 'Final account', 'Result',
                     'Worst drawdown', 'Wiped out', 'Opposite: final', 'Opposite: result', 'Opposite: max drawdown']
            right = (2, 3, 4, 5, 6, 7, 9, 10, 11)
        fill_table(self.results_table, heads, rows, right)
        self.results_table.selectRow(0)
        self._show_trades()

    def _show_trades(self):
        if not self.report:
            return
        rep, S = self.report['rep'], self.report['start']
        row = max(self.results_table.currentRow(), 0)
        r = rep['results'][min(row, len(rep['results']) - 1)]
        heads = ['Entry', 'Exit', 'Side', 'Size', 'Lots', 'Move', 'Account after', 'Exit reason']
        if r['trades'] is None:
            self.trades_title.setText(f'Trade log: {r["name"]} — not shown for random runs (each of the {rep["runs"]} runs differs)')
            fill_table(self.trades_table, heads, [])
            return
        self.trades_title.setText(f'Trade log: {r["name"]} — {r["ntr"]} trade{"s" if r["ntr"] != 1 else ""}')
        fill_table(self.trades_table, heads, [self._trade_row(t, S) for t in r['trades']], (3, 4, 5, 6))

    @staticmethod
    def _trade_row(t, S):
        return [t.entry_date, t.exit_date, side_text(t.direction), f'{t.size:g}x', t.lots, f'{t.move:+.2%}',
                money(t.account_after * S), engine.reason_label(t.reason, t.stop_moved)]

    # ---------- export ----------
    def export(self):
        if not self.report:
            return
        rep = self.report['rep']
        safe = ''.join(ch if ch.isalnum() else '_' for ch in self.report['symbol'])
        default = f'report_{safe}_{rep["dates"][0]}_{"long" if rep["direction"] > 0 else "short"}.html'
        path, _ = QFileDialog.getSaveFileName(self, 'Export report', default, 'HTML files (*.html)')
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(self.report_html())
        except OSError as ex:
            self.status.setStyleSheet(f'color: {theme()["critical"]}; font-weight: 600;')
            self.status.setText(f'Could not save the report: {ex}')
            return
        self.status.setStyleSheet('')
        self.status.setText(f'Report saved to {path}')

    @staticmethod
    def _png(widget) -> str:
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        widget.grab().save(buf, 'PNG')
        return base64.b64encode(bytes(QByteArray(buf.data()))).decode('ascii')

    def report_html(self) -> str:
        """Self-contained HTML: summary, both charts as embedded PNGs, results and trade logs."""
        e = html.escape
        rep, S = self.report['rep'], self.report['start']
        self._hover(-1)
        t = self.results_table
        heads = [t.horizontalHeaderItem(c).text() for c in range(t.columnCount())]
        num = lambda c: ' class=n' if c >= 2 and heads[c] != 'Wiped out' else ''
        head_row = ''.join(f'<th{num(c)}>{e(h)}</th>' for c, h in enumerate(heads))
        body = ''.join('<tr>' + ''.join(f'<td{num(c)}>{e(t.item(r, c).text())}</td>' for c in range(t.columnCount())) + '</tr>'
                       for r in range(t.rowCount()))
        logs = ''
        if rep['follow'] != 'random':
            th = ''.join(f'<th{" class=n" if i in (3, 4, 5, 6) else ""}>{h}</th>' for i, h in enumerate(
                ['Entry', 'Exit', 'Side', 'Size', 'Lots', 'Move', 'Account after', 'Exit reason']))
            for r in rep['results']:
                trs = ''.join('<tr>' + ''.join(f'<td{" class=n" if i in (3, 4, 5, 6) else ""}>{e(str(v))}</td>'
                                               for i, v in enumerate(self._trade_row(tr, S))) + '</tr>' for tr in r['trades'])
                logs += f'<details><summary>{e(r["name"])} — {r["ntr"]} trades</summary><table><tr>{th}</tr>{trs}</table></details>'
            logs = f'<div class=card><h2>Trade logs</h2>{logs}</div>'
        legend = ' &nbsp; '.join(f'<span style="color:{col}">&#9632;</span> {e(r["name"])}' for r, col in self._shown())
        css = ('body{font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;'
               'color:#0b0b0b;background:#f9f9f7}.card{background:#fcfcfb;border:1px solid #e1e0d9;border-radius:10px;padding:16px;'
               'margin:14px 0}h1{font-size:24px;margin:0}h2{font-size:17px;margin:0 0 6px}img{width:100%;height:auto;display:block}'
               'table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid #eee;padding:5px 6px;'
               'text-align:left}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}.mut{color:#52514e;font-size:13px}'
               'details{margin-top:8px}summary{cursor:pointer;font-size:13px}')
        return (f'<!doctype html><meta charset=utf-8><title>{e(self.report["name"])} behaviour report</title><style>{css}</style>'
                f'<h1>{e(self.title.text())}</h1><p class=mut>{e(self.summary.text())}</p>'
                f'<div class=card><h2>Account value by behaviour profile</h2><p class=mut>{legend}</p>'
                f'<img alt="Account value curves" src="data:image/png;base64,{self._png(self.equity_plot)}"></div>'
                f'<div class=card><h2>{e(self.price_title.text())}</h2>'
                f'{"<p class=mut>" + self.opp_readout.text() + "</p>" if self._opposite_mode() else ""}'
                f'<img alt="Lower chart" src="data:image/png;base64,{self._png(self.price_plot)}"></div>'
                f'<div class=card><h2>Results</h2><table><tr>{head_row}</tr>{body}</table><p class=mut>Wiped out = account at or '
                f'below 20% of its starting value. Exits are taken at daily closes; costs are 2 basis points per side.</p></div>'
                f'{logs}<p class=mut><b>{e(DISCLAIMER)}</b></p>')
