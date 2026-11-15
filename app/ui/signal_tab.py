"""Signal tab: run once a day; shows what the chosen profile does today with the open position."""
from __future__ import annotations

import html

import pyqtgraph as pg
from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDateEdit, QDoubleSpinBox, QSpinBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from .. import data, engine
from .. import state as state_io
from .widgets import (ProfileEditor, SecurityPicker, disclaimer_label, fill_table, make_plot, make_table, money,
                      price_text, run_async, set_dates, side_text, theme)

FLAT_CHART_DAYS = 60
FOLLOW = [('same', 'Same side as my first entry'), ('trend', 'Follow the 20-day trend'),
          ('chase', f'Chase the last {engine.CHASE_DAYS} days'), ('random_one', 'Random long/short (trader number below)'),
          ('manual', 'I choose every entry myself (cannot be checked against the model)')]
FOLLOW_TEXT = {'same': 'same side as the first entry', 'trend': 'the 20-day trend', 'chase': f'the last {engine.CHASE_DAYS} days',
               'random_one': 'random long/short'}


def close_detail(ev: engine.Event, profile: engine.Profile) -> str:
    d = ev.data
    r = d['move']
    return {
        engine.TARGET: f'up {r:.2%}, target is +{profile.tp:.2%}',
        engine.GAVE_BACK: f'was up {d["peak"]:.2%}, now {r:+.2%}',
        engine.PANIC: f'{-d["day"]:.2%} against you in one day while losing',
        engine.STOP: f'down {-r:.2%}, stop is -{(d["stop"] or 0):.2%}',
        engine.TIME: f'held {engine.MAX_HOLD} trading days',
        engine.MARGIN: 'account marked to market is at or below 20% of its starting value',
        engine.BREAK_EVEN: f'was down {-d.get("trough", 0):.2%}, now back to {r:+.2%}',
    }.get(ev.reason, '')


def event_text(ev: engine.Event, profile: engine.Profile) -> str:
    """One plain sentence for an event, used in the list under the headline."""
    d = ev.data
    if ev.kind == engine.ADD_LOT:
        return (f'<b>ADD TO POSITION</b>. Down {-d["first_move"]:.2%} from the first entry. The model adds another lot of '
                f'{d["base"]:g}x account ({money(d["lot_value"])}) at {price_text(d["close"])}. Position is now {d["lots"]} lots; '
                f'new average entry {price_text(d["average_entry"])}.')
    if ev.kind == engine.STOP_WIDENED:
        return (f'<b>STOP WIDENED</b>. The stop of -{d["old_stop"]:.2%} was hit (down {-d["move"]:.2%}). '
                f'The model stays in and widens the stop to -{d["new_stop"]:.2%}.')
    if ev.kind == engine.MARGIN_CALL:
        return '<b>MARGIN CALL</b>. The account marked to market fell to or below 20% of its starting value.'
    if ev.kind == engine.CLOSE:
        label = engine.reason_label(ev.reason, d.get('stop_moved', False))
        return f'<b>CLOSE</b>. {label}: {close_detail(ev, profile)}. Closed at {price_text(d["close"])}.'
    if ev.kind == engine.OPEN:
        return (f'<b>RE-ENTER {side_text(d["direction"]).upper()}</b>. The model opens its next trade at that close, '
                f'{price_text(d["price"])}: {d["base"]:g}x account ({money(d["value"])}).')
    if ev.kind == engine.TOP_UP:
        return (f'<b>TOP UP</b>. The account was wiped out ({money(d["left"], True)} left). The model deposits another '
                f'{money(d["deposit"], True)} (top-up {d["number"]} of {engine.MAX_TOPUPS}) and carries on with {money(d["account"], True)}.')
    if ev.kind == engine.STOP_TRADING:
        return '<b>STOP TRADING</b>. Account is at or below 20% of its starting value.'
    return ''


