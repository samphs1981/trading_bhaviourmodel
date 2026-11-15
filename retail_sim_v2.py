"""Retail-behaviour simulation, version 2: adds behaviours driven by the OPEN position's running profit or loss.
New switches (all off = identical to retail_sim.py):
  avg_down   add an equal-sized lot each time price is another 1.5% against the first entry (max 2 adds);
             target and stop are then measured from the average entry
  move_stop  the first time the stop is hit, do not exit: widen the stop by half instead (once per trade)
  panic      close after a single day of 2% or more against the position while the trade is losing
  give_back  once the trade has been up by half the target, close if it gives back half the target from its peak
Usage: python retail_sim_v2.py sp500.csv [traders=2000] [cost_bps_per_side=2]"""
import sys, numpy as np, pandas as pd

def simulate(close, mom, start, rng, entry, tp, sl, lev=1.0, revenge=False, overconf=False,
             avg_down=False, move_stop=False, panic=False, give_back=False,
             days=252, cost=2e-4, max_hold=120, ruin=0.2, add_step=0.015, max_adds=2, panic_day=0.02):
    eq = 1.0; t = start; end = start+days; mult = 1.0; wins_row = 0; trades = []; reasons = {}
    while t < end-1 and eq > ruin:
        d = (1 if rng.random() < .5 else -1) if entry == 'random' else ((1 if mom[t] > 0 else -1) if entry == 'trend' else 1)
        base = lev*mult; lots = [close[t]]; k = t; stop = sl; peak = 0.0; moved = False
        while True:
            k += 1; rl = [d*(close[k]/p-1) for p in lots]; r = sum(rl)/len(rl)        # move measured from the average entry
            size = base*len(lots); pnl = size*r; day = d*(close[k]/close[k-1]-1); peak = max(peak, r); why = None
            if r >= tp: why = 'target'
            elif give_back and peak >= tp/2 and r <= peak-tp/2: why = 'gave back'
            elif panic and r < 0 and day <= -panic_day: why = 'panic'
            elif stop is not None and r <= -stop:
                if move_stop and not moved: stop *= 1.5; moved = True                  # hope instead of exiting
                else: why = 'stop'
            if why is None:
                if k-t >= max_hold or k >= end-1: why = 'time'
                elif eq*(1+pnl) <= ruin: why = 'margin call'
            if why: break
            if avg_down and len(lots) <= max_adds and rl[0] <= -add_step*len(lots): lots.append(close[k])   # add to the loser
        eq *= max(0.0, 1+pnl-2*cost*size); trades.append(pnl); reasons[why] = reasons.get(why, 0)+1
        if pnl > 0: wins_row += 1; mult = 2.0 if (overconf and wins_row >= 2) else 1.0
        else: wins_row = 0; mult = min(mult*2, 4.0) if revenge else 1.0
        t = k
    return eq, trades, eq <= ruin, reasons

BASES = [('Disciplined +2% / -2%, no leverage', .02, .02, 1, False, False),
         ('Cut winners, hold losers +1% / -4%, no leverage', .01, .04, 1, False, False),
         ('Cut winners, hold losers, 5x leverage', .01, .04, 5, False, False),
         ('All combined (5x, revenge, overconfidence)', .01, .04, 5, True, True)]
ADDS = [('as before', {}), ('+ averaging down', dict(avg_down=True)), ('+ moving the stop', dict(move_stop=True)),
        ('+ panic exit', dict(panic=True)), ('+ giving-back exit', dict(give_back=True)),
        ('+ all four', dict(avg_down=True, move_stop=True, panic=True, give_back=True))]

if __name__ == '__main__':
    N = int(sys.argv[2]) if len(sys.argv) > 2 else 2000; cost = float(sys.argv[3])/1e4 if len(sys.argv) > 3 else 2e-4
    c = pd.read_csv(sys.argv[1])['close'].values; mom = np.r_[np.zeros(20), c[20:]/c[:-20]-1]
    starts = np.random.default_rng(1).integers(60, len(c)-253, N); rows = []
    for entry in ('random', 'trend'):
        for bname, tp, sl, lev, rv, oc in BASES:
            for aname, kw in ADDS:
                res = [simulate(c, mom, s, np.random.default_rng(1000+i), entry, tp, sl, lev, rv, oc, cost=cost, **kw) for i, s in enumerate(starts)]
                e = np.array([x[0] for x in res]); tr = np.concatenate([x[1] for x in res]); rs = {}
                for x in res:
                    for k, v in x[3].items(): rs[k] = rs.get(k, 0)+v
                tot = sum(rs.values())
                rows.append(dict(entry=entry, base=bname, added=aname, lose=round(np.mean(e < 1)*100), median=round((np.median(e)-1)*100),
                    wiped=round(np.mean([x[2] for x in res])*100), winrate=round(np.mean(tr > 0)*100), trades=round(len(tr)/N),
                    exits=' '.join(f'{k}:{round(v/tot*100)}%' for k, v in sorted(rs.items(), key=lambda z: -z[1]))))
    df = pd.DataFrame(rows); df.to_csv('retail_sim_v2_results.csv', index=False)
    pd.set_option('display.width', 250); pd.set_option('display.max_colwidth', 70); print(df.to_string(index=False))
