"""FastAPI server for the Retail Behaviour Model: Report tab.

Run locally:   uvicorn web.server:app --reload
The model comes from app/engine.py and prices from app/data.py, unchanged from the desktop app.
Research tool only: it places no orders.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import data, engine

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC = os.path.join(HERE, 'static')
CACHE_DIR = os.path.join(tempfile.gettempdir(), 'behaviour_model_cache')
BUNDLED_SP500 = os.path.join(ROOT, 'sp500.csv')
PRICE_TTL = 30 * 60                  # seconds a download is reused, to stay polite to Yahoo
SYMBOL_RE = re.compile(r'^[A-Za-z0-9.^=\-]{1,15}$')
DISCLAIMER = 'Simulation for research only. Not financial advice. This app places no orders.'
FOLLOW = [
    ('random_one', "Random long/short: one trader's story"),
    ('same', 'Same side as my entry'),
    ('trend', 'Follow the 20-day trend'),
    ('chase', f'Chase the last {engine.CHASE_DAYS} days (long after a rise, short after a fall)'),
    ('random', f'Random long/short: median of {engine.MC_RUNS} traders'),
]
HORIZONS = [(252, '1 year (252 days)'), (126, '6 months (126 days)'), (63, '3 months (63 days)')]

app = FastAPI(title='Retail Behaviour Model', docs_url=None, redoc_url=None)
_memory: dict = {}                   # symbol -> (fetched_at, PriceSeries)
_lock = threading.Lock()
_heavy = threading.Semaphore(2)      # at most two replays at once on a small instance


class CustomProfile(BaseModel):
    tp: float = Field(gt=0, le=1)
    sl: Optional[float] = Field(default=None, gt=0, le=1)
    lev: float = Field(default=1, gt=0, le=100)
    revenge: bool = False
    overconf: bool = False


class ReportRequest(BaseModel):
    symbol: str = '^GSPC'
    date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    side: str = Field(default='long', pattern='^(long|short)$')
    follow: str = 'random_one'
    trader: int = Field(default=1, ge=1, le=engine.MC_RUNS)
    start: float = Field(default=10_000, gt=0, le=1e12)
    horizon: int = 252
    switches: dict[str, bool] = {}
    custom: Optional[CustomProfile] = None


def load_prices(symbol: str) -> data.PriceSeries:
    """Completed daily closes for a symbol: memory cache, then Yahoo, then any saved copy."""
    offline = os.environ.get('BEHAVIOUR_MODEL_CSV')          # tests and offline use
    if offline:
        return data.PriceSeries(symbol, data.load_csv(offline), 'csv')
    with _lock:
        hit = _memory.get(symbol)
        if hit and time.time() - hit[0] < PRICE_TTL:
            return hit[1]
    try:
        series = data.get_prices(symbol, cache_dir=CACHE_DIR, max_age=PRICE_TTL)
    except ValueError as ex:
        if symbol == '^GSPC' and os.path.exists(BUNDLED_SP500):
            rows = data.load_csv(BUNDLED_SP500)
            return data.PriceSeries(symbol, rows, 'csv', f'{ex} Using the saved S&P 500 history to {rows[-1][0]} instead.')
        raise
    if not series.note:                                      # do not pin a stale fallback in memory
        with _lock:
            _memory[symbol] = (time.time(), series)
    return series


def entry_index(dates: list, date: str) -> int:
    import bisect
    if date not in dates:
        i = bisect.bisect_left(dates, date)
        near = [d for d in (dates[i - 1] if i > 0 else None, dates[i] if i < len(dates) else None) if d]
        raise ValueError(f'{date} is not a trading day in the data ({dates[0]} to {dates[-1]}). '
                         f'Nearest trading day{"s" if len(near) > 1 else ""}: {" and ".join(near)}.')
    i = dates.index(date)
    if i >= len(dates) - 1:
        raise ValueError(f'{date} is the latest close, so there is nothing after it to replay yet. Pick an earlier date.')
    return i


def trade_json(t: engine.Trade) -> dict:
    return dict(entry=t.entry_date, exit=t.exit_date, side='Long' if t.direction > 0 else 'Short', size=t.size,
                lots=t.lots, move=t.move, account_after=t.account_after,
                reason=engine.reason_label(t.reason, t.stop_moved))


@app.get('/api/meta')
def meta() -> dict:
    return dict(
        securities=[dict(symbol=s, name=n) for s, n in data.SECURITIES],
        profiles=[dict(name=p.name, describe=p.describe(), shown=p.name in engine.DEFAULT_SHOWN) for p in engine.PRESETS],
        switches=[dict(key=k, label=label, tip=tip, default=(k, label, tip) in engine.SWITCHES)
                  for k, label, tip in engine.ALL_SWITCHES],
        follow=[dict(key=k, label=label) for k, label in FOLLOW],
        horizons=[dict(days=d, label=label) for d, label in HORIZONS],
        runs=engine.MC_RUNS, disclaimer=DISCLAIMER)


@app.post('/api/report')
def report(req: ReportRequest) -> dict:
    symbol = req.symbol.strip().upper()
    switch_keys = {k for k, _, _ in engine.ALL_SWITCHES}
    try:
        if not SYMBOL_RE.match(symbol):
            raise ValueError('That does not look like a Yahoo symbol. Use letters, digits and . ^ = - only.')
        if req.follow not in dict(FOLLOW):
            raise ValueError('Unknown rule for later trades.')
        if req.horizon not in dict(HORIZONS):
            raise ValueError('Unknown replay length.')
        switches = {k: bool(v) for k, v in req.switches.items() if k in switch_keys}
        profiles = list(engine.PRESETS)
        if req.custom:
            base = engine.Profile('Custom', req.custom.tp, req.custom.sl, req.custom.lev, req.custom.revenge, req.custom.overconf)
            profiles.append(base.with_switches(name=f'Custom: {base.describe()}'))
        profiles = [p.with_switches(**switches) for p in profiles]
        series = load_prices(symbol)
        dates, closes = series.dates, series.closes
        i0 = entry_index(dates, req.date)
        with _heavy:
            rep = engine.run_report(closes, dates, i0, req.side, req.follow, req.horizon, profiles, trader=req.trader)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex

    results = []
    for r in rep['results']:
        ft = r['first']
        out = dict(name=r['name'], describe=r['profile'].describe(), random=r['random'], curve=r['curve'],
                   opp_curve=r['opp_curve'], final=r['final'], wiped=r['wiped'], ntr=r['ntr'], deposited=r['deposited'],
                   opp_final=r['opp_final'], opp_dd=r['opp_dd'],
                   first=dict(reason=engine.reason_label(ft.reason, ft.stop_moved), exit=ft.exit_date, move=ft.move))
        if r['random']:
            out.update(lose=r['lose'], trades=None)
        else:
            out.update(wins=r['wins'], result=r['result'], dd=r['dd'], trades=[trade_json(t) for t in r['trades']])
        results.append(out)
    source = {'yahoo': 'Live prices from Yahoo Finance', 'cache': 'Prices from Yahoo Finance',
              'csv': 'Saved price history'}[series.source]
    return dict(
        name=data.security_name(symbol), symbol=symbol, source=source, note=series.note, latest=dates[-1],
        dates=rep['dates'], price=rep['price'], direction=rep['direction'], follow=req.follow, trader=req.trader,
        runs=rep['runs'], start=req.start, extras=[s.lower() for s in profiles[0].switch_names()], results=results,
        disclaimer=DISCLAIMER)


@app.get('/healthz')
def healthz() -> dict:
    return dict(ok=True)


@app.get('/')
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC, 'index.html'), headers={'Cache-Control': 'no-cache'})


app.mount('/static', StaticFiles(directory=STATIC), name='static')
