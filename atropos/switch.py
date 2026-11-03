"""Backtest Atropos presets with one position at a time.

Policies
  independent - every signal is its own trade (what grid.py assumes)
  hold        - one position at a time; signals while holding are ignored
  switch      - one position at a time; an opposite signal while holding
                closes everything at that candle's close (taker) and opens
                the new side; a same-side signal is ignored

Usage
  python3 atropos/switch.py --candles okx_btc_5m.csv [--qty 1]
"""
import argparse

import numpy as np

import grid
from backtest import SIGNAL_FILES, load_candles_csv, load_signals


def run(trades, params, policy):
    """Returns per-trade rows: (trade, net, how it ended)."""
    if policy == "independent":
        return [(t, grid.simulate(t, *params), "") for t in trades]
    rows, pos = [], None
    for t in trades:
        if pos is not None:
            ptr, plegs = pos
            k = t.i - ptr.i - 1  # bar of the new signal, relative to the open trade
            if max(b for _, b, _, _ in plegs) > k:  # still holding at this close
                if policy == "hold" or t.side == ptr.side:
                    continue
                cut = [(q, b, p, r) if b <= k else (q, k, ptr.close[k], grid.TAKER)
                       for q, b, p, r in plegs]
                rows.append((ptr, net(ptr, cut), "switched"))
                pos = None
            else:
                rows.append((ptr, net(ptr, plegs), ""))
                pos = None
        pos = (t, grid.legs(t, *params))
    if pos is not None:
        rows.append((pos[0], net(*pos), ""))
    return rows


def net(tr, legs):
    return sum(grid.leg(tr, q, p, r) for q, _, p, r in legs if q)


def max_dd(pnls):
    eq = np.cumsum([0, *pnls])
    return float(np.max(np.maximum.accumulate(eq) - eq))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", nargs="+", default=SIGNAL_FILES)
    ap.add_argument("--candles", required=True)
    ap.add_argument("--qty", type=float, default=1.0, help="BTC per signal for the report")
    ap.add_argument("--max-bars", type=int, default=288 * 7)
    a = ap.parse_args()

    candles = load_candles_csv(a.candles)
    times = sorted(candles)[:-1]
    idx = {t: i for i, t in enumerate(times)}
    trades = [grid.Trade(s, candles, times, idx, a.max_bars)
              for s in load_signals(a.signals, 9) if s["ts"] in idx]
    scale = a.qty / grid.QTY

    print(f"{len(trades)} signals, {a.qty} BTC per trade (USDT)")
    print(f"{'preset':<14} {'policy':<12} {'trades':>6} {'switch':>6} {'losses':>6} "
          f"{'total':>10} {'maxDD':>9}")
    detail = {}
    for name, params in grid.PRESETS.items():
        for policy in ("independent", "hold", "switch"):
            rows = run(trades, params, policy)
            pnls = [r[1] * scale for r in rows]
            print(f"{name:<14} {policy:<12} {len(rows):>6} "
                  f"{sum(r[2] == 'switched' for r in rows):>6} {sum(p < 0 for p in pnls):>6} "
                  f"{sum(pnls):>10.0f} {max_dd(pnls):>9.0f}")
            detail[name, policy] = rows

    for name in ("conservative", "aggressive"):
        print(f"\n== {name}: per signal, independent vs switch")
        sw = {r[0].label: r for r in detail[name, "switch"]}
        for t, pnl, _ in detail[name, "independent"]:
            s = sw.get(t.label)
            right = f"{s[1] * scale:>9.0f} {s[2]}" if s else f"{'skipped':>9}"
            print(f"{t.label} {t.side:<5} {pnl * scale:>9.0f} | {right}")


if __name__ == "__main__":
    main()
