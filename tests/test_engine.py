"""Engine tests. Uses sp500.csv from the project folder (or the path in the SP500_CSV environment variable).

retail_sim_v2.py is the reference: if the engine disagrees with it, the engine is wrong.
"""
import copy
import itertools
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import retail_sim_v2 as ref                     # noqa: E402
from app import data, engine, state as state_io  # noqa: E402

CSV = os.environ.get('SP500_CSV') or os.path.join(ROOT, 'sp500.csv')
if not os.path.exists(CSV):
    pytest.skip('sp500.csv not found (put it in the project folder or set SP500_CSV)', allow_module_level=True)

ROWS = data.load_csv(CSV)
DATES = [d for d, _ in ROWS]
CLOSES = [c for _, c in ROWS]
C_NP = np.array(CLOSES)
MOM = np.r_[np.zeros(20), C_NP[20:] / C_NP[:-20] - 1]
STARTS = [60, 517, 1203, 1850, len(CLOSES) - 253]
SWITCH_KEYS = [k for k, _, _ in engine.SWITCHES]
SWITCH_SETS = [{}] + [{k: True} for k in SWITCH_KEYS] + [{k: True for k in SWITCH_KEYS}]
ALL_COMBINED = next(p for p in engine.PRESETS if p.name == 'All combined')


def ref_simulate(profile, start, entry, rng=None):
    return ref.simulate(C_NP, MOM, start, rng, entry, profile.tp, profile.sl, profile.lev, profile.revenge,
                        profile.overconf, avg_down=profile.avg_down, move_stop=profile.move_stop,
                        panic=profile.panic, give_back=profile.give_back)


# ---------- 1. equivalence with the reference ----------
@pytest.mark.parametrize('preset', engine.PRESETS, ids=lambda p: p.name)
@pytest.mark.parametrize('switches', SWITCH_SETS, ids=lambda s: '+'.join(s) or 'plain')
def test_equivalence_with_reference(preset, switches):
    prof = preset.with_switches(**switches)
    for start, entry in itertools.product(STARTS, ('long', 'trend', 'random')):
        if entry == 'long':
            daily, trades = engine.replay(CLOSES, start, 1, prof, 'same', 252)
            eq, ref_trades, wiped, reasons = ref_simulate(prof, start, 'long')
        elif entry == 'trend':
            daily, trades = engine.replay(CLOSES, start, None, prof, 'trend', 252)
            eq, ref_trades, wiped, reasons = ref_simulate(prof, start, 'trend')
        else:
            daily, trades = engine.replay(CLOSES, start, None, prof, 'random', 252, np.random.default_rng(7 + start))
            eq, ref_trades, wiped, reasons = ref_simulate(prof, start, 'random', np.random.default_rng(7 + start))
        where = f'{prof.name} {switches} start={start} entry={entry}'
        assert daily[-1] == pytest.approx(eq, abs=1e-9), where
        assert len(trades) == len(ref_trades), where
        assert [t.pnl for t in trades] == pytest.approx(ref_trades, abs=1e-12), where
        assert (daily[-1] <= engine.RUIN) == wiped, where
        # exit reasons agree too (the engine splits the reference's 'time' into max-hold and end-of-period)
        mine = {}
        for t in trades:
            key = 'time' if t.reason in (engine.TIME, engine.END) else t.reason
            mine[key] = mine.get(key, 0) + 1
        assert mine == reasons, where


# ---------- 2. day-by-day equals batch ----------
NEW_SETS = [dict(break_even=True), dict(top_up=True), {k: True for k, _, _ in engine.ALL_SWITCHES}]


@pytest.mark.parametrize('switches', SWITCH_SETS + NEW_SETS, ids=lambda s: '+'.join(s) or 'plain')
def test_day_by_day_equals_batch(switches):
    for preset, start in itertools.product(engine.PRESETS, STARTS[:3]):
        prof = preset.with_switches(**switches)
        end = start + 251
        daily, trades = engine.replay(CLOSES, start, -1, prof, 'same', 252, dates=DATES)
        st = engine.TraderState()
        curve = [1.0]
        for k in range(start + 1, end + 1):                 # one close at a time, as the Signal tab does
            if st.position is None and not st.stopped:
                engine.open_trade(st, prof, CLOSES[k - 1], -1, DATES[k - 1], index=k - 1)
            engine.step(st, prof, CLOSES[k - 1], CLOSES[k], DATES[k], index=k, end_of_period=(k == end))
            curve.append(engine.mark_to_market(st))
        assert st.history == trades, f'{prof.name} start={start}'
        assert st.account == daily[-1]
        assert curve == daily


