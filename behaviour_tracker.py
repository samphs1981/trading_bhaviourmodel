"""Behaviour tracker: applies the simulated retail-trader rules to a live position, one day at a time.

Run:   python behaviour_tracker.py            (opens http://127.0.0.1:8765 in your browser)
       python behaviour_tracker.py --csv sp500.csv     (offline: read closes from a date,close CSV instead of Yahoo)

Rules are identical to retail_sim_v2.py: exits are checked on each daily close, sizing changes apply to the NEXT trade.
Optional extra behaviours: averaging down, moving the stop, panic exit, giving-back exit.
State is kept in tracker_state.json next to this file. Research tool only - it places no orders."""
import json, os, sys, time, html, urllib.request, urllib.parse, webbrowser, datetime as dt
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__)); STATE = os.path.join(HERE, 'tracker_state.json')
COST, MAX_HOLD, RUIN_FRAC, PORT = 2e-4, 120, 0.2, 8765
ADD_STEP, MAX_ADDS, PANIC_DAY = 0.015, 2, 0.02
EXTRAS = [('avg', 'Averaging down (add a lot every 1.5% against the first entry, max 2 adds)'),
          ('move', 'Moving the stop (first time the stop is hit, widen it by half instead)'),
          ('panic', 'Panic exit (close after a 2% day against a losing position)'),
          ('give', 'Giving-back exit (close a winner that falls half the target from its peak)')]
PROFILES = {  # name: (take profit, stop loss, leverage, revenge, overconfidence) - same as retail_sim.py
 'All combined (5x, revenge, overconfidence, +1% / -4%)':  (.01, .04, 5, True,  True),
 'All combined, never cut losers':                         (.01, None, 5, True,  True),
 'Cut winners, hold losers + 5x leverage':                 (.01, .04, 5, False, False),
 'Cut winners, hold losers + 5x + revenge sizing':         (.01, .04, 5, True,  False),
 'Cut winners, hold losers + 5x + overconfidence':         (.01, .04, 5, False, True),
 'Cut winners, hold losers: +1% / -4%, no leverage':       (.01, .04, 1, False, False),
 'Never cut losers: +1% target, no stop, no leverage':     (.01, None, 1, False, False),
 'Disciplined: +2% / -2%, no leverage':                    (.02, .02, 1, False, False),
 'Disciplined +2% / -2% but 5x leverage':                  (.02, .02, 5, False, False),
 'Cut losers, let winners run: +4% / -1%, no leverage':    (.04, .01, 1, False, False),
}

# ---------- data ----------
def fetch_closes(symbol, csv=None):
    """Return list of (date 'YYYY-MM-DD', close) for COMPLETED US sessions only, oldest first."""
    if csv:
        rows = [l.strip().split(',') for l in open(csv) if l[:1].isdigit()]
        return [(r[0], float(r[1])) for r in rows]
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=1y&interval=1d'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    r = json.load(urllib.request.urlopen(req, timeout=20))['chart']['result'][0]
    off = r['meta'].get('gmtoffset', -18000); out = []
    for ts, c in zip(r['timestamp'], r['indicators']['quote'][0]['close']):
        if c is not None:
            out.append((dt.datetime.fromtimestamp(ts+off, dt.timezone.utc).strftime('%Y-%m-%d'), round(c, 2), ts))
    reg = r['meta'].get('currentTradingPeriod', {}).get('regular', {})
    if out and reg and reg.get('start', 0) <= out[-1][2] and time.time() < reg.get('end', 0) + 600:
        out.pop()                                     # today's US session has not finished: ignore the partial bar
    return [(d, c) for d, c, _ in out]

# ---------- model (mirrors simulate() in retail_sim.py) ----------
def new_state(start_equity, profile, symbol, extras=()):
    return dict(start=start_equity, equity=start_equity, profile=profile, symbol=symbol, extras=list(extras), mult=1.0, wins_row=0,
                position=None, history=[], stopped=False)

def next_size(st):
    tp, sl, lev, rv, oc = PROFILES[st['profile']]
    return lev*st['mult']

