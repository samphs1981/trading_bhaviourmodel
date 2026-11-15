"""Price loading: Yahoo daily closes (urllib only) and date,close CSV files, with a small on-disk cache.

No Qt here. Everything returns a list of (date 'YYYY-MM-DD', close) for completed sessions, oldest first.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

YAHOO_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=10y&interval=1d'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
CACHE_MAX_AGE = 30 * 60          # seconds a cached download is reused without asking Yahoo again
SESSION_GRACE = 600              # seconds after the regular close before the last bar counts as final

SECURITIES = [
    ('^GSPC', 'S&P 500'), ('^IXIC', 'Nasdaq Composite'), ('^AXJO', 'ASX 200'), ('^FTSE', 'FTSE 100'),
    ('^N225', 'Nikkei 225'), ('GC=F', 'Gold'), ('BZ=F', 'Brent crude'), ('AUDUSD=X', 'AUD/USD'),
    ('BTC-USD', 'Bitcoin'), ('AAPL', 'Apple'), ('MSFT', 'Microsoft'), ('NVDA', 'Nvidia'), ('TSLA', 'Tesla'),
    ('BHP.AX', 'BHP'), ('CBA.AX', 'Commonwealth Bank'),
]


@dataclass
class PriceSeries:
    symbol: str
    rows: list            # [(date, close), ...]
    source: str           # 'yahoo' | 'cache' | 'csv'
    note: str = ''        # e.g. why a stale cache was used

    @property
    def dates(self) -> list:
        return [d for d, _ in self.rows]

    @property
    def closes(self) -> list:
        return [c for _, c in self.rows]


def security_name(symbol: str) -> str:
    return dict(SECURITIES).get(symbol, symbol)


def parse_chart(payload: dict, now: Optional[float] = None) -> list:
    """Turn a Yahoo v8 chart response into cleaned (date, close) rows.

    - dates use the exchange offset in meta.gmtoffset
    - null or non-positive closes are ignored
    - the last bar is dropped while its session has not finished
    """
    chart = payload.get('chart') or {}
    if chart.get('error'):
        raise ValueError(chart['error'].get('description') or 'Yahoo returned an error for this symbol.')
    result = (chart.get('result') or [None])[0]
    if not result or not result.get('timestamp'):
        raise ValueError('Yahoo returned no price data for this symbol.')
    meta = result.get('meta') or {}
    off = meta.get('gmtoffset') or 0
    closes = result['indicators']['quote'][0].get('close') or []
    out = []
    for ts, c in zip(result['timestamp'], closes):
        if ts is None or c is None or not c > 0:
            continue
        day = dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).strftime('%Y-%m-%d')
        if out and out[-1][0] == day:
            out[-1] = (day, float(c), ts)        # Yahoo sometimes repeats the latest day
        else:
            out.append((day, float(c), ts))
    reg = (meta.get('currentTradingPeriod') or {}).get('regular') or {}
    now = time.time() if now is None else now
    if out and reg and reg.get('start', 0) <= out[-1][2] and now < reg.get('end', 0) + SESSION_GRACE:
        out.pop()                                # this session has not finished: ignore the partial bar
    if not out:
        raise ValueError('No completed sessions in the data for this symbol.')
    return [(d, c) for d, c, _ in out]


def fetch_yahoo(symbol: str, timeout: float = 20) -> list:
    url = YAHOO_URL.format(symbol=urllib.parse.quote(symbol))
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            raise ValueError(f'Yahoo does not know the symbol "{symbol}".') from ex
        raise ValueError(f'Yahoo refused the request for "{symbol}" (HTTP {ex.code}).') from ex
    except (urllib.error.URLError, TimeoutError, OSError) as ex:
        raise ValueError(f'Could not reach Yahoo: {getattr(ex, "reason", ex)}') from ex
    return parse_chart(payload)


def load_csv(path: str) -> list:
    """Read a date,close CSV. Lines that do not start with a digit (headers) are skipped."""
    rows = {}
    with open(path, encoding='utf-8-sig') as fh:
        for line in fh:
            if not line[:1].isdigit():
                continue
            parts = line.strip().split(',')
            try:
                close = float(parts[1])
            except (IndexError, ValueError):
                continue
            if close > 0:
                rows[parts[0].strip()[:10]] = close
    if not rows:
        raise ValueError(f'No date,close rows found in {os.path.basename(path)}.')
    return sorted(rows.items())


def save_csv(path: str, rows: list) -> None:
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('date,close\n')
        for d, c in rows:
            fh.write(f'{d},{c}\n')


def default_cache_dir() -> str:
    return os.path.join(os.path.expanduser('~'), '.retail_behaviour_model', 'cache')


def _cache_file(cache_dir: str, symbol: str) -> str:
    safe = ''.join(ch if ch.isalnum() else '_' for ch in symbol)
    return os.path.join(cache_dir, f'{safe}.json')


def get_prices(symbol: str = '', csv_path: Optional[str] = None, cache_dir: Optional[str] = None,
               force: bool = False, max_age: float = CACHE_MAX_AGE) -> PriceSeries:
    """Load closes from a CSV if given, otherwise from the cache or Yahoo.

    A recent cache is reused; a stale one is refreshed. If Yahoo fails and any cache exists,
    the cache is returned with a note instead of raising.
    """
    if csv_path:
        return PriceSeries(symbol or os.path.basename(csv_path), load_csv(csv_path), 'csv')
    symbol = symbol.strip()
    if not symbol:
        raise ValueError('Choose a security or type a Yahoo symbol.')
    cache_dir = cache_dir or default_cache_dir()
    path = _cache_file(cache_dir, symbol)
    cached = None
    try:
        with open(path, encoding='utf-8') as fh:
            cached = json.load(fh)
        cached['rows'] = [(d, float(c)) for d, c in cached['rows']]
    except (OSError, ValueError, KeyError, TypeError):
        cached = None
    if cached and cached['rows'] and not force and time.time() - cached.get('fetched', 0) < max_age:
        return PriceSeries(symbol, cached['rows'], 'cache')
    try:
        rows = fetch_yahoo(symbol)
    except ValueError as ex:
        if cached and cached['rows']:
            when = dt.datetime.fromtimestamp(cached.get('fetched', 0)).strftime('%Y-%m-%d %H:%M')
            return PriceSeries(symbol, cached['rows'], 'cache', f'{ex} Using prices saved on {when}.')
        raise
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(dict(symbol=symbol, fetched=time.time(), rows=rows), fh)
        os.replace(tmp, path)
    except OSError:
        pass                                     # a cache that cannot be written is not an error
    return PriceSeries(symbol, rows, 'yahoo')