# ---------- 3 & 4. Signal-tab updates ----------
def signal_state(profile, entry_date, direction, start=10_000.0):
    st = engine.TraderState(start=start, account=start)
    engine.open_trade(st, profile, dict(ROWS)[entry_date], direction, entry_date)
    return st


def test_idempotent_update():
    prof = ALL_COMBINED.with_switches(avg_down=True, move_stop=True)
    i = DATES.index('2025-02-18')
    st = signal_state(prof, DATES[i], 'long')
    for cut in (i + 1, i + 4, i + 30, len(ROWS)):
        engine.process_sessions(st, prof, ROWS[:cut])
        before = copy.deepcopy(st)
        again = engine.process_sessions(st, prof, ROWS[:cut])       # press Update a second time
        assert again == []
        assert st == before


def test_skipped_days_equal_daily_updates():
    for preset in engine.PRESETS:
        prof = preset.with_switches(**{k: True for k in SWITCH_KEYS})
        for start in STARTS:
            a = signal_state(prof, DATES[start], 'long')
            b = signal_state(prof, DATES[start], 'long')
            one_at_a_time = []
            for cut in range(start + 2, start + 7):
                one_at_a_time += engine.process_sessions(a, prof, ROWS[:cut])
            all_at_once = engine.process_sessions(b, prof, ROWS[:start + 6])   # five new sessions in one update
            assert a.last_date == b.last_date == DATES[start + 5]
            assert all_at_once == one_at_a_time
            a.last_events = b.last_events = []       # only the "what happened in the last update" note may differ
            assert a == b, f'{prof.name} start={start}'


def test_signal_matches_report_over_a_year():
    """Walking sessions in uneven batches gives the same trades as the Report replay."""
    prof = ALL_COMBINED.with_switches(avg_down=True, panic=True)
    start = DATES.index('2025-02-18')
    _, trades = engine.replay(CLOSES, start, 1, prof, 'same', 252, dates=DATES, start_account=10_000.0)
    st = signal_state(prof, DATES[start], 'long')
    cut = start + 1
    for jump in itertools.cycle((1, 3, 1, 7, 2)):
        cut = min(cut + jump, start + 252)
        engine.process_sessions(st, prof, ROWS[:cut])
        if st.position is None and not st.stopped:
            # a skipped-day close is booked on its trigger date; re-enter there, as the model does
            exit_date = st.history[-1].exit_date
            engine.open_trade(st, prof, dict(ROWS)[exit_date], 1, exit_date)
            engine.process_sessions(st, prof, ROWS[:cut])
            if st.position is None and not st.stopped:
                continue
        if cut == start + 252 or st.stopped:
            break
    closed = [t for t in trades if t.reason != engine.END]
    assert len(st.history) >= len(closed) > 0
    for mine, theirs in zip(st.history, closed):
        assert (mine.entry_date, mine.exit_date, mine.reason) == (theirs.entry_date, theirs.exit_date, theirs.reason)
        assert mine.account_after == pytest.approx(theirs.account_after, rel=1e-12)


def test_state_round_trips_through_json(tmp_path):
    prof = ALL_COMBINED.with_switches(avg_down=True, move_stop=True)
    doc = state_io.new_doc(10_000, '^GSPC', prof, csv_path=CSV)
    i = DATES.index('2025-02-18')
    engine.open_trade(doc.trader, prof, CLOSES[i], 'long', DATES[i])
    engine.process_sessions(doc.trader, prof, ROWS[:i + 40])
    engine.open_trade(doc.trader, prof, CLOSES[i + 39], 'long', DATES[i + 39]) if doc.trader.position is None and not doc.trader.stopped else None
    state_io.save(str(tmp_path), doc)
    back = state_io.load(str(tmp_path))
    assert back == doc
    state_io.reset(str(tmp_path))
    assert state_io.load(str(tmp_path)) is None