def open_position(st, closes, date, direction, price=None):
    d = dict(closes)
    if st['position'] or st['stopped']: raise ValueError('A position is already open, or trading has stopped.')
    if date not in d: raise ValueError(f'{date} is not a completed US trading day in the data (latest is {closes[-1][0]}).')
    size = next_size(st)
    st['position'] = dict(date=date, dir=1 if direction == 'long' else -1, entry=float(price) if price else d[date],
                          size=size, notional=round(size*st['equity'], 2))

def advance(st, closes):
    """Walk every completed close since entry, in order (same rules as retail_sim_v2.py). Returns today's instruction."""
    tp, sl, lev, rv, oc = PROFILES[st['profile']]; ruin = RUIN_FRAC*st['start']; p = st['position']; x = st.get('extras', [])
    if st['stopped']: return dict(action='STOP', why='Account is at or below 20% of its starting value. The model stops trading here.')
    if not p: return dict(action='FLAT')
    dates = [d for d, _ in closes]; i0 = dates.index(p['date']) if p['date'] in dates else None
    if i0 is None: return dict(action='HOLD', why='Entry date not found in data.', r=0, held=0, lots=[p['entry']], stop=sl, events=[], tp=tp)
    d = p['dir']; base = p['size']; lots = [p['entry']]; stop = sl; peak = 0.0; moved = False; r = 0.0; held = 0; events = []
    for k in range(i0+1, len(closes)):
        c = closes[k][1]; held = k-i0; rl = [d*(c/q-1) for q in lots]; r = sum(rl)/len(rl); size = base*len(lots); pnl = size*r
        day = d*(c/closes[k-1][1]-1); peak = max(peak, r); why = None
        if r >= tp: why = f'Take profit: up {r:.2%}, target is +{tp:.0%}'
        elif 'give' in x and peak >= tp/2 and r <= peak-tp/2: why = f'Gave back profit: was up {peak:.2%}, now {r:+.2%}'
        elif 'panic' in x and r < 0 and day <= -PANIC_DAY: why = f'Panic exit: {-day:.2%} against you in one day while losing'
        elif stop is not None and r <= -stop:
            if 'move' in x and not moved:
                stop *= 1.5; moved = True; events.append(dict(date=closes[k][0], kind='STOP WIDENED', text=f'Stop of -{stop/1.5:.1%} was hit (down {-r:.2%}). Model stays in and widens the stop to -{stop:.1%}.'))
            else: why = f'Stop loss: down {-r:.2%}, stop is -{stop:.1%}'
        if why is None:
            if held >= MAX_HOLD: why = f'Held {MAX_HOLD} trading days: time exit'
            elif st['equity']*(1+pnl) <= ruin: why = 'Margin call: account would be at or below 20% of its starting value'
        if why:
            before = st['equity']; st['equity'] = round(before*max(0.0, 1+pnl-2*COST*size), 2); old_mult = st['mult']
            if pnl > 0:
                st['wins_row'] += 1; st['mult'] = 2.0 if (oc and st['wins_row'] >= 2) else 1.0
            else:
                st['wins_row'] = 0; st['mult'] = min(st['mult']*2, 4.0) if rv else 1.0
            st['history'].append(dict(entry_date=p['date'], exit_date=closes[k][0], dir='long' if d > 0 else 'short', entry=p['entry'],
                exit=c, size=size, lots=len(lots), move=round(r, 5), equity_before=before, equity_after=st['equity'], why=why))
            st['position'] = None; st['stopped'] = st['equity'] <= ruin
            return dict(action='CLOSE', why=why, exit_date=closes[k][0], exit=c, r=r, late=(k < len(closes)-1),
                        old_mult=old_mult, stopped=st['stopped'], events=events)
        if 'avg' in x and len(lots) <= MAX_ADDS and rl[0] <= -ADD_STEP*len(lots):
            lots.append(c); events.append(dict(date=closes[k][0], kind='ADD TO POSITION', text=f'Down {-rl[0]:.2%} from the first entry. Model adds another lot of {base:.0f}x account (${p["notional"]:,.0f}) at {c:,.2f}. Position is now {len(lots)} lots.'))
    return dict(action='HOLD', r=r, held=held, tp=tp, sl=sl, stop=stop, lots=lots, events=events, last=closes[-1][0])

