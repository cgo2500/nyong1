"""Grid search for split take-profit, stop-loss and trailing stop on Atropos signals.

Each trade: market entry at the signal candle close (taker), fraction F closed
by a limit at TP1 (maker), the rest managed by one of
  fixed  - limit at TP2 (maker)
  be     - limit at TP2, stop moved to the fee break-even after TP1
  trail  - trailing stop (taker) armed once price is ACT points in favor,
           trailing DIST points behind the best price since then
Stops are stop-market (taker). Conservative fills: when a stop and a target
are both inside one candle the stop wins, and a trailing stop is checked
against a candle before that candle's high/low can move it.

Overfitting guard: parameters are also scored on the average of their grid
neighbours, and re-fit on the earlier signals then tested on the later ones.

Usage
  python3 atropos/grid.py --candles okx_btc_5m.csv
"""
import argparse
import itertools
import os

import numpy as np

from backtest import BAR_MS, HERE, SIGNAL_FILES, load_candles_csv, load_signals

QTY = 0.1
MAKER, TAKER = 0.0002, 0.0005

SLS = range(600, 3601, 200)
TP1S = range(400, 3601, 200)
TP2S = range(800, 6001, 400)
FRACS = (0.2, 0.3, 0.5, 0.7)
ACTS = range(400, 4001, 200)
DISTS = range(400, 2401, 200)
BE_ATS = (None, *range(300, 2501, 100))
PRESETS = {
    "SL/TP 1000": (1000, 1000, 1.0, "fixed", 1200, None),
    "conservative": (1200, 2200, 0.3, "trail", 2200, 400),
    "aggressive": (2400, 2600, 0.7, "trail", 2600, 400),
}


class Trade:
    """Price path after entry, in points: fav = best move for us, adv = worst."""

    def __init__(self, sig, candles, times, idx, max_bars):
        i = idx[sig["ts"]]
        self.label, self.side = sig["label"], sig["side"]
        self.entry = candles[sig["ts"]][3]
        sgn = 1 if self.side == "long" else -1
        path = [candles[t] for t in times[i + 1:i + 1 + max_bars]]
        o, h, l, c = (np.array(x) for x in zip(*path))
        self.fav = (h - self.entry) if sgn == 1 else (self.entry - l)
        self.adv = (self.entry - l) if sgn == 1 else (h - self.entry)
        self.last = sgn * (c[-1] - self.entry)
        self.n = len(path)
        self.cumfav = np.maximum.accumulate(self.fav)
        self.cumadv = np.maximum.accumulate(self.adv)
        # fee break-even distance for a taker entry and a stop-market (taker) exit
        self.be = self.entry * (TAKER + TAKER) / (1 - TAKER)
        self._be_hit = {}
        self._trail = {}

    def first_fav(self, pts):
        return int(np.searchsorted(self.cumfav, pts))  # n when never reached

    def first_adv(self, pts):
        return int(np.searchsorted(self.cumadv, pts))

    def be_hit(self, t_tp1):
        """First candle after TP1 that trades back through the break-even stop."""
        if t_tp1 not in self._be_hit:
            hit = np.nonzero(self.adv[t_tp1 + 1:] >= -self.be)[0]
            self._be_hit[t_tp1] = t_tp1 + 1 + int(hit[0]) if len(hit) else self.n
        return self._be_hit[t_tp1]

    def trail(self, act, dist):
        """(exit bar, exit points) of a trailing stop armed at `act` points."""
        key = (act, dist)
        if key not in self._trail:
            t = self.first_fav(act)
            res = (self.n, self.last)
            if t < self.n:
                peak = act
                for k in range(t + 1, self.n):
                    stop = peak - dist
                    if -self.adv[k] <= stop:
                        res = (k, stop)
                        break
                    peak = max(peak, self.fav[k])
            self._trail[key] = res
        return self._trail[key]


def leg(tr, qty, exit_pts, rate):
    """Net PnL of one leg; exit_pts is signed points from entry (side-adjusted)."""
    sgn = 1 if tr.side == "long" else -1
    exit_px = tr.entry + sgn * exit_pts
    return exit_pts * qty - exit_px * qty * rate - tr.entry * qty * TAKER


