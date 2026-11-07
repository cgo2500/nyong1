"""Grid search for split take-profit, stop-loss and trailing stop in percent of entry.

Same rules as grid.py, but every distance is a share of the entry price, so a
rule means the same at 60k and at 90k. Each trade's prices are rescaled so the
entry is 10000: one point is then one basis point (100 = 1%), and grid.py's
simulation runs unchanged. Amounts are 10000 USDT per trade.

Usage
  python3 atropos/pct_grid.py --candles okx_btc_5m.csv [--train 28]
"""
import argparse
import itertools

import numpy as np

import compare
import grid
from backtest import SIGNAL_FILES, load_candles_csv, load_signals

BASE = 10000  # rescaled entry price: 1 point = 1 bp
SLS = range(60, 401, 20)
TP1S = range(60, 401, 20)
TP2S = range(100, 801, 40)
FRACS = (0.2, 0.3, 0.5, 0.7)
ACTS = range(60, 601, 40)
DISTS = range(20, 241, 20)
USDT = 10000 / BASE / grid.QTY  # simulate() result -> USDT at 10000 USDT per trade


def pct_trade(sig, candles, times, idx, max_bars):
    tr = grid.Trade(sig, candles, times, idx, max_bars)
    k = BASE / tr.entry
    tr.price = tr.entry
    for name in ("fav", "adv", "close", "cumfav", "cumadv"):
        setattr(tr, name, getattr(tr, name) * k)
    tr.last *= k
    tr.entry = BASE
    tr.be = BASE * (grid.TAKER + grid.TAKER) / (1 - grid.TAKER)
    return tr


def combos():
    for sl, tp1, frac in itertools.product(SLS, TP1S, FRACS):
        for tp2 in TP2S:
            if tp2 > tp1:
                yield (sl, tp1, frac, "be", tp2, None)
        for act, dist in itertools.product(ACTS, DISTS):
            if act >= tp1 and dist <= act:
                yield (sl, tp1, frac, "trail", act, dist)


def neighbours(p):
    sl, tp1, frac, mode, a, b = p
    da = 40
    steps = [(20, 0, 0, 0), (-20, 0, 0, 0), (0, 20, 0, 0), (0, -20, 0, 0), (0, 0, da, 0), (0, 0, -da, 0)]
    if mode == "trail":
        steps += [(0, 0, 0, 20), (0, 0, 0, -20)]
    for d_sl, d_tp1, d_a, d_b in steps:
        yield (sl + d_sl, tp1 + d_tp1, frac, mode, a + d_a, None if b is None else b + d_b)


def fmt(p):
    sl, tp1, frac, mode, a, b = p
    tail = f"TP2 {a / 100:.2f}% (TP1 후 본절)" if mode == "be" else f"트레일 시작 {a / 100:.2f}% · 간격 {b / 100:.2f}%"
    return f"SL {sl / 100:.2f}% · TP1 {tp1 / 100:.2f}% ({int(frac * 100)}%) · {tail}"


def search(trades, label, top=6):
    results = {p: np.array([grid.simulate(t, *p) for t in trades]) * USDT for p in combos()}
    total = {p: r.sum() for p, r in results.items()}
    robust = {p: np.mean([total[q] for q in neighbours(p) if q in total] + [total[p]]) for p in total}
    print(f"\n== {label}: {len(trades)} trades, {len(total)} combos")
    for name, score in (("best total", total), ("best neighbourhood avg", robust)):
        print(f"-- {name}")
        for p in sorted(score, key=score.get, reverse=True)[:top]:
            r = results[p]
            print(f"{total[p]:>8.0f} {robust[p]:>8.0f} win {(r > 0).mean() * 100:>3.0f}%  {fmt(p)}")
    return total, robust


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", nargs="+", default=SIGNAL_FILES)
    ap.add_argument("--candles", required=True)
    ap.add_argument("--train", type=int, default=28, help="signals used to fit in walk-forward")
    ap.add_argument("--max-bars", type=int, default=288 * 7)
    a = ap.parse_args()

    candles = load_candles_csv(a.candles)
    times = sorted(candles)[:-1]
    idx = {t: i for i, t in enumerate(times)}
    sigs = [s for s in load_signals(a.signals, 9) if s["ts"] in idx]
    trades = [pct_trade(s, candles, times, idx, a.max_bars) for s in sigs]

    total, robust = search(trades, "all signals")
    train, test = trades[:a.train], trades[a.train:]
    t_total, t_robust = search(train, f"train {train[0].label} ~ {train[-1].label}", top=3)
    print(f"\n== walk-forward: test on {len(test)} signals {test[0].label} ~ {test[-1].label}")
    for name, score in (("train best total", t_total), ("train best nbr avg", t_robust),
                        ("all best nbr avg", robust)):
        p = max(score, key=score.get)
        print(f"{name:<20} test {sum(grid.simulate(t, *p) for t in test) * USDT:>8.0f}  {fmt(p)}")

    best = max(robust, key=robust.get)
    print(f"\n== {fmt(best)}: position policies (mark-to-market)")
    for policy in ("hold", "switch", "independent", "pyramid"):
        pos = compare.positions(trades, best, policy)
        eq = compare.equity(pos, len(times))[trades[0].i:]
        dd = float(np.max(np.maximum.accumulate(eq) - eq))
        print(f"{policy:<12} net {eq[-1]:>8.0f}  MDD {-dd:>7.0f}  max pos {compare.max_open(pos)}")


if __name__ == "__main__":
    main()