class SignalTab(QWidget):
    def __init__(self, data_dir: str, cache_dir: str, status_message=None, parent=None):
        super().__init__(parent)
        self.data_dir, self.cache_dir = data_dir, cache_dir
        self.status_message = status_message or (lambda text: None)
        self.doc = None
        self.series = None
        self.busy = False
        self._build()
        try:
            self.doc = state_io.load(self.data_dir)
        except ValueError as ex:
            self.setup_error.setText(str(ex))
        self._show_page()
        if self.doc:
            QTimer.singleShot(0, self.update_now)        # runs automatically on launch

    # ---------- layout ----------
    def _build(self):
        t = theme()
        self.stack = QStackedWidget()

        # setup page
        self.setup_start = QDoubleSpinBox(decimals=0, minimum=100, maximum=1e9, singleStep=1000, prefix='$ ')
        self.setup_start.setGroupSeparatorShown(True)
        self.setup_start.setValue(10000)
        self.setup_security = SecurityPicker()
        self.setup_profile = ProfileEditor(with_switches=True)
        self.setup_profile.set_profile(next(p for p in engine.PRESETS if p.name == 'All combined'))
        self.setup_error = QLabel('')
        self.setup_error.setWordWrap(True)
        self.setup_error.setStyleSheet(f'color: {t["critical"]}; font-weight: 600;')
        start_btn = QPushButton('Start tracking')
        start_btn.clicked.connect(self._start)
        box = QGroupBox('Set up the daily signal')
        form = QFormLayout(box)
        intro = QLabel('Choose the account, security and behaviour profile to follow. You can then record a position and '
                       'press Update once a day to see what this profile would do with it.')
        intro.setWordWrap(True)
        form.addRow(intro)
        form.addRow('Starting account', self.setup_start)
        form.addRow('Security', self.setup_security)
        form.addRow(self.setup_profile)
        self.setup_follow = QComboBox()
        for key, label in FOLLOW:
            self.setup_follow.addItem(label, key)
        self.setup_follow.setToolTip('The model re-enters at the close of the day a trade ends. With any rule except the last, '
                                     'the Signal does the same automatically, so it matches a Report backtest exactly.')
        self.setup_trader = QSpinBox(minimum=1, maximum=engine.MC_RUNS, value=1)
        self.setup_trader.setEnabled(False)
        self.setup_follow.currentIndexChanged.connect(
            lambda *_: self.setup_trader.setEnabled(self.setup_follow.currentData() == 'random_one'))
        form.addRow('Later trades', self.setup_follow)
        form.addRow('Trader number', self.setup_trader)
        form.addRow(self.setup_error)
        form.addRow(start_btn)
        box.setMaximumWidth(620)
        setup = QWidget()
        sl = QVBoxLayout(setup)
        sl.addWidget(box, 0, Qt.AlignTop | Qt.AlignHCenter)
        sl.addStretch(1)
        sl.addWidget(disclaimer_label())
        self.stack.addWidget(setup)

        # main page
        self.context = QLabel('')
        self.context.setWordWrap(True)
        self.update_btn = QPushButton('Update')
        self.update_btn.setToolTip('Download the latest closes and apply every completed session not yet processed')
        self.update_btn.clicked.connect(self.update_now)
        self.reset_btn = QPushButton('Reset…')
        self.reset_btn.clicked.connect(self._reset)
        top = QHBoxLayout()
        top.addWidget(self.context, 1)
        top.addWidget(self.update_btn)
        top.addWidget(self.reset_btn)

        self.card = QFrame()
        self.card.setObjectName('card')
        self.headline = QLabel('')
        self.headline.setStyleSheet('font-size: 30px; font-weight: 700;')
        self.headline.setWordWrap(True)
        self.detail = QLabel('')
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.RichText)
        self.detail.setStyleSheet('font-size: 14px;')
        self.events = QLabel('')
        self.events.setWordWrap(True)
        self.events.setTextFormat(Qt.RichText)
        cl = QVBoxLayout(self.card)
        cl.addWidget(self.headline)
        cl.addWidget(self.detail)
        cl.addWidget(self.events)

        self.error = QLabel('')
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f'color: {t["critical"]}; font-weight: 600;')
        self.error.setVisible(False)

        self.open_box = QGroupBox('Open a position')
        self.open_date = QDateEdit(QDate.currentDate())
        self.open_date.setCalendarPopup(True)
        self.open_date.setDisplayFormat('yyyy-MM-dd')
        self.open_side = QComboBox()
        self.open_side.addItem('Long', 'long')
        self.open_side.addItem('Short', 'short')
        self.open_price = QLineEdit()
        self.open_price.setPlaceholderText("blank = that day's close")
        self.open_price.setMinimumWidth(190)
        self.open_price.setMaximumWidth(220)
        self.open_btn = QPushButton('Open position')
        self.open_btn.clicked.connect(self._open)
        ol = QHBoxLayout(self.open_box)
        for label, w in (('Entry date', self.open_date), ('Side', self.open_side), ('Actual entry price (optional)', self.open_price)):
            ol.addWidget(QLabel(label))
            ol.addWidget(w)
        ol.addWidget(self.open_btn)
        ol.addStretch(1)

        self.stats = QLabel('')
        self.stats.setTextFormat(Qt.RichText)
        self.stats.setStyleSheet('font-size: 14px;')
        self.chart_title = QLabel('')
        self.chart_title.setStyleSheet('font-weight: 600;')
        self.plot = make_plot(price_text, 190)
        self.plot.setMouseEnabled(x=False, y=False)
        hist_title = QLabel('Closed trades (newest first)')
        hist_title.setStyleSheet('font-weight: 600;')
        self.history = make_table()
        self.history.setMinimumHeight(110)

        main = QWidget()
        ml = QVBoxLayout(main)
        ml.addLayout(top)
        ml.addWidget(self.card)
        ml.addWidget(self.error)
        ml.addWidget(self.open_box)
        ml.addWidget(self.stats)
        self.align = QLabel('')
        self.align.setWordWrap(True)
        self.align.setTextFormat(Qt.RichText)
        ml.addWidget(self.align)
        ml.addWidget(self.chart_title)
        ml.addWidget(self.plot, 3)
        ml.addWidget(hist_title)
        ml.addWidget(self.history, 2)
        ml.addWidget(disclaimer_label())
        self.stack.addWidget(main)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.stack)

    def _show_page(self):
        self.stack.setCurrentIndex(1 if self.doc else 0)
        if self.doc:
            self._render()

    # ---------- actions ----------
    def _save(self):
        try:
            state_io.save(self.data_dir, self.doc)
        except OSError as ex:
            self._error(f'Could not save the state file: {ex}')

    def _error(self, text):
        self.error.setText(text)
        self.error.setVisible(bool(text))

    def _start(self):
        try:
            csv_path = self.setup_security.csv_path()
            symbol = self.setup_security.display_name() if csv_path else self.setup_security.symbol()
            self.doc = state_io.new_doc(self.setup_start.value(), symbol, self.setup_profile.profile(), csv_path,
                                        self.setup_follow.currentData(), self.setup_trader.value())
        except ValueError as ex:
            self.setup_error.setText(str(ex))
            return
        self.setup_error.setText('')
        self.series = None
        self._save()
        self._show_page()
        self.update_now()

    def _reset(self):
        answer = QMessageBox.question(
            self, 'Reset the signal tracker',
            'This erases the account, the open position and the closed-trade history.\n\nReset and start again?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            state_io.reset(self.data_dir)
        except OSError as ex:
            self._error(f'Could not delete the state file: {ex}')
            return
        self.doc = None
        self.series = None
        self._error('')
        self._show_page()

    def update_now(self):
        """Fetch closes on a worker thread, then apply every unprocessed completed session once."""
        if self.busy or not self.doc:
            return
        symbol, csv_path, cache_dir = self.doc.symbol, self.doc.csv_path, self.cache_dir
        self.busy = True
        self.update_btn.setEnabled(False)
        self.open_btn.setEnabled(False)
        self.status_message('Updating prices…')
        run_async(lambda progress: data.get_prices(symbol, csv_path, cache_dir, force=True), self._prices_loaded, self._prices_failed)

    def _finish_busy(self):
        self.busy = False
        self.update_btn.setEnabled(True)
        self.open_btn.setEnabled(True)

    def _prices_failed(self, message):
        self._finish_busy()
        self.status_message('')
        self._error(f'Could not get prices: {message}')

    def _prices_loaded(self, series):
        self._finish_busy()
        if not self.doc:
            return
        self.series = series
        self._error(series.note)
        before = self.doc.trader.last_date
        engine.process_sessions(self.doc.trader, self.doc.profile, series.rows, self.doc.follow())
        self._save()
        latest = series.rows[-1][0]
        self.status_message(f'Latest completed session: {latest}.' + ('' if before != latest else ' Nothing new to apply.'))
        self.open_date.setDate(QDate.fromString(latest, 'yyyy-MM-dd'))
        self._render()

    def _open(self):
        if not self.series:
            self._error('Prices are not loaded yet. Press Update first.')
            return
        tr, closes = self.doc.trader, dict(self.series.rows)
        date = self.open_date.date().toString('yyyy-MM-dd')
        latest = self.series.rows[-1][0]
        text = self.open_price.text().strip().replace(',', '')
        try:
            if date not in closes:
                raise ValueError(f'{date} is not a completed trading day in the data (latest is {latest}).')
            if tr.history and date < tr.history[-1].exit_date:
                raise ValueError(f'The entry date cannot be before the last trade closed ({tr.history[-1].exit_date}).')
            try:
                price = float(text) if text else closes[date]
            except ValueError:
                raise ValueError(f'"{text}" is not a price. Leave it blank to use the close of {date}.') from None
            first_trade = not tr.history
            engine.open_trade(tr, self.doc.profile, price, self.open_side.currentData(), date)
            if first_trade:
                self.doc.first_direction = tr.position.direction       # the model's later sides are measured from this
        except ValueError as ex:
            self._error(str(ex))
            return
        self._error(self.series.note)
        self.open_price.clear()
        engine.process_sessions(tr, self.doc.profile, self.series.rows, self.doc.follow())   # entry before the latest session: catch up now
        self._save()
        self._render()

    # ---------- rendering ----------
    def _render(self):
        doc = self.doc
        tr, prof, t = doc.trader, doc.profile, theme()
        e = html.escape
        name = data.security_name(doc.symbol) if not doc.csv_path else doc.symbol
        if self.series:
            last_d, last_c = self.series.rows[-1]
            latest = f'latest completed session: <b>{last_d}</b>, close {price_text(last_c)}'
        else:
            latest = 'prices not loaded yet'
        src = ' (from CSV file)' if doc.csv_path else (' (live from Yahoo Finance)' if self.series and self.series.source == 'yahoo'
                                                       else (' (saved prices, Yahoo not reached)' if self.series else ''))
        rule = dict(FOLLOW)[doc.follow_rule].split(' (')[0] + (f', trader {doc.trader_number}' if doc.follow_rule == 'random_one' else '')
        self.context.setText(f'<b>{e(prof.name)}</b> ({e(prof.describe())}) · later trades: {e(rule.lower())} · {e(name)}{src} · {latest}')

        events = tr.last_events
        close_ev = next((ev for ev in events if ev.kind == engine.CLOSE), None)
        pos = tr.position
        lines, listed, accent = [], [], t['series'][0]
        if pos is not None:
            today = [ev for ev in events if ev.date == tr.last_date]
            add = next((ev for ev in today if ev.kind == engine.ADD_LOT), None)
            wid = next((ev for ev in today if ev.kind == engine.STOP_WIDENED), None)
            n, move = len(pos.lots), engine.current_move(pos)
            reopened = next((ev for ev in today if ev.kind == engine.OPEN), None)
            closed_today = next((ev for ev in today if ev.kind == engine.CLOSE), None)
            if reopened and closed_today:
                d = closed_today.data
                head, accent = f'CLOSE, THEN RE-ENTER {side_text(pos.direction).upper()}', t['critical']
                label = engine.reason_label(closed_today.reason, d.get('stop_moved', False))
                lines.append(f'<b>{label}</b>: {close_detail(closed_today, prof)}. Close at {price_text(d["close"])}. '
                             f'Account is now <b>{money(d["account_after"], True)}</b> (was {money(d["account_before"], True)}).')
                top = next((ev for ev in today if ev.kind == engine.TOP_UP), None)
                if top:
                    lines.append(f'<b>Top up:</b> that is a wipe-out, so the model deposits another {money(top.data["deposit"], True)} '
                                 f'(top-up {top.data["number"]} of {engine.MAX_TOPUPS}); account {money(top.data["account"], True)}.')
                lines.append(f'<b>Then open the next trade at the same close</b>, as the model does '
                             f'({FOLLOW_TEXT.get(doc.follow_rule, "")}): {side_text(pos.direction).lower()}, '
                             f'{e(engine.sizing_note(tr, prof))}.')
            elif add:
                head, accent = 'ADD TO POSITION', t['warning']
                d = add.data
                lines.append(f'Averaging down: the close is {-d["first_move"]:.2%} against the first entry. Add another lot of '
                             f'<b>{d["base"]:g}x account ({money(d["lot_value"])})</b> at {price_text(d["close"])}. '
                             f'New average entry: <b>{price_text(d["average_entry"])}</b> over {d["lots"]} lots.')
                if wid:
                    lines.append(f'Also today: the stop was hit and widened to -{wid.data["new_stop"]:.2%}. Stay in.')
            elif wid:
                head, accent = 'STOP WIDENED: STAY IN', t['warning']
                d = wid.data
                lines.append(f'The stop of -{d["old_stop"]:.2%} was hit (down {-d["move"]:.2%}). The model does not exit: it widens the '
                             f'stop to -{d["new_stop"]:.2%}, now at <b>{price_text(engine.price_level(pos, -pos.stop))}</b>. This happens once per trade.')
            else:
                head = 'HOLD'
            when = 'not yet updated' if pos.days_held == 0 else f'after {pos.days_held} trading day{"s" if pos.days_held != 1 else ""}'
            lines.append(f'{side_text(pos.direction)} from {pos.entry_date} at {price_text(pos.lots[0])}: {n} lot{"s" if n > 1 else ""} of '
                         f'{pos.base:g}x account = {pos.size:g}x ({money(pos.size * tr.account)}). Current move <b>{move:+.2%}</b> {when}; '
                         f'on the account {pos.size * move:+.2%}.')
            stop = (f'stop-loss at <b>{price_text(engine.price_level(pos, -pos.stop))}</b> (-{pos.stop:.2%}'
                    f'{", already widened" if pos.moved else ""})') if pos.stop is not None else 'no stop-loss'
            lines.append(f'Close when a daily close reaches: take-profit at <b>{price_text(engine.price_level(pos, prof.tp))}</b> '
                         f'(+{prof.tp:.2%}), {stop}.')
            listed = [ev for ev in events if ev.date != tr.last_date]
            list_title = 'Applied on days since your last update:'
        else:
            list_title = 'Before that, while the position was open:'
            if close_ev:
                d = close_ev.data
                head, accent = 'CLOSE THE POSITION', t['critical']
                label = engine.reason_label(close_ev.reason, d.get('stop_moved', False))
                lines.append(f'<b>{label}</b>: {close_detail(close_ev, prof)}. Model exit: {close_ev.date} close {price_text(d["close"])}.')
                if close_ev.date < (tr.last_date or ''):
                    lines.append(f'<b>This triggered on {close_ev.date}, before the latest session ({tr.last_date})</b>; '
                                 f'the model books it at that day\'s close.')
                lines.append(f'Account is now <b>{money(d["account_after"], True)}</b> (was {money(d["account_before"], True)}).')
                top = next((ev for ev in events if ev.kind == engine.TOP_UP), None)
                if top:
                    lines.append(f'<b>Top up:</b> that is a wipe-out, so the model deposits another {money(top.data["deposit"], True)} '
                                 f'(top-up {top.data["number"]} of {engine.MAX_TOPUPS}). Account is now <b>{money(top.data["account"], True)}</b>; '
                                 f'total money put in {money(top.data["total_in"], True)}.')
                listed = [ev for ev in events if ev.kind in (engine.ADD_LOT, engine.STOP_WIDENED, engine.MARGIN_CALL)]
            else:
                head, accent = 'NO OPEN POSITION', t['good']
            if tr.stopped:
                head, accent = ('CLOSE, THEN STOP TRADING' if close_ev else 'STOP TRADING'), t['critical']
                lines.append(f'<b>Stop trading.</b> The account is at or below 20% of its starting value '
                             f'({money(tr.ruin, True)}). The model ends here.')
            else:
                lines.append(f'Next trade size: <b>{e(engine.sizing_note(tr, prof))}</b>')
        self.headline.setText(head)
        self.detail.setText('<br>'.join(lines))
        self.events.setText(f'<b>{list_title}</b><br>' + '<br>'.join(f'{ev.date}: {event_text(ev, prof)}' for ev in listed) if listed else '')
        self.events.setVisible(bool(listed))
        self.card.setStyleSheet(f'#card {{ border: 1px solid {t["grid"]}; border-left: 8px solid {accent}; border-radius: 8px; '
                                f'background: {t["surface"]}; }}')
        self.open_box.setVisible(pos is None and not tr.stopped)
        self.open_box.setTitle('Open your first position' if not tr.history else 'Open a position')
        follow = doc.follow()
        if doc.follow_rule == 'manual':
            self.align.setText('Model check: not available, because you choose every entry yourself. Set "Later trades" to a model '
                               'rule at setup if the Signal must match a backtest.')
            self.align.setStyleSheet(f'color: {t["secondary"]};')
        elif follow is None or not self.series:
            self.align.setText(f'Model check: later trades follow {FOLLOW_TEXT[doc.follow_rule]}. It starts after your first entry.')
            self.align.setStyleSheet(f'color: {t["secondary"]};')
        else:
            ok, message = engine.check_alignment(tr, prof, self.series.rows, follow)
            mark = {True: '&#10003; Model check passed.', False: '&#10007; MODEL CHECK FAILED.', None: 'Model check:'}[ok]
            self.align.setText(f'<b>{mark}</b> {e(message)}')
            self.align.setStyleSheet(f'color: {t["critical"]}; font-weight: 600;' if ok is False else f'color: {t["secondary"]};')

        value = engine.mark_to_market(tr)
        open_note = ' including the open position' if pos is not None else ''
        self.stats.setText(f'Account <b>{money(value, True)}</b>{open_note} ({value / tr.total_in - 1:+.1%} on the {money(tr.total_in, True)} put in'
                           f'{f", including {tr.topups} top-up deposit" + ("s" if tr.topups > 1 else "") if tr.topups else ""}) · Wins in a row: <b>{tr.wins_row}</b> · '
                           f'Size multiplier for the next trade: <b>{tr.mult:g}x</b>')
        fill_table(self.history, ['Entry', 'Exit', 'Side', 'Size', 'Lots', 'Move', 'Account after', 'Exit reason'],
                   [[h.entry_date, h.exit_date, side_text(h.direction), f'{h.size:g}x', h.lots, f'{h.move:+.2%}',
                     money(h.account_after, True), engine.reason_label(h.reason, h.stop_moved)] for h in reversed(tr.history)],
                   (3, 4, 5, 6))
        self._draw_chart()

    def _draw_chart(self):
        t, tr, prof = theme(), self.doc.trader, self.doc.profile
        plot, pos = self.plot, tr.position
        plot.clear()
        if not self.series:
            self.chart_title.setText('Price chart appears after the first update')
            return
        rows = self.series.rows
        dates = [d for d, _ in rows]
        if pos is not None and pos.entry_date in dates:
            i0 = dates.index(pos.entry_date)
            self.chart_title.setText(f'Price since entry ({pos.entry_date}), with entry, lots, target and stop')
        else:
            i0 = max(0, len(rows) - FLAT_CHART_DAYS)
            self.chart_title.setText(f'Daily close, last {len(rows) - i0} sessions')
        seg = rows[i0:]
        set_dates(plot, [d for d, _ in seg])
        ys = [c for _, c in seg]
        plot.plot(list(range(len(seg))), ys, pen=pg.mkPen(t['secondary'], width=2), symbol='o' if len(seg) < 40 else None,
                  symbolSize=6, symbolBrush=t['secondary'], symbolPen=pg.mkPen(t['surface'], width=1))
        levels = list(ys)
        if pos is not None:
            def hline(y, text, color, style=Qt.SolidLine, side=0.02):
                anchor = (0, 1) if side < 0.5 else (1, 1)
                plot.addItem(pg.InfiniteLine(pos=y, angle=0, movable=False, pen=pg.mkPen(color, width=2, style=style),
                                             label=text, labelOpts=dict(position=side, color=t['ink'], anchors=[anchor, anchor])))
                levels.append(y)
            hline(pos.lots[0], f'Entry {price_text(pos.lots[0])}', t['series'][0])
            for n, lot in enumerate(pos.lots[1:], start=2):
                hline(lot, f'Lot {n} {price_text(lot)}', t['series'][0], Qt.DotLine, 0.35)
            if len(pos.lots) > 1:
                avg = engine.average_entry(pos)
                hline(avg, f'Average entry {price_text(avg)}', t['muted'], Qt.DashLine, 0.65)
            tp = engine.price_level(pos, prof.tp)
            hline(tp, f'Target {price_text(tp)}', t['good'], Qt.DashLine, 0.98)
            if pos.stop is not None:
                sl = engine.price_level(pos, -pos.stop)
                hline(sl, f'Stop {price_text(sl)}', t['critical'], Qt.DashLine, 0.98)
        lo, hi = min(levels), max(levels)
        pad = (hi - lo) * 0.12 or hi * 0.01
        plot.setYRange(lo - pad, hi + pad, padding=0)
        plot.setXRange(-0.5, max(len(seg) - 0.5, 1.5), padding=0)
