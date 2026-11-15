"""Behaviour report: pick a security, an entry date and long/short, and replay every behaviour profile from that entry.

Run:   python behaviour_report.py                    (opens http://127.0.0.1:8766 in your browser)
       python behaviour_report.py --csv sp500.csv    (offline: use a date,close CSV instead of Yahoo)

Your first trade is the one you enter. Later trades follow the rule you choose (same side, trend, or random).
Rules are identical to retail_sim.py. Each run also saves the report as an HTML file next to this script.
Research tool only - it places no orders."""
import json, os, sys, time, html, random, urllib.request, urllib.parse, webbrowser, datetime as dt
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__)); PORT = 8766
COST, MAX_HOLD, RUIN, RUNS = 2e-4, 120, 0.2, 300
SECURITIES = [('^GSPC','S&P 500'),('^IXIC','Nasdaq Composite'),('^AXJO','ASX 200'),('^FTSE','FTSE 100'),('^N225','Nikkei 225'),
    ('GC=F','Gold'),('BZ=F','Brent crude'),('AUDUSD=X','AUD/USD'),('BTC-USD','Bitcoin'),('AAPL','Apple'),('MSFT','Microsoft'),
    ('NVDA','Nvidia'),('TSLA','Tesla'),('BHP.AX','BHP'),('CBA.AX','Commonwealth Bank')]
PROFILES = [  # name, take profit, stop loss, leverage, revenge, overconfidence, shown on chart by default
 ('Disciplined: +2% / -2%',                     .02, .02, 1, False, False, True),
 ('Cut losers, let winners run: +4% / -1%',     .04, .01, 1, False, False, False),
 ('Cut winners, hold losers: +1% / -4%',        .01, .04, 1, False, False, True),
 ('Never cut losers: +1%, no stop',             .01, None, 1, False, False, False),
 ('Disciplined, 5x leverage',                   .02, .02, 5, False, False, False),
 ('Cut winners, hold losers, 5x leverage',      .01, .04, 5, False, False, True),
 ('Hold losers, 5x + revenge sizing',           .01, .04, 5, True,  False, False),
 ('Hold losers, 5x + overconfidence sizing',    .01, .04, 5, False, True,  False),
 ('All combined',                               .01, .04, 5, True,  True,  True),
 ('All combined, never cut losers',             .01, None, 5, True,  True,  False),
]
COLORS = ['#2a78d6','#eb6834','#1baf7a','#eda100','#e87ba4','#008300','#6250d6','#e34948']

def fetch_closes(symbol, csv=None):
    """(date, close) for completed sessions, oldest first."""
    if csv:
        return [(r[0], float(r[1])) for r in (l.strip().split(',') for l in open(csv) if l[:1].isdigit())]
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=10y&interval=1d'
    r = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=20))['chart']['result'][0]
    off = r['meta'].get('gmtoffset', 0); out = []
    for ts, c in zip(r['timestamp'], r['indicators']['quote'][0]['close']):
        if c is not None and c > 0: out.append((dt.datetime.fromtimestamp(ts+off, dt.timezone.utc).strftime('%Y-%m-%d'), c, ts))
    reg = r['meta'].get('currentTradingPeriod', {}).get('regular', {})
    if out and reg and reg.get('start', 0) <= out[-1][2] and time.time() < reg.get('end', 0)+600: out.pop()
    return [(d, c) for d, c, _ in out]

EXTRAS = [('avg', 'Averaging down (add a lot every 1.5% against the first entry, max 2 adds)'),
          ('move', 'Moving the stop (first time the stop is hit, widen it by half instead)'),
          ('panic', 'Panic exit (close after a 2% day against a losing position)'),
          ('give', 'Giving-back exit (close a winner that falls half the target from its peak)')]
ADD_STEP, MAX_ADDS, PANIC_DAY = 0.015, 2, 0.02

