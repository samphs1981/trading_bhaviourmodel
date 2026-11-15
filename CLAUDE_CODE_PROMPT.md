# Build a PySide6 desktop app: Retail Behaviour Model (Report + Signal)

## What this is and why

I have a research model that simulates common retail-trader mistakes (closing winners early, holding losers, leverage, revenge sizing, overconfidence sizing, averaging down, moving the stop, panic exits, giving-back exits) on daily closing prices. It currently exists as three scripts with small browser UIs. I want one desktop app in PySide6 that replaces the two UI scripts.

The app is a research and self-awareness tool. It never places orders and has no broker connection.

## Files in this folder (read these first)

- `retail_sim_v2.py`: **the source of truth for the model rules.** The `simulate()` function defines every behaviour. The app's engine must reproduce it exactly.
- `behaviour_report.py`: existing browser version of the Report feature. Use it as the reference for what the report shows (`run_path`, `analyse`, the results table, the curves).
- `behaviour_tracker.py`: existing browser version of the Signal feature. Use it as the reference for state handling and the daily instruction logic (`advance`, `open_position`, `sizing_note`), and for the Yahoo download including how it drops an unfinished session bar (`fetch_closes`).
- `sp500.csv`: `date,close` for the S&P 500, 11 Nov 2016 to 10 Nov 2026. Use it for tests and as an offline data source.

All three scripts implement the full version-2 model, including the four open-position behaviours (`extras` in the two UI scripts). The new app must do the same.

## The model, stated precisely

A trader holds one position at a time. Everything is evaluated once per day on the daily close.

**Profile parameters**

| Parameter | Meaning |
| --- | --- |
| `tp` | Take-profit as a fraction, e.g. 0.01 |
| `sl` | Stop-loss as a fraction, or None for no stop |
| `lev` | Leverage: position notional = `lev` x size multiplier x account value at entry |
| `revenge` | After a losing trade, the size multiplier doubles (max 4). After a win it resets to 1 |
| `overconf` | After 2 or more wins in a row, the size multiplier is 2 |
| `avg_down` | Add an equal-sized lot each time the close is another 1.5% against the FIRST entry price, max 2 adds. Target and stop are then measured from the average of the lots' individual returns |
| `move_stop` | The first time the stop is hit in a trade, do not exit: multiply the stop by 1.5 instead. Once per trade |
| `panic` | Close if the day's move is 2% or more against the position while the trade is losing |
| `give_back` | Once the trade has been up by `tp/2`, close if it falls `tp/2` below its peak |

**Constants:** cost 2 basis points per side (applied to total position size on exit as `2 * cost * size`), maximum hold 120 trading days, ruin level 20% of starting account (trading stops for good at or below it).

**Daily check order for an open trade** (this order matters; copy it from `simulate()` in `retail_sim_v2.py`):

1. Target reached -> close.
2. Give-back rule -> close.
3. Panic rule -> close.
4. Stop reached -> if `move_stop` and not yet moved this trade, widen the stop and stay in; otherwise close.
5. If still open: maximum hold reached or end of replay period -> close.
6. If still open: account marked to market at or below the ruin level -> margin call, close.
7. If still open and `avg_down` applies -> add a lot at today's close.

**After a close:** account = account x max(0, 1 + pnl - 2 x cost x size), where `pnl` = size x average lot return. Then update the win streak and size multiplier. A trade counts as a win when `pnl > 0`.

**Preset profiles** (ship these, and let the user build a custom one from the switches):

| Name | tp | sl | lev | revenge | overconf |
| --- | --- | --- | --- | --- | --- |
| Disciplined | 2% | 2% | 1 | no | no |
| Cut losers, let winners run | 4% | 1% | 1 | no | no |
| Cut winners, hold losers | 1% | 4% | 1 | no | no |
| Never cut losers | 1% | none | 1 | no | no |
| Disciplined, 5x leverage | 2% | 2% | 5 | no | no |
| Cut winners, hold losers, 5x | 1% | 4% | 5 | no | no |
| Hold losers, 5x + revenge | 1% | 4% | 5 | yes | no |
| Hold losers, 5x + overconfidence | 1% | 4% | 5 | no | yes |
| All combined | 1% | 4% | 5 | yes | yes |
| All combined, never cut losers | 1% | none | 5 | yes | yes |