# ---------- 5. data cleaning ----------
def yahoo_payload(closes, session_end):
    day = 86400
    t0 = 1_762_785_000                       # 14:30 UTC, a US regular-session open
    ts = [t0 + day * i for i in range(len(closes))]
    return {'chart': {'error': None, 'result': [{
        'meta': {'gmtoffset': -18000, 'currentTradingPeriod': {'regular': {'start': ts[-1], 'end': session_end(ts[-1])}}},
        'timestamp': ts, 'indicators': {'quote': [{'close': closes}]}}]}}


def test_unfinished_last_bar_and_null_closes_are_dropped():
    closes = [100.0, None, 101.5, 0.0, -3.0, 102.25, 103.0]
    payload = yahoo_payload(closes, lambda start: start + 23400)
    now_mid_session = payload['chart']['result'][0]['timestamp'][-1] + 3600
    rows = data.parse_chart(payload, now=now_mid_session)
    assert [c for _, c in rows] == [100.0, 101.5, 102.25]          # nulls, non-positive and the live bar are gone
    assert rows[0][0] == '2025-11-10'                              # date uses the exchange offset
    # within the 10-minute grace after the bell the bar is still treated as unfinished
    assert len(data.parse_chart(payload, now=now_mid_session + 19800 + 300)) == 3
    # once the session is over, the last bar is kept
    rows = data.parse_chart(payload, now=now_mid_session + 19800 + 601)
    assert [c for _, c in rows] == [100.0, 101.5, 102.25, 103.0]


def test_null_last_bar_keeps_previous_completed_session():
    payload = yahoo_payload([100.0, 101.0, None], lambda start: start + 23400)
    rows = data.parse_chart(payload, now=payload['chart']['result'][0]['timestamp'][-1] + 60)
    assert [c for _, c in rows] == [100.0, 101.0]


def test_csv_loader_skips_headers_and_bad_rows(tmp_path):
    p = tmp_path / 'x.csv'
    p.write_text('date,close\n2026-01-02,10.5\n2026-01-05,\n2026-01-06,0\n2026-01-07,11\n', encoding='utf-8')
    assert data.load_csv(str(p)) == [('2026-01-02', 10.5), ('2026-01-07', 11.0)]


# ---------- acceptance cases from the spec ----------
def test_known_report_case():
    start = DATES.index('2025-02-18')
    rep = engine.run_report(CLOSES, DATES, start, 'long', 'same', 252, engine.PRESETS)
    by_name = {r['name']: r for r in rep['results']}
    disciplined = by_name['Disciplined']
    assert disciplined['ntr'] == 30
    assert disciplined['final'] - 1 == pytest.approx(0.109, abs=0.002)
    combined = by_name['All combined']
    assert combined['wiped']
    assert '2025-04-01' <= combined['trades'][-1].exit_date <= '2025-04-10'


def test_known_signal_case():
    st = signal_state(ALL_COMBINED, '2026-10-20', 'short')
    events = engine.process_sessions(st, ALL_COMBINED, ROWS)
    closes = [e for e in events if e.kind == engine.CLOSE]
    assert len(closes) == 1
    assert (closes[0].reason, closes[0].date) == (engine.TARGET, '2026-10-23')
    assert st.position is None and st.last_date == DATES[-1]
    assert [e.kind for e in st.last_events] == [engine.CLOSE]


# ---------- opposite positioning ----------
def test_opposite_positioning_mirrors_every_trade():
    for preset in engine.PRESETS:
        prof = preset.with_switches(avg_down=True, move_stop=True)
        for start in STARTS:
            daily, trades, opp = engine.replay_with_opposite(CLOSES, start, 1, prof, 'trend', 252)
            assert (daily, trades) == engine.replay(CLOSES, start, 1, prof, 'trend', 252)
            assert len(opp) == len(daily)
            # rebuild the opposite account from the model's own trades: minus its dollar result, same dollar costs
            acct = 1.0
            for t in trades:
                acct = (acct - max(-t.account_before, t.account_before * t.pnl)
                        - t.account_before * 2 * engine.COST * t.size)
            assert opp[-1] == pytest.approx(acct, abs=1e-12), f'{prof.name} start={start}'
            # mirror image: the two accounts add up to twice the start, less both sides' costs
            if daily[-1] > 0:
                costs = sum(t.account_before * 2 * engine.COST * t.size for t in trades)
                assert opp[-1] + daily[-1] == pytest.approx(2 - 2 * costs, abs=1e-9)