def simulate(tr, sl, tp1, frac, mode, p2, p3=None, be_at=None):
    """Net PnL of one trade. mode fixed/be: p2 = TP2; mode trail: p2 = act, p3 = dist.

    be_at: once price is this many points in favor, the stop for the whole
    position moves to the fee break-even.
    """
    q1, q2 = QTY * frac, QTY * (1 - frac)
    t_stop, stop_pts = tr.first_adv(sl), -sl
    if be_at is not None:
        t_arm = tr.first_fav(be_at)
        if t_arm < t_stop:
            t_stop, stop_pts = tr.be_hit(t_arm), tr.be
    t_tp1 = tr.first_fav(tp1)

    if t_tp1 < t_stop:
        pnl = leg(tr, q1, tp1, MAKER)
    elif t_stop < tr.n:
        pnl = leg(tr, q1, stop_pts, TAKER)
    else:
        pnl = leg(tr, q1, tr.last, TAKER)

    if mode == "be" and t_tp1 < t_stop:
        t_be = tr.be_hit(t_tp1)
        if t_be < t_stop or stop_pts < tr.be:
            t_stop, stop_pts = min(t_be, t_stop), tr.be
    if mode == "trail":
        if tr.first_fav(p2) < t_stop:
            _, pts = tr.trail(p2, p3)
            return pnl + leg(tr, q2, max(pts, stop_pts), TAKER)
    elif tr.first_fav(p2) < t_stop:
        return pnl + leg(tr, q2, p2, MAKER)
    if t_stop < tr.n:
        return pnl + leg(tr, q2, stop_pts, TAKER)
    return pnl + leg(tr, q2, tr.last, TAKER)


def combos():
    for sl, tp1, frac in itertools.product(SLS, TP1S, FRACS):
        for tp2 in TP2S:
            if tp2 > tp1:
                yield (sl, tp1, frac, "fixed", tp2, None)
                yield (sl, tp1, frac, "be", tp2, None)
        for act, dist in itertools.product(ACTS, DISTS):
            if act >= tp1 and dist <= act:
                yield (sl, tp1, frac, "trail", act, dist)


def evaluate(trades, params):
    return np.array([simulate(tr, *params) for tr in trades])


def max_dd(pnls):
    eq = np.cumsum(pnls)
    return float(np.max(np.maximum.accumulate(np.concatenate([[0], eq]))[1:] - eq))


def neighbours(p):
    sl, tp1, frac, mode, a, b = p
    da = 400 if mode != "trail" else 200
    steps = [(200, 0, 0, 0), (-200, 0, 0, 0), (0, 200, 0, 0), (0, -200, 0, 0),
             (0, 0, da, 0), (0, 0, -da, 0)]
    if mode == "trail":
        steps += [(0, 0, 0, 200), (0, 0, 0, -200)]
    for d_sl, d_tp1, d_a, d_b in steps:
        yield (sl + d_sl, tp1 + d_tp1, frac, mode, a + d_a, None if b is None else b + d_b)


def search(trades, label, top=8):
    results = {p: evaluate(trades, p) for p in combos()}
    total = {p: r.sum() for p, r in results.items()}
    robust = {p: np.mean([total[q] for q in neighbours(p) if q in total] + [total[p]])
              for p in total}
    print(f"\n== {label}: {len(trades)} trades, {len(total)} combos")
    for name, score in (("best total", total), ("best neighbourhood avg", robust)):
        print(f"-- {name}")
        print(f"{'SL':>5} {'TP1':>5} {'F':>4} {'mode':<6} {'TP2/ACT':>7} {'DIST':>5} "
              f"{'total':>9} {'nbr avg':>8} {'win%':>5} {'maxDD':>7}")
        for p in sorted(score, key=score.get, reverse=True)[:top]:
            r = results[p]
            print(f"{p[0]:>5} {p[1]:>5} {p[2]:>4} {p[3]:<6} {p[4]:>7} {p[5] or '-':>5} "
                  f"{total[p]:>9.1f} {robust[p]:>8.1f} {(r > 0).mean() * 100:>5.0f} {max_dd(r):>7.1f}")
    print("-- best neighbourhood avg per exit mode / TP1 share")
    for mode, frac in itertools.product(("fixed", "be", "trail"), FRACS):
        p = max((q for q in robust if q[3] == mode and q[2] == frac), key=robust.get)
        r = results[p]
        print(f"{p[0]:>5} {p[1]:>5} {p[2]:>4} {p[3]:<6} {p[4]:>7} {p[5] or '-':>5} "
              f"{total[p]:>9.1f} {robust[p]:>8.1f} {(r > 0).mean() * 100:>5.0f} {max_dd(r):>7.1f}")
    return results, total, robust


