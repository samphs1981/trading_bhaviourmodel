"""Retail-behaviour model engine. Pure Python, no Qt.

One day-stepping core (`step`) drives both the Report tab (`replay`, `run_report`)
and the Signal tab (`process_sessions`), so the two can never disagree.

The rules reproduce `simulate()` in retail_sim_v2.py exactly, including the order
of the daily checks and the floating-point form of each expression.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field, replace
from typing import Callable, Optional, Sequence

# ---- model constants (same values as retail_sim_v2.py) ----
COST = 2e-4          # 2 basis points per side
MAX_HOLD = 120       # trading days
RUIN = 0.2           # fraction of the starting account
ADD_STEP = 0.015     # averaging down: every 1.5% against the first entry
MAX_ADDS = 2
PANIC_DAY = 0.02
TREND_DAYS = 20
MC_RUNS = 300
MC_SEED = 1000

# ---- close reasons ----
TARGET, GAVE_BACK, PANIC, STOP, TIME, END, MARGIN = (
    'target', 'gave back', 'panic', 'stop', 'time', 'end of period', 'margin call')

# ---- event kinds ----
HOLD, ADD_LOT, STOP_WIDENED, CLOSE, MARGIN_CALL, STOP_TRADING = (
    'HOLD', 'ADD_LOT', 'STOP_WIDENED', 'CLOSE', 'MARGIN_CALL', 'STOP_TRADING')

SWITCHES = [
    ('avg_down', 'Averaging down', 'add a lot every 1.5% against the first entry, max 2 adds'),
    ('move_stop', 'Moving the stop', 'first time the stop is hit, widen it by half instead'),
    ('panic', 'Panic exit', 'close after a 2% day against a losing position'),
    ('give_back', 'Giving-back exit', 'close a winner that falls half the target from its peak'),
]


@dataclass(frozen=True)
class Profile:
    name: str
    tp: float
    sl: Optional[float]
    lev: float = 1
    revenge: bool = False
    overconf: bool = False
    avg_down: bool = False
    move_stop: bool = False
    panic: bool = False
    give_back: bool = False

    def with_switches(self, **switches) -> 'Profile':
        return replace(self, **switches)

    def switch_names(self) -> list[str]:
        return [label for key, label, _ in SWITCHES if getattr(self, key)]

    def describe(self) -> str:
        """Short rule summary, e.g. '+1% / -4%, 5x, revenge, overconfidence'."""
        stop = f'-{self.sl * 100:g}%' if self.sl is not None else 'no stop'
        parts = [f'+{self.tp * 100:g}% / {stop}', f'{self.lev:g}x']
        if self.revenge: parts.append('revenge')
        if self.overconf: parts.append('overconfidence')
        parts += [s.lower() for s in self.switch_names()]
        return ', '.join(parts)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> 'Profile':
        return Profile(**d)


PRESETS: list[Profile] = [
    Profile('Disciplined', .02, .02, 1),
    Profile('Cut losers, let winners run', .04, .01, 1),
    Profile('Cut winners, hold losers', .01, .04, 1),
    Profile('Never cut losers', .01, None, 1),
    Profile('Disciplined, 5x leverage', .02, .02, 5),
    Profile('Cut winners, hold losers, 5x', .01, .04, 5),
    Profile('Hold losers, 5x + revenge', .01, .04, 5, revenge=True),
    Profile('Hold losers, 5x + overconfidence', .01, .04, 5, overconf=True),
    Profile('All combined', .01, .04, 5, revenge=True, overconf=True),
    Profile('All combined, never cut losers', .01, None, 5, revenge=True, overconf=True),
]
DEFAULT_SHOWN = ('Disciplined', 'Cut winners, hold losers', 'Cut winners, hold losers, 5x', 'All combined')


@dataclass
class Event:
    kind: str
    date: Optional[str] = None
    reason: str = ''
    data: dict = field(default_factory=dict)   # plain JSON-able values only


@dataclass
class Position:
    direction: int                 # +1 long, -1 short
    lots: list                     # entry price of each lot; lots[0] is the first entry
    base: float                    # size of one lot as a multiple of the account
    stop: Optional[float]          # current stop distance (fraction), None = no stop
    peak: float = 0.0              # best average return seen so far
    moved: bool = False            # stop already widened this trade
    entry_date: Optional[str] = None
    entry_index: Optional[int] = None
    days_held: int = 0
    lot_dates: list = field(default_factory=list)
    last_close: Optional[float] = None
    last_return: float = 0.0       # average lot return at the last processed close
    last_pnl: float = 0.0          # size x return at the last processed close

    @property
    def size(self) -> float:
        return self.base * len(self.lots)


@dataclass
class Trade:
    entry_date: Optional[str]
    exit_date: Optional[str]
    direction: int
    entry_price: float
    exit_price: float
    size: float                    # total position as a multiple of the account
    lots: int
    move: float                    # average lot return
    pnl: float                     # size x move (before costs)
    account_before: float
    account_after: float
    reason: str
    stop_moved: bool = False
    entry_index: Optional[int] = None
    exit_index: Optional[int] = None

    @property
    def win(self) -> bool:
        return self.pnl > 0


@dataclass
class TraderState:
    start: float = 1.0
    account: float = 1.0
    mult: float = 1.0
    wins_row: int = 0
    position: Optional[Position] = None
    history: list = field(default_factory=list)
    stopped: bool = False
    last_date: Optional[str] = None            # last processed session (Signal tab)
    last_events: list = field(default_factory=list)   # non-HOLD events of the last update

    @property
    def ruin(self) -> float:
        return RUIN * self.start

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> 'TraderState':
        d = dict(d)
        pos = d.pop('position', None)
        hist = d.pop('history', [])
        evs = d.pop('last_events', [])
        st = TraderState(**d)
        st.position = Position(**pos) if pos else None
        st.history = [Trade(**t) for t in hist]
        st.last_events = [Event(**e) for e in evs]
        return st


def _dir(direction) -> int:
    if direction in (1, -1):
        return direction
    if direction in ('long', 'short'):
        return 1 if direction == 'long' else -1
    raise ValueError(f'direction must be long or short, got {direction!r}')


def reason_label(reason: str, stop_moved: bool = False) -> str:
    return {TARGET: 'Take profit', GAVE_BACK: 'Gave back profit', PANIC: 'Panic exit',
            STOP: 'Stop loss' + (' (after widening)' if stop_moved else ''),
            TIME: f'Time exit ({MAX_HOLD} days)', END: 'End of period (still open)',
            MARGIN: 'Margin call'}.get(reason, reason)


def open_trade(state: TraderState, profile: Profile, price: float, direction,
               date: Optional[str] = None, index: Optional[int] = None) -> Position:
    """Open a position at `price`. Size is fixed here: leverage x current size multiplier."""
    if state.stopped:
        raise ValueError('Trading has stopped: the account is at or below 20% of its starting value.')
    if state.position is not None:
        raise ValueError('A position is already open.')
    if not price or price <= 0:
        raise ValueError('Entry price must be positive.')
    state.position = Position(direction=_dir(direction), lots=[price], base=profile.lev * state.mult,
                              stop=profile.sl, entry_date=date, entry_index=index,
                              lot_dates=[date], last_close=price)
    if date is not None:
        state.last_date = date
    state.last_events = []
    return state.position


def step(state: TraderState, profile: Profile, prev_close: float, close: float,
         date: Optional[str] = None, *, index: Optional[int] = None,
         end_of_period: bool = False) -> list[Event]:
    """Advance an open position by one daily close. Returns what happened that day."""
    pos = state.position
    if pos is None or state.stopped:
        return []
    d = pos.direction
    lots = pos.lots
    rl = [d * (close / p - 1) for p in lots]
    r = sum(rl) / len(rl)                       # move measured from the average of the lots' returns
    size = pos.base * len(lots)
    pnl = size * r
    day = d * (close / prev_close - 1)
    pos.peak = max(pos.peak, r)
    pos.days_held += 1
    pos.last_close, pos.last_return, pos.last_pnl = close, r, pnl
    tp = profile.tp
    events: list[Event] = []
    why = None

    if r >= tp:
        why = TARGET
    elif profile.give_back and pos.peak >= tp / 2 and r <= pos.peak - tp / 2:
        why = GAVE_BACK
    elif profile.panic and r < 0 and day <= -PANIC_DAY:
        why = PANIC
    elif pos.stop is not None and r <= -pos.stop:
        if profile.move_stop and not pos.moved:
            old = pos.stop
            pos.stop *= 1.5
            pos.moved = True
            events.append(Event(STOP_WIDENED, date, data=dict(old_stop=old, new_stop=pos.stop, move=r, close=close)))
        else:
            why = STOP
    if why is None:
        if pos.days_held >= MAX_HOLD:
            why = TIME
        elif end_of_period:
            why = END
        elif state.account * (1 + pnl) <= state.ruin:
            why = MARGIN

    if why is not None:
        before = state.account
        state.account = before * max(0.0, 1 + pnl - 2 * COST * size)
        old_mult = state.mult
        if pnl > 0:
            state.wins_row += 1
            state.mult = 2.0 if (profile.overconf and state.wins_row >= 2) else 1.0
        else:
            state.wins_row = 0
            state.mult = min(state.mult * 2, 4.0) if profile.revenge else 1.0
        state.history.append(Trade(
            entry_date=pos.entry_date, exit_date=date, direction=d, entry_price=lots[0], exit_price=close,
            size=size, lots=len(lots), move=r, pnl=pnl, account_before=before, account_after=state.account,
            reason=why, stop_moved=pos.moved, entry_index=pos.entry_index, exit_index=index))
        state.position = None
        state.stopped = state.account <= state.ruin
        if why == MARGIN:
            events.append(Event(MARGIN_CALL, date, data=dict(close=close, move=r)))
        events.append(Event(CLOSE, date, why, dict(
            close=close, move=r, peak=pos.peak, day=day, size=size, lots=len(lots), stop=pos.stop,
            stop_moved=pos.moved, account_before=before, account_after=state.account,
            old_mult=old_mult, new_mult=state.mult, wins_row=state.wins_row)))
        if state.stopped:
            events.append(Event(STOP_TRADING, date, data=dict(account=state.account)))
        return events

    if profile.avg_down and len(lots) <= MAX_ADDS and rl[0] <= -ADD_STEP * len(lots):
        lots.append(close)                       # add to the loser at today's close
        pos.lot_dates.append(date)
        events.append(Event(ADD_LOT, date, data=dict(
            close=close, first_move=rl[0], lots=len(lots), base=pos.base,
            lot_value=pos.base * state.account, average_entry=average_entry(pos))))
    if not events:
        events.append(Event(HOLD, date, data=dict(close=close, move=r)))
    return events


# ---- helpers on an open position ----
def average_entry(pos: Position) -> float:
    """Price at which the average lot return is zero."""
    return len(pos.lots) / sum(1 / q for q in pos.lots)


def price_level(pos: Position, move: float) -> float:
    """Close at which the average lot return equals `move` (signed, e.g. +tp or -stop)."""
    return (1 + pos.direction * move) * average_entry(pos)


def current_move(pos: Position) -> float:
    """Average lot return at the last processed close, including any lot added that day."""
    c = pos.last_close
    return sum(pos.direction * (c / p - 1) for p in pos.lots) / len(pos.lots)


def mark_to_market(state: TraderState) -> float:
    if state.position is None:
        return state.account
    return max(0.0, state.account * (1 + state.position.last_pnl))


def sizing_note(state: TraderState, profile: Profile) -> str:
    """e.g. '5x leverage x 2 = 10x account = $105,000 (doubled: 2 wins in a row)'."""
    m = state.mult
    if m == 1:
        why = 'normal size'
    elif profile.overconf and state.wins_row >= 2:
        why = f'doubled: {state.wins_row} wins in a row'
    else:
        why = f'{m:.0f}x normal: doubling after a loss, max 4x'
    total = profile.lev * m
    return f'{profile.lev:g}x leverage x {m:.0f} = {total:g}x account = ${total * state.account:,.0f} ({why})'


# ---- Report tab: batch replay ----
def trend_direction(closes: Sequence[float], t: int) -> int:
    return 1 if (t >= TREND_DAYS and closes[t] > closes[t - TREND_DAYS]) else -1


def replay(closes: Sequence[float], start_index: int, first_direction, profile: Profile,
           follow_rule: str = 'same', horizon: int = 252, rng=None,
           dates: Optional[Sequence[str]] = None, start_account: float = 1.0):
    """Replay one trader from `start_index`. Returns (daily_equity, trades).

    first_direction: +1/-1 (or 'long'/'short') for the first trade, or None to let
    `follow_rule` choose the first trade too (as retail_sim_v2.simulate does).
    follow_rule: 'same' | 'trend' | 'random' (random needs `rng` with a .random() method).
    horizon: number of sessions including the entry day, capped at the latest close.
    """
    daily, trades, _ = replay_with_opposite(closes, start_index, first_direction, profile, follow_rule,
                                            horizon, rng, dates, start_account)
    return daily, trades


def replay_with_opposite(closes: Sequence[float], start_index: int, first_direction, profile: Profile,
                         follow_rule: str = 'same', horizon: int = 252, rng=None,
                         dates: Optional[Sequence[str]] = None, start_account: float = 1.0):
    """As `replay`, plus the account of someone taking the exact opposite side of every trade.

    The opposite trader is the mirror image of the model: same entry day, exit day and the same
    dollar position, but short when the model is long and vice versa. Every dollar the model
    loses, they gain (and the reverse), so a wiped-out model leaves them near twice the start.
    They pay the same dollar costs. No ruin rule or floor is applied to them (either would break
    the mirror), so if the model more than doubles, the opposite account goes below zero.
    Returns (daily_equity, trades, opposite_daily_equity).
    """
    end = min(start_index + horizon - 1, len(closes) - 1)
    first_dir = None if first_direction is None else _dir(first_direction)
    state = TraderState(start=start_account, account=start_account)
    daily = [start_account] * (end - start_index + 1)
    opp_daily = list(daily)
    opp = start_account
    t = start_index
    first = True
    while t < end and not state.stopped:
        if first and first_dir is not None:
            d = first_dir
        elif follow_rule == 'same':
            d = first_dir if first_dir is not None else 1
        elif follow_rule == 'trend':
            d = trend_direction(closes, t)
        elif follow_rule == 'random':
            d = 1 if rng.random() < .5 else -1
        else:
            raise ValueError(f'unknown follow rule {follow_rule!r}')
        first = False
        open_trade(state, profile, closes[t], d, dates[t] if dates else None, index=t)
        k = t
        while state.position is not None:
            k += 1
            step(state, profile, closes[k - 1], closes[k], dates[k] if dates else None,
                 index=k, end_of_period=(k >= end))
            daily[k - start_index] = mark_to_market(state)
            if state.position is not None:
                gross = max(-state.account, state.account * state.position.last_pnl)   # model's open gain in dollars
                opp_daily[k - start_index] = opp - gross
            else:
                tr = state.history[-1]
                gross = max(-tr.account_before, tr.account_before * tr.pnl)            # the model cannot lose more than it has
                opp = opp - gross - tr.account_before * 2 * COST * tr.size
                opp_daily[k - start_index] = opp
        t = k
    for j in range(t - start_index + 1, len(daily)):
        daily[j] = state.account
        opp_daily[j] = opp
    return daily, state.history, opp_daily


def worst_drawdown(curve: Sequence[float]) -> float:
    peak = curve[0]
    dd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd = min(dd, v / peak - 1)
    return dd


def run_report(closes: Sequence[float], dates: Sequence[str], start_index: int, direction, follow_rule: str,
               horizon: int, profiles: Sequence[Profile], runs: int = MC_RUNS,
               progress: Optional[Callable[[int, int], None]] = None, trader: int = 1) -> dict:
    """Replay every profile from one entry. Account values are multiples of the starting account.

    follow_rule: 'same' | 'trend' | 'random_one' (one trader's coin flips, chosen by `trader`,
    which is one of the runs behind the median) | 'random' (median of `runs` traders).
    """
    if not 0 <= start_index < len(closes) - 1:
        raise ValueError('The entry day needs at least one later close to replay.')
    d = _dir(direction)
    results = []
    for n, prof in enumerate(profiles):
        if follow_rule == 'random':
            paths = [replay_with_opposite(closes, start_index, d, prof, 'random', horizon, random.Random(MC_SEED + i), dates)
                     for i in range(runs)]
            opp_finals = [p[2][-1] for p in paths]
            length = len(paths[0][0])
            curve = [sorted(p[0][j] for p in paths)[runs // 2] for j in range(length)]
            finals = [p[0][-1] for p in paths]
            results.append(dict(
                profile=prof, name=prof.name, random=True, curve=curve, first=paths[0][1][0],
                final=sorted(finals)[runs // 2], lose=sum(f < 1 for f in finals) / runs,
                wiped=sum(f <= RUIN for f in finals) / runs, trades=None,
                ntr=sum(len(p[1]) for p in paths) / runs,
                opp_curve=[sorted(p[2][j] for p in paths)[runs // 2] for j in range(length)],
                opp_final=sorted(opp_finals)[runs // 2],
                opp_dd=sorted(worst_drawdown(p[2]) for p in paths)[runs // 2],      # median of each run's worst drawdown
                opp_wiped=sum(f <= 0 for f in opp_finals) / runs))
        else:
            if follow_rule == 'random_one':
                curve, trades, opp = replay_with_opposite(closes, start_index, d, prof, 'random', horizon,
                                                          random.Random(MC_SEED + trader - 1), dates)
            else:
                curve, trades, opp = replay_with_opposite(closes, start_index, d, prof, follow_rule, horizon, None, dates)
            results.append(dict(
                profile=prof, name=prof.name, random=False, curve=curve, first=trades[0], final=curve[-1],
                dd=worst_drawdown(curve), wiped=curve[-1] <= RUIN, trades=trades, ntr=len(trades),
                wins=sum(t.win for t in trades),
                opp_curve=opp, opp_final=opp[-1], opp_dd=worst_drawdown(opp), opp_wiped=opp[-1] <= 0))
        if progress:
            progress(n + 1, len(profiles))
    length = len(results[0]['curve'])
    return dict(dates=list(dates[start_index:start_index + length]),
                price=list(closes[start_index:start_index + length]),
                results=results, start_index=start_index, direction=d, follow=follow_rule,
                horizon=horizon, runs=runs, trader=trader)


# ---- Signal tab: walk completed sessions once each ----
def process_sessions(state: TraderState, profile: Profile, sessions: Sequence[tuple]) -> list[Event]:
    """Apply every session dated after `state.last_date`, in order, exactly once.

    sessions: (date 'YYYY-MM-DD', close) for completed sessions, oldest first.
    Returns all events (including HOLD). Calling it again with the same data does nothing.
    """
    events: list[Event] = []
    stepped = False
    for k, (date, close) in enumerate(sessions):
        if state.last_date is not None and date <= state.last_date:
            continue
        if state.position is not None and not state.stopped:
            prev = sessions[k - 1][1] if k > 0 else close
            events += step(state, profile, prev, close, date)
            stepped = True
        state.last_date = date
    if stepped:
        state.last_events = [e for e in events if e.kind != HOLD]
    return events