def test_opposite_of_a_losing_long_wins():
    start = DATES.index('2025-02-18')
    rep = engine.run_report(CLOSES, DATES, start, 'long', 'same', 252, engine.PRESETS)
    combined = next(r for r in rep['results'] if r['name'] == 'All combined')
    assert combined['wiped'] and combined['opp_final'] == pytest.approx(2.0, abs=0.05)   # wiped out -> about double
    assert combined['opp_dd'] <= 0


# ---------- behaviours added in the app (not in the reference) ----------
def test_new_behaviours_off_by_default():
    assert not any(getattr(p, k) for p in engine.PRESETS for k, _, _ in engine.NEW_SWITCHES)


def test_break_even_closes_a_recovered_loser():
    prof = engine.Profile('t', .05, .10, 1, break_even=True)
    st = engine.TraderState()
    engine.open_trade(st, prof, 100.0, 'long', 'd0')
    assert [e.kind for e in engine.step(st, prof, 100.0, 100.4, 'd1')] == [engine.HOLD]     # up a little, never down: stay
    assert [e.kind for e in engine.step(st, prof, 100.4, 98.5, 'd2')] == [engine.HOLD]      # down 1.5%: now armed
    assert [e.kind for e in engine.step(st, prof, 98.5, 99.5, 'd3')] == [engine.HOLD]       # still under water
    ev = engine.step(st, prof, 99.5, 100.1, 'd4')                                           # back to entry: out
    assert (ev[0].kind, ev[0].reason) == (engine.CLOSE, engine.BREAK_EVEN)
    assert st.position is None and st.history[-1].move == pytest.approx(0.001)
    # the same path without the habit keeps holding
    plain = engine.Profile('t', .05, .10, 1)
    st2 = engine.TraderState()
    engine.open_trade(st2, plain, 100.0, 'long', 'd0')
    for prev, c in ((100.0, 100.4), (100.4, 98.5), (98.5, 99.5), (99.5, 100.1)):
        engine.step(st2, plain, prev, c)
    assert st2.position is not None


def test_target_still_wins_over_break_even():
    prof = engine.Profile('t', .01, .10, 1, break_even=True)
    st = engine.TraderState()
    engine.open_trade(st, prof, 100.0, 'long')
    engine.step(st, prof, 100.0, 98.0)
    assert engine.step(st, prof, 98.0, 101.5)[0].reason == engine.TARGET


def test_top_up_deposits_then_stops():
    prof = engine.Profile('t', .50, None, 5, top_up=True)
    st = engine.TraderState(start=10_000.0, account=10_000.0)
    left = []
    for n in range(engine.MAX_TOPUPS + 1):
        engine.open_trade(st, prof, 100.0, 'long', f'd{n}')
        kinds = [e.kind for e in engine.step(st, prof, 100.0, 80.0, f'e{n}')]              # -20% at 5x: wiped out
        left.append(st.history[-1].account_after)
        if n < engine.MAX_TOPUPS:
            assert kinds == [engine.MARGIN_CALL, engine.CLOSE, engine.TOP_UP] or kinds == [engine.CLOSE, engine.TOP_UP]
            assert not st.stopped and st.account == pytest.approx(left[-1] + 10_000.0)
        else:
            assert kinds[-1] == engine.STOP_TRADING and st.stopped                         # no deposits left
    assert st.topups == engine.MAX_TOPUPS and st.total_in == 30_000.0


def test_chase_direction_follows_the_last_three_days():
    closes = [100, 101, 102, 103, 99, 98, 97, 104]
    assert [engine.chase_direction(closes, t) for t in range(8)] == [-1, -1, -1, 1, -1, -1, -1, 1]
    daily, trades = engine.replay(CLOSES, STARTS[1], 1, engine.PRESETS[0], 'chase', 252)
    for t in trades[1:]:
        assert t.direction == engine.chase_direction(CLOSES, t.entry_index)