The four open-position switches (`avg_down`, `move_stop`, `panic`, `give_back`) are independent checkboxes that can be added to any preset.

## Architecture

Keep the model completely separate from the UI so it can be tested without Qt.

```
app/
  engine.py      # pure Python, no Qt: Profile dataclass, step-by-step trade engine, replay, Monte Carlo
  data.py        # price loading: Yahoo download + CSV, with a small on-disk cache
  state.py       # load/save Signal-tab state as JSON
  ui/
    main_window.py
    report_tab.py
    signal_tab.py
    widgets.py   # shared: security picker, profile editor, chart helpers
  main.py
tests/
  test_engine.py
requirements.txt # PySide6, pyqtgraph, pytest
```

**engine.py design.** Build one day-stepping core and use it for both tabs, so Report and Signal can never disagree:

- `Profile` dataclass with the parameters above.
- `TraderState`: account value, size multiplier, win streak, open position (direction, list of lot prices, base size, current stop, peak return, stop-moved flag, entry index), trade history, stopped flag.
- `step(state, profile, prev_close, close, date) -> list[Event]`: advances one day for an open position and returns events such as `HOLD`, `ADD_LOT`, `STOP_WIDENED`, `CLOSE(reason)`, `MARGIN_CALL`, `STOP_TRADING`.
- `open_trade(state, profile, price, direction, date)`.
- `replay(closes, start_index, first_direction, profile, follow_rule, horizon, rng) -> (daily_equity, trades)` built on `step`.

**data.py.** Download daily closes from `https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=10y&interval=1d` with a browser-like User-Agent, using only `urllib`. Convert timestamps to dates with the exchange offset in `meta.gmtoffset`. Drop the last bar when that session has not finished (see `fetch_closes` in `behaviour_tracker.py`). Ignore null or non-positive closes. Cache per symbol on disk and offer "Load CSV" as a fallback. Run downloads in a worker thread (`QThread` or `QRunnable`) so the window never freezes, and show errors in the UI instead of crashing.

**Securities list** (dropdown, plus a free-text Yahoo symbol field): `^GSPC` S&P 500, `^IXIC` Nasdaq Composite, `^AXJO` ASX 200, `^FTSE` FTSE 100, `^N225` Nikkei 225, `GC=F` Gold, `BZ=F` Brent crude, `AUDUSD=X` AUD/USD, `BTC-USD` Bitcoin, `AAPL`, `MSFT`, `NVDA`, `TSLA`, `BHP.AX`, `CBA.AX`.

## Tab 1: Report

Purpose: "If I had entered this security on this day, long or short, what would each behaviour profile have done to my account?"

**Inputs**

- Security (dropdown or custom symbol).
- Entry date (date picker; must be a trading day with at least one later close; show a clear message otherwise).
- Long or short.
- Rule for later trades: same side as my entry, follow the 20-day trend (long if close > close 20 days earlier), or random long/short.
- Starting account in dollars (default 10,000).
- Replay length: 3 months (63 days), 6 months (126), 1 year (252), capped at the latest close.
- Which profiles to draw (checkbox list, at most 8 at once), plus the four open-position switches applied to all profiles in the run.

**Outputs**

- Account-value curves, one line per selected profile, with a dashed line at the starting account, a legend, and a crosshair showing date and values under the cursor. Use pyqtgraph.
- Price chart of the security for the same period, with the entry marked.
- Results table for all profiles: how the first trade ended (reason and date), first-trade move, number of trades, wins, final account, percent result, worst drawdown, wiped out yes/no.
- Trade log for the profile selected in the table: entry date, exit date, side, size, lots, move, account after, exit reason.
- With the "random" rule: run 300 seeded simulations per profile, plot the median curve, and replace the single-path columns with median final account, share of runs losing, share wiped out.
- "Export report" button that saves a self-contained HTML file (tables plus the charts as embedded images).