def trail_table(total, sl, tp1, frac):
    """Total PnL by trailing activation (rows) and distance (columns)."""
    print(f"\n== trailing stop table (SL {sl}, TP1 {tp1}, TP1 share {frac})")
    print("ACT\\DIST" + "".join(f"{d:>7}" for d in DISTS))
    for act in ACTS:
        row = [total.get((sl, tp1, frac, "trail", act, d)) for d in DISTS]
        if any(v is not None for v in row):
            print(f"{act:>8}" + "".join(f"{v:>7.0f}" if v is not None else f"{'':>7}" for v in row))


def be_table(trades, configs):
    """Total PnL when the stop moves to break-even once price is +X in favor."""
    print("\n== break-even stop after +X points (total net / losing trades)")
    print(f"{'+X':>6}" + "".join(f"{name:>22}" for name in configs))
    for x in BE_ATS:
        cells = []
        for p in configs.values():
            r = np.array([simulate(tr, *p, be_at=x) for tr in trades])
            cells.append(f"{r.sum():>14.1f} / {int((r < 0).sum()):>3}")
        print(f"{x or 'off':>6}" + "".join(f"{c:>22}" for c in cells))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", nargs="+", default=SIGNAL_FILES)
    ap.add_argument("--candles", required=True)
    ap.add_argument("--tz", type=float, default=9)
    ap.add_argument("--max-bars", type=int, default=288 * 7)
    ap.add_argument("--train", type=int, default=16, help="signals used to fit in walk-forward")
    a = ap.parse_args()

    candles = load_candles_csv(a.candles)
    times = sorted(candles)[:-1]  # last candle may still be forming
    idx = {t: i for i, t in enumerate(times)}
    sigs = [s for s in load_signals(a.signals, a.tz) if s["ts"] in idx]
    trades = [Trade(s, candles, times, idx, a.max_bars) for s in sigs]

    print("signal(KST)       side   MFE  MAE  MAE-before-MFE>=1000  bars-to-+1000")
    for tr in trades:
        t1000 = tr.first_fav(1000)
        mae_pre = tr.cumadv[t1000 - 1] if t1000 else 0
        print(f"{tr.label:<17} {tr.side:<5} {tr.cumfav[-1]:>5.0f} {tr.cumadv[-1]:>5.0f} "
              f"{mae_pre:>10.0f} {t1000 if t1000 < tr.n else '-':>16}")

    _, total, robust = search(trades, "all signals")
    best_trail = max((q for q in robust if q[3] == "trail"), key=robust.get)
    trail_table(total, *best_trail[:3])
    be_table(trades, {**PRESETS, "grid best": max(robust, key=robust.get)})

    train, test = trades[:a.train], trades[a.train:]
    _, t_total, t_robust = search(train, f"train (first {a.train})", top=3)
    print(f"\n== walk-forward: fit on first {a.train}, test on last {len(test)}")
    for name, score in (("best total", t_total), ("best neighbourhood avg", t_robust)):
        p = max(score, key=score.get)
        print(f"{name:<24} {p} -> test net {evaluate(test, p).sum():.1f} USDT")
    base = (1000, 1000, 1.0, "fixed", 1200, None)
    print(f"{'baseline SL/TP 1000':<24} {base} -> test net {evaluate(test, base).sum():.1f} USDT")


if __name__ == "__main__":
    main()