def test_report_result_counts_top_up_deposits():
    start = DATES.index('2025-02-18')
    profs = [ALL_COMBINED.with_switches(top_up=True)]
    r = engine.run_report(CLOSES, DATES, start, 'long', 'same', 252, profs)['results'][0]
    assert r['deposited'] > 0
    assert r['result'] == pytest.approx(r['final'] / (1 + r['deposited']) - 1)


# ---------- Signal must equal the Report backtest ----------
FOLLOW_RULES = ['same', 'trend', 'chase', 'random_one']


@pytest.mark.parametrize('rule', FOLLOW_RULES)
@pytest.mark.parametrize('switches', [{}, {k: True for k in SWITCH_KEYS}, {k: True for k, _, _ in engine.ALL_SWITCHES}],
                         ids=['plain', 'four-extras', 'all-extras'])
def test_signal_followed_daily_equals_report_backtest(rule, switches):
    """Run the Signal one session at a time (and in uneven batches) for a year: same trades and account as the Report."""
    account = 10_000.0
    for preset, start, side, trader in itertools.product(engine.PRESETS, STARTS[:3], (1, -1), (1, 7)):
        prof = preset.with_switches(**switches)
        end = start + 251
        rep = engine.run_report(CLOSES, DATES, start, side, rule, 252, [prof], trader=trader)['results'][0]
        follow = dict(rule=rule, first_direction=side, trader=trader)
        for jumps in ((1,), (1, 4, 2, 9)):
            st = engine.TraderState(start=account, account=account)
            engine.open_trade(st, prof, CLOSES[start], side, DATES[start])
            cut = start + 1
            for jump in itertools.cycle(jumps):
                cut = min(cut + jump, end + 1)
                engine.process_sessions(st, prof, ROWS[:cut], follow)
                if cut == end + 1:
                    break
            where = f'{prof.name} {rule} start={start} side={side} trader={trader} jumps={jumps}'
            model = rep['trades']
            # On the backtest's final day the Report labels whatever is open "end of period". If that day is also a
            # margin call, the Signal books the margin call: same day, same close, same account.
            margin_at_end = bool(model[-1].reason == engine.END and st.history and st.history[-1].reason == engine.MARGIN
                                 and st.history[-1].exit_date == DATES[end])
            still_open = model[-1].reason == engine.END and not margin_at_end
            closed = model[:-1] if still_open else model
            assert len(st.history) == len(closed), where
            for mine, theirs in zip(st.history, closed):
                reason = engine.MARGIN if (theirs.reason == engine.END and margin_at_end) else theirs.reason
                assert (mine.entry_date, mine.exit_date, mine.direction, mine.reason, mine.lots) == \
                       (theirs.entry_date, theirs.exit_date, theirs.direction, reason, theirs.lots), where
                assert mine.account_after == pytest.approx(theirs.account_after * account, rel=1e-9), where
            # what is left open in the Signal is the trade the Report closes as "end of period"
            if still_open:
                assert st.position is not None and st.position.entry_date == model[-1].entry_date, where
                cost = 2 * engine.COST * model[-1].size * model[-1].account_before * account
                assert engine.mark_to_market(st) - cost == pytest.approx(rep['final'] * account, rel=1e-9, abs=1e-6), where
            else:
                assert st.account == pytest.approx(rep['final'] * account, rel=1e-9), where
            ok, message = engine.check_alignment(st, prof, ROWS[:end + 1], follow)
            assert ok, f'{where}: {message}'


def test_alignment_check_catches_a_tampered_history():
    prof = ALL_COMBINED
    start = DATES.index('2025-02-18')
    follow = dict(rule='same', first_direction=1, trader=1)
    st = engine.TraderState(start=10_000.0, account=10_000.0)
    engine.open_trade(st, prof, CLOSES[start], 1, DATES[start])
    engine.process_sessions(st, prof, ROWS[:start + 20], follow)
    assert engine.check_alignment(st, prof, ROWS[:start + 20], follow)[0] is True
    st.history[1].account_after += 1.0
    ok, message = engine.check_alignment(st, prof, ROWS[:start + 20], follow)
    assert ok is False and 'Trade 2 differs' in message
    # revised price history is caught too
    st.history[1].account_after -= 1.0
    revised = [(d, c * (1.01 if d == st.history[0].exit_date else 1)) for d, c in ROWS[:start + 20]]
    assert engine.check_alignment(st, prof, revised, follow)[0] is False