## Tab 2: Signal

Purpose: I run this once a day. It tells me what the chosen profile does today with my open position.

**Setup (first run or after reset):** starting account, security, profile (preset plus switches).

**When flat:** show the size for the next trade and why, for example "5x leverage x 2 = 10x account = $105,000 (doubled: 2 wins in a row)". Form to open a position: entry date (defaults to the latest completed session), long or short, optional actual entry price (blank = that day's close).

**When a position is open:** press "Update" (also runs automatically on launch). The app walks every completed session since the last processed one, in order, and shows ONE headline instruction in large type:

- **HOLD** with the current move, days held, and the price levels for the target and current stop.
- **ADD TO POSITION** (averaging down) with the lot size and the new average entry.
- **STOP WIDENED** (moving the stop): stay in, with the new stop level.
- **CLOSE** with the reason (target, stop, gave back, panic, time exit, margin call) and the new account value, followed by the next trade size.
- **STOP TRADING** when the account is at or below 20% of the starting value.

If the user skipped days and something triggered earlier, say on which date it triggered and book it at that day's close, as the model does. Events that happened on skipped days (for example a lot added two days ago) must be listed under the headline so nothing is silently applied.

Also show: account value and change since start, win streak, size multiplier, a small chart of price since entry with entry, lots, target and stop drawn as lines, and a table of closed trades.

**State** is saved to a JSON file in the user's app-data folder (`QStandardPaths.AppDataLocation`) after every change. It must record the last processed session date so that pressing Update twice never applies the same day twice. "Reset" asks for confirmation.

**Status bar note:** "US markets close at 5:00am Perth (4:00am mid-March to early November). Run after 6:00am Perth, Tuesday to Saturday." Derive the displayed latest-session date from the data, not the system clock.

## Tests (write these before the UI)

In `tests/test_engine.py`, using `sp500.csv`:

1. **Equivalence with the reference.** For every preset, with and without each of the four switches, for at least 4 start indices and entries `long` and `trend`: the final account from `engine.replay(...)` equals `retail_sim_v2.simulate(...)[0]` to within 1e-9, and the number of trades matches. (For `random`, pass the same `numpy.random.default_rng(seed)` to both and consume random numbers in the same order.)
2. **Day-by-day equals batch.** Feeding closes one day at a time through `step` (as the Signal tab does) gives the same trades and final account as `replay` (as the Report tab does).
3. **Idempotent update.** Processing the same session twice changes nothing.
4. **Skipped days.** Processing five new sessions in one update gives the same state as processing them one at a time.
5. **Data cleaning.** An unfinished last bar and null closes are dropped.

Do not change `retail_sim_v2.py` to make tests pass. If the engine disagrees with it, the engine is wrong.

## Acceptance criteria

- `pip install -r requirements.txt` then `python -m app.main` opens the window on Windows, macOS and Linux.
- `pytest` passes.
- Report tab reproduces this known case from `sp500.csv`: S&P 500, long from 2025-02-18, later trades same side, 1 year, no extra switches. "Disciplined" ends at about +10.9% after 30 trades, and "All combined" is wiped out in early April 2025.
- Signal tab: open a short at 2026-10-20 on the "All combined" profile using `sp500.csv`; the first Update reports a take-profit close dated 2026-10-23.
- The window stays responsive during downloads and Monte Carlo runs.
- Every screen that shows results carries the line: "Simulation for research only. Not financial advice. This app places no orders."

## Out of scope

No broker integration, no order placement, no intraday data, no accounts or cloud sync, no parameter optimisation.

## How to work

Start by reading the four files. Write `engine.py` and its tests first and get them passing, then `data.py`, then the two tabs. Show me the test output and a screenshot description of each tab when done, and list anything in this spec you found ambiguous along with the choice you made.