def sizing_note(st):
    tp, sl, lev, rv, oc = PROFILES[st['profile']]; m = st['mult']
    if m == 1: why = 'normal size'
    elif st['wins_row'] >= 2: why = f'DOUBLED: {st["wins_row"]} wins in a row (overconfidence rule)'
    else: why = f'{m:.0f}x normal: doubling after a loss (revenge rule, max 4x)'
    return f'{lev}x leverage x {m:.0f} = {lev*m:.0f}x your account = ${lev*m*st["equity"]:,.0f} position ({why})'

# ---------- tiny web UI ----------
def load():  return json.load(open(STATE)) if os.path.exists(STATE) else None
def save(s): json.dump(s, open(STATE, 'w'), indent=1)
CSS = '''body{font:15px/1.5 system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#111;background:#f7f7f5}
.card{background:#fff;border:1px solid #ddd;border-radius:10px;padding:16px;margin:14px 0}.big{font-size:26px;font-weight:700;margin:0 0 4px}
.CLOSE,.STOP{border-left:8px solid #d03b3b}.HOLD{border-left:8px solid #2a78d6}.FLAT{border-left:8px solid #1baf7a}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid #eee;padding:5px 6px;text-align:left}
input,select,button{font:inherit;padding:6px 8px;margin:4px 6px 4px 0}button{cursor:pointer}.mut{color:#666;font-size:13px}.err{color:#b00020;font-weight:600}'''

def page(st, closes, msg='', err=''):
    e = html.escape; b = [f'<!doctype html><meta charset=utf-8><title>Behaviour tracker</title><style>{CSS}</style><h1>Behaviour tracker</h1>']
    if err: b.append(f'<p class=err>{e(err)}</p>')
    if st is None:
        opts = ''.join(f'<option>{e(k)}</option>' for k in PROFILES)
        xc = ''.join(f'<label style="display:block"><input type=checkbox name=extras value={k}> {e(v)}</label>' for k, v in EXTRAS)
        b.append(f'''<div class=card><form method=post action=/setup><p class=big>Set up</p>
        Starting account ($) <input name=start type=number step=any value=10000 required><br>
        Behaviour profile <select name=profile>{opts}</select><br>
        Yahoo symbol <input name=symbol value="^GSPC"><br><span class=mut>Extra behaviours (optional):</span><br>{xc}<button>Start</button></form></div>''')
        return ''.join(b)
    last = closes[-1] if closes else ('?', 0)
    ins = advance(st, closes) if closes else dict(action='HOLD', why='No price data', r=0, held=0); save(st)
    a = ins['action']; xn = ', '.join(v.split(' (')[0].lower() for k, v in EXTRAS if k in st.get('extras', [])); b.append(f'<p class=mut>{e(st["profile"])}{" + " + xn if xn else ""} &middot; {e(st["symbol"])} &middot; latest completed US close: <b>{last[0]}</b> at {last[1]:,.2f}</p>')
    if a == 'CLOSE':
        late = f' <b>This triggered on {ins["exit_date"]}, before the latest close</b>; the model books it at that day\'s close.' if ins['late'] else ''
        b.append(f'<div class="card CLOSE"><p class=big>CLOSE the position</p>{e(ins["why"])}. Model exit: {ins["exit_date"]} close {ins["exit"]:,.2f}.{late}<br>Account is now <b>${st["equity"]:,.2f}</b>.</div>')
        a = 'STOP' if st['stopped'] else 'FLAT'
    if a == 'STOP': b.append('<div class="card STOP"><p class=big>STOP trading</p>Account is at or below 20% of its starting value. The model ends here.</div>')
    if ins.get('events') and a != 'HOLD':
        b.append('<div class=card><b>Before that, while the position was open</b><br>' + '<br>'.join(f'{v["date"]}: <b>{v["kind"]}</b>. {e(v["text"])}' for v in ins['events']) + '</div>')
    if a == 'HOLD':
        p = st['position']; tp = ins.get('tp'); stop = ins.get('stop'); lots = ins.get('lots', [p['entry']]); n = len(lots); inv = sum(1/q for q in lots)/n
        lvl = lambda v: (1+p['dir']*v)/inv                                   # close at which the average lot return equals v
        lv = (f'take profit at {lvl(tp):,.2f}' + (f', stop loss at {lvl(-stop):,.2f}' if stop else ', no stop loss')) if tp else ''
        ev = ins.get('events', []); today = [v for v in ev if v['date'] == ins.get('last')]; head = today[-1]['kind'] if today else 'HOLD - do nothing'
        evs = ('<br>' + '<br>'.join(f'{v["date"]}: <b>{v["kind"]}</b>. {e(v["text"])}' for v in ev)) if ev else ''
        b.append(f'''<div class="card {"CLOSE" if today else "HOLD"}"><p class=big>{head}</p>{"Long" if p["dir"]>0 else "Short"} from {p["date"]} at {p["entry"]:,.2f},
        {n} lot(s) of {p["size"]:.0f}x account = {p["size"]*n:.0f}x (${p["notional"]*n:,.0f}). Average move: <b>{ins["r"]:+.2%}</b> after {ins["held"]} trading day(s); on the account: {p["size"]*n*ins["r"]:+.2%}.{evs}<br>
        <span class=mut>Close when a daily close reaches: {lv}. Sizing after wins and losses applies to the next trade.</span></div>''')
    if a == 'FLAT':
        b.append(f'''<div class="card FLAT"><p class=big>No open position</p>Next trade size: <b>{e(sizing_note(st))}</b>
        <form method=post action=/open>US session date of entry <input name=date type=date value="{last[0]}" required>
        <select name=dir><option value=long>Long</option><option value=short>Short</option></select>
        Entry price <input name=price type=number step=any placeholder="that day's close"><button>Open position</button></form>
        <span class=mut>Leave the price blank to use that day's close, as the simulation does.</span></div>''')
    b.append(f'<div class=card>Account <b>${st["equity"]:,.2f}</b> (started ${st["start"]:,.2f}, {st["equity"]/st["start"]-1:+.1%}) &middot; wins in a row: {st["wins_row"]} &middot; size multiplier for next trade: {st["mult"]:.0f}x<form method=get action=/><button>Refresh prices</button></form></div>')
    if st['history']:
        rows = ''.join(f'<tr><td>{h["entry_date"]}</td><td>{h["exit_date"]}</td><td>{h["dir"]}</td><td>{h["size"]:.0f}x</td><td>{h["move"]:+.2%}</td><td>${h["equity_after"]:,.2f}</td><td>{e(h["why"].split(":")[0])}</td></tr>' for h in reversed(st['history']))
        b.append(f'<div class=card><b>Closed trades</b><table><tr><th>Entry</th><th>Exit</th><th>Side</th><th>Size</th><th>Move</th><th>Account after</th><th>Reason</th></tr>{rows}</table></div>')
    b.append('<form method=post action=/reset onsubmit="return document.getElementById(\'ok\').checked"><label class=mut><input type=checkbox id=ok> I want to erase everything</label> <button>Reset</button></form>')
    return ''.join(b)