def run_path(c, i0, first_dir, prof, follow, rng, horizon=252, extras=()):
    """Replay one trader from index i0 (same rules as retail_sim_v2.py). Returns (daily equity from 1.0, trades)."""
    _, tp, sl, lev, rv, oc, _ = prof; end = min(i0+horizon-1, len(c)-1)
    avg_down, move_stop, panic, give_back = ('avg' in extras, 'move' in extras, 'panic' in extras, 'give' in extras)
    eq = 1.0; t = i0; mult = 1.0; wins = 0; daily = [1.0]*(end-i0+1); trades = []; first = True
    while t < end and eq > RUIN:
        if first or follow == 'same': d = first_dir
        elif follow == 'trend': d = 1 if (t >= 20 and c[t] > c[t-20]) else -1
        else: d = 1 if rng.random() < .5 else -1
        first = False; base = lev*mult; lots = [c[t]]; k = t; stop = sl; peak = 0.0; moved = False
        while True:
            k += 1; rl = [d*(c[k]/p-1) for p in lots]; r = sum(rl)/len(rl); size = base*len(lots); pnl = size*r
            day = d*(c[k]/c[k-1]-1); peak = max(peak, r); daily[k-i0] = max(0.0, eq*(1+pnl)); why = None
            if r >= tp: why = 'Take profit'
            elif give_back and peak >= tp/2 and r <= peak-tp/2: why = 'Gave back profit'
            elif panic and r < 0 and day <= -PANIC_DAY: why = 'Panic exit'
            elif stop is not None and r <= -stop:
                if move_stop and not moved: stop *= 1.5; moved = True
                else: why = 'Stop loss' + (' (after widening)' if moved else '')
            if why is None:
                if k-t >= MAX_HOLD: why = 'Time exit (120 days)'
                elif k >= end: why = 'End of period (still open)'
                elif eq*(1+pnl) <= RUIN: why = 'Margin call'
            if why: break
            if avg_down and len(lots) <= MAX_ADDS and rl[0] <= -ADD_STEP*len(lots): lots.append(c[k])
        eq *= max(0.0, 1+pnl-2*COST*size); daily[k-i0] = eq
        trades.append(dict(i=t, k=k, dir=d, size=size, lots=len(lots), r=r, eq=eq, why=why))
        if pnl > 0: wins += 1; mult = 2.0 if (oc and wins >= 2) else 1.0
        else: wins = 0; mult = min(mult*2, 4.0) if rv else 1.0
        t = k
    for j in range(t-i0+1, len(daily)): daily[j] = eq
    return daily, trades