class H(BaseHTTPRequestHandler):
    csv = None
    def log_message(self, *a): pass
    def _send(self, body):
        d = body.encode(); self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(d))); self.end_headers(); self.wfile.write(d)
    def _closes(self, st):
        try: return fetch_closes(st['symbol'] if st else '^GSPC', self.csv), ''
        except Exception as ex: return [], f'Could not get prices: {ex}'
    def do_GET(self):
        st = load(); closes, err = self._closes(st) if st else ([], ''); self._send(page(st, closes, err=err))
    def do_POST(self):
        q = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode()); f = {k: v[0] for k, v in q.items()}
        st = load(); err = ''
        try:
            if self.path == '/setup' and f.get('profile') in PROFILES: st = new_state(float(f['start']), f['profile'], f.get('symbol', '^GSPC').strip() or '^GSPC', q.get('extras', [])); save(st)
            elif self.path == '/reset':
                if os.path.exists(STATE): os.remove(STATE)
                st = None
            elif self.path == '/open' and st:
                closes, err = self._closes(st)
                if closes: open_position(st, closes, f['date'], f['dir'], f.get('price')); save(st)
        except Exception as ex: err = str(ex)
        closes, e2 = self._closes(st) if st else ([], ''); self._send(page(st, closes, err=err or e2))

if __name__ == '__main__':
    if '--csv' in sys.argv: H.csv = sys.argv[sys.argv.index('--csv')+1]
    srv = HTTPServer(('127.0.0.1', PORT), H); url = f'http://127.0.0.1:{PORT}'
    print(f'Behaviour tracker running at {url}  (Ctrl+C to stop)')
    if '--no-browser' not in sys.argv: webbrowser.open(url)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