def analyse(closes, date, direction, follow, horizon=252, extras=()):
    dates = [d for d, _ in closes]; c = [x for _, x in closes]
    if date not in dates: raise ValueError(f'{date} is not a trading day in the data ({dates[0]} to {dates[-1]}). Pick a day the market was open.')
    i0 = dates.index(date)
    if i0 >= len(c)-1: raise ValueError(f'{date} is the latest close, so there is nothing after it to replay yet. Pick an earlier date.')
    d = 1 if direction == 'long' else -1; res = []
    for pi, prof in enumerate(PROFILES):
        if follow == 'random':
            runs = [run_path(c, i0, d, prof, follow, random.Random(1000+n), horizon, extras) for n in range(RUNS)]
            n = len(runs[0][0]); curve = [sorted(r[0][j] for r in runs)[RUNS//2] for j in range(n)]
            finals = [r[0][-1] for r in runs]; first = runs[0][1][0]
            res.append(dict(name=prof[0], curve=curve, first=first, final=sorted(finals)[RUNS//2], lose=sum(f < 1 for f in finals)/RUNS,
                            wiped=sum(f <= RUIN for f in finals)/RUNS, trades=None, ntr=sum(len(r[1]) for r in runs)/RUNS))
        else:
            curve, tr = run_path(c, i0, d, prof, follow, None, horizon, extras); peak = 1.0; dd = 0.0
            for v in curve: peak = max(peak, v); dd = min(dd, v/peak-1)
            res.append(dict(name=prof[0], curve=curve, first=tr[0], final=curve[-1], dd=dd, wiped=curve[-1] <= RUIN, trades=tr,
                            ntr=len(tr), wins=sum(t['r'] > 0 for t in tr)))
    return dict(dates=dates[i0:i0+len(res[0]['curve'])], price=c[i0:i0+len(res[0]['curve'])], res=res, i0=i0, alld=dates)

# ---------- report rendering ----------
def svg_lines(series, dates, fmt, h=300, baseline=None):
    """series: list of (label, color, values). Lines with end labels, light grid, year/month ticks."""
    W, L, R, T, B = 900, 64, 90, 12, 26; n = len(dates)
    lo = min(min(v) for _, _, v in series); hi = max(max(v) for _, _, v in series)
    if baseline is not None: lo = min(lo, baseline); hi = max(hi, baseline)
    raw = (hi-lo)/4 or 1; mag = 10**len(str(int(raw)))/10 if raw >= 1 else 10.0**-2
    stp = next(m*mag for m in (1, 2, 2.5, 5, 10) if m*mag >= raw)                  # round tick spacing
    lo = (lo//stp)*stp; hi = -((-hi)//stp)*stp; ng = int(round((hi-lo)/stp))
    X = lambda k: L+(k/max(1, n-1))*(W-L-R); Y = lambda v: T+(hi-v)/(hi-lo)*(h-T-B); o = [f'<svg viewBox="0 0 {W} {h}" role="img">']
    for g in range(ng+1):
        v = lo+stp*g; o.append(f'<line x1="{L}" x2="{W-R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="#e1e0d9"/><text x="{L-6}" y="{Y(v)+4:.1f}" text-anchor="end">{fmt(v)}</text>')
    step = max(1, n//6)
    for k in range(0, n, step): o.append(f'<text x="{X(k):.1f}" y="{h-8}" text-anchor="middle">{dates[k]}</text>')
    if baseline is not None: o.append(f'<line x1="{L}" x2="{W-R}" y1="{Y(baseline):.1f}" y2="{Y(baseline):.1f}" stroke="#898781" stroke-dasharray="4 4"/>')
    ends = sorted(((Y(v[-1]), lab, col, v[-1]) for lab, col, v in series)); ly = []
    for y, *_ in ends: ly.append(max(y, ly[-1]+14) if ly else max(y, T+8))          # keep end labels from overlapping
    for lab, col, v in series:
        o.append('<path fill="none" stroke-width="2" stroke-linejoin="round" stroke="%s" d="%s"/>' % (col, ''.join(f'{"M" if k == 0 else "L"}{X(k):.1f} {Y(x):.1f}' for k, x in enumerate(v))))
    for (y, lab, col, val), yy in zip(ends, ly):
        o.append(f'<line x1="{W-R+2}" x2="{W-R+12}" y1="{y:.1f}" y2="{yy:.1f}" stroke="{col}"/><circle cx="{W-R+16}" cy="{yy:.1f}" r="4" fill="{col}"/><text x="{W-R+24}" y="{yy+4:.1f}" class="lab">{fmt(val)}</text>')
    leg = ''.join(f'<span class=lg><i style="background:{col}"></i>{html.escape(lab)}</span>' for lab, col, _ in series) if len(series) > 1 else ''
    return f'<div class=leg>{leg}</div>'+''.join(o)+'</svg>'

CSS = '''body{font:15px/1.5 system-ui,sans-serif;max-width:940px;margin:24px auto;padding:0 16px;color:#0b0b0b;background:#f9f9f7}
.card{background:#fcfcfb;border:1px solid #e1e0d9;border-radius:10px;padding:16px;margin:14px 0}h1{font-size:24px;margin:0}h2{font-size:17px;margin:0 0 6px}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-bottom:1px solid #eee;padding:5px 6px;text-align:left}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
svg{width:100%;height:auto;display:block}svg text{font-size:11px;fill:#898781}svg text.lab{fill:#52514e;font-size:12px}
input,select,button{font:inherit;padding:6px 8px;margin:4px 8px 4px 0}button{cursor:pointer}.mut{color:#52514e;font-size:13px}.err{color:#b00020;font-weight:600}
'''+'''.leg{margin:4px 0 6px}.lg{display:inline-block;margin-right:16px;font-size:13px;color:#52514e}.lg i{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
label.c{display:inline-block;margin-right:14px;font-size:13px}details{margin-top:8px}summary{cursor:pointer;font-size:13px}'''

def form(f=None, err=''):
    f = f or {}; e = html.escape; sym = f.get('symbol', '^GSPC')
    opts = ''.join(f'<option value="{e(s)}"{" selected" if s == sym else ""}>{e(n)} ({e(s)})</option>' for s, n in SECURITIES)
    sel = lambda k, v: ' selected' if f.get(k) == v else ''
    shown = f.get('show') or [str(i) for i, p in enumerate(PROFILES) if p[6]]
    checks = ''.join(f'<label class=c><input type=checkbox name=show value={i}{" checked" if str(i) in shown else ""}> {e(p[0])}</label>' for i, p in enumerate(PROFILES))
    xs = f.get('extras') or []
    xchecks = ''.join(f'<label class=c style="display:block"><input type=checkbox name=extras value={k}{" checked" if k in xs else ""}> {e(v)}</label>' for k, v in EXTRAS)
    return f'''<div class=card><form method=post action=/run>{f'<p class=err>{e(err)}</p>' if err else ''}
    Security <select name=symbol>{opts}</select> or Yahoo symbol <input name=custom size=10 value="{e(f.get('custom',''))}" placeholder="e.g. WES.AX"><br>
    Entry date <input type=date name=date value="{e(f.get('date',''))}" required>
    <select name=dir><option value=long{sel('dir','long')}>Long</option><option value=short{sel('dir','short')}>Short</option></select>
    Later trades <select name=follow><option value=same{sel('follow','same')}>Same side as my entry</option><option value=trend{sel('follow','trend')}>Follow the 20-day trend</option><option value=random{sel('follow','random')}>Random long/short ({RUNS} runs)</option></select><br>
    Starting account ($) <input type=number name=start step=any value="{e(f.get('start','10000'))}" style="width:110px">
    Replay for <select name=horizon><option value=252{sel('horizon','252')}>up to 1 year</option><option value=126{sel('horizon','126')}>up to 6 months</option><option value=63{sel('horizon','63')}>up to 3 months</option></select>
    <p class=mut style="margin:6px 0 2px">Extra behaviours, applied to every profile:</p>{xchecks}<p class=mut style="margin:6px 0 2px">Curves to draw (up to 8):</p>{checks}<br><button>Run model</button></form></div>'''

def report(f, a, name):
    e = html.escape; S = float(f.get('start') or 10000); money = lambda v: f'${v*S:,.0f}'; rnd = f['follow'] == 'random'
    show = [int(i) for i in f.get('show', [])][:8] or [i for i, p in enumerate(PROFILES) if p[6]]
    ser = [(a['res'][i]['name'], COLORS[n], [v*S for v in a['res'][i]['curve']]) for n, i in enumerate(show)]
    days = len(a['dates']); mv = a['price'][-1]/a['price'][0]-1; side = 'Long' if f['dir'] == 'long' else 'Short'
    follow = {'same': 'same side as the entry', 'trend': 'following the 20-day trend', 'random': f'random long/short, median of {RUNS} runs'}[f['follow']]
    xs = ', '.join(v.split(' (')[0].lower() for k, v in EXTRAS if k in f.get('extras', [])) or 'none'
    b = [f'<h1>{e(name)}: {side.lower()} from {a["dates"][0]}</h1><p class=mut>{days-1} trading days replayed, {a["dates"][0]} to {a["dates"][-1]}. Price moved {mv:+.1%} over the period. Later trades: {follow}. Extra behaviours: {xs}. Starting account {money(1)}.</p>']
    b.append(f'<div class=card><h2>Account value by behaviour profile</h2>{"<p class=mut>Each line is the median account across the runs; the first trade is the same in every run.</p>" if rnd else ""}{svg_lines(ser, a["dates"], lambda v: f"${v:,.0f}", baseline=S)}</div>')
    b.append(f'<div class=card><h2>{e(name)} price</h2>{svg_lines([("Close", "#52514e", a["price"])], a["dates"], lambda v: f"{v:,.2f}", h=200)}</div>')
    hd = '<th>Profile</th><th>Your first trade</th><th class=n>Move</th><th class=n>Trades</th>' + ('<th class=n>Median final</th><th class=n>Runs losing</th><th class=n>Runs wiped out</th>' if rnd else '<th class=n>Wins</th><th class=n>Final account</th><th class=n>Result</th><th class=n>Worst drawdown</th><th>Wiped out</th>')
    rows = []
    for r in a['res']:
        ft = r['first']; first = f'{e(ft["why"])} on {a["alld"][ft["k"]]}'
        if rnd: tail = f'<td class=n>{r["ntr"]:.0f}</td><td class=n>{money(r["final"])}</td><td class=n>{r["lose"]:.0%}</td><td class=n>{r["wiped"]:.0%}</td>'
        else: tail = f'<td class=n>{r["ntr"]}</td><td class=n>{r["wins"]}</td><td class=n>{money(r["final"])}</td><td class=n>{r["final"]-1:+.1%}</td><td class=n>{r["dd"]:.1%}</td><td>{"Yes" if r["wiped"] else "No"}</td>'
        rows.append(f'<tr><td>{e(r["name"])}</td><td>{first}</td><td class=n>{ft["r"]:+.2%}</td>{tail}</tr>')
    b.append(f'<div class=card><h2>Results</h2><table><tr>{hd}</tr>{"".join(rows)}</table><p class=mut>Wiped out = account at or below 20% of its starting value. Exits are taken at daily closes; costs are 2 basis points per side.</p></div>')
    if not rnd:
        logs = []
        for r in a['res']:
            tr = ''.join(f'<tr><td>{a["alld"][t["i"]]}</td><td>{a["alld"][t["k"]]}</td><td>{"Long" if t["dir"] > 0 else "Short"}</td><td class=n>{t["size"]:.0f}x</td><td class=n>{t["lots"]}</td><td class=n>{t["r"]:+.2%}</td><td class=n>{money(t["eq"])}</td><td>{e(t["why"])}</td></tr>' for t in r['trades'])
            logs.append(f'<details><summary>{e(r["name"])} - {r["ntr"]} trades</summary><table><tr><th>Entry</th><th>Exit</th><th>Side</th><th class=n>Size</th><th class=n>Lots</th><th class=n>Move</th><th class=n>Account after</th><th>Reason</th></tr>{tr}</table></details>')
        b.append(f'<div class=card><h2>Trade logs</h2>{"".join(logs)}</div>')
    return ''.join(b)

def page(body, title='Behaviour report'): return f'<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title><style>{CSS}</style>{body}'

class H(BaseHTTPRequestHandler):
    csv = None
    def log_message(self, *a): pass
    def _send(self, body):
        d = body.encode(); self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.send_header('Content-Length', str(len(d))); self.end_headers(); self.wfile.write(d)
    def do_GET(self): self._send(page('<h1>Behaviour report</h1><p class=mut>Pick a security, your entry date and side. The model replays every behaviour profile from that entry.</p>'+form()))
    def do_POST(self):
        q = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
        f = {k: v[0] for k, v in q.items()}; f['show'] = q.get('show', []); f['extras'] = q.get('extras', [])
        try:
            sym = (f.get('custom') or '').strip() or f.get('symbol', '^GSPC'); name = dict(SECURITIES).get(sym, sym)
            a = analyse(fetch_closes(sym, self.csv), f['date'], f.get('dir', 'long'), f.get('follow', 'same'), int(f.get('horizon', 252)), tuple(f.get('extras', [])))
            rep = report(f, a, name); fn = f'report_{"".join(ch if ch.isalnum() else "_" for ch in sym)}_{f["date"]}_{f.get("dir","long")}{"_"+"-".join(f["extras"]) if f["extras"] else ""}.html'
            open(os.path.join(HERE, fn), 'w', encoding='utf-8').write(page(rep, f'{name} behaviour report'))
            self._send(page(rep+f'<p class=mut>Saved as {html.escape(fn)} next to the script.</p><h2>Run another</h2>'+form(f)))
        except Exception as ex: self._send(page('<h1>Behaviour report</h1>'+form(f, f'Could not run: {ex}')))

if __name__ == '__main__':
    if '--csv' in sys.argv: H.csv = sys.argv[sys.argv.index('--csv')+1]
    srv = HTTPServer(('127.0.0.1', PORT), H); url = f'http://127.0.0.1:{PORT}'
    print(f'Behaviour report running at {url}  (Ctrl+C to stop)')
    if '--no-browser' not in sys.argv: webbrowser.open(url)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
