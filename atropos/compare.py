"""Compare the eight dashboard strategies on mark-to-market equity.

Every 5m candle the account is valued with open positions at that candle's
close, so the drawdown includes positions held at the same time (what the
dashboard's signal-order drawdown misses). Amounts are 10000 USDT per trade.

Usage
  python3 atropos/compare.py --candles okx_btc_5m.csv
  python3 atropos/compare.py --candles okx_btc_5m.csv --from 2026-08-10 --to 2026-09-17
"""
import argparse

import numpy as np

import grid
from backtest import SIGNAL_FILES, load_candles_csv, load_signals
from dashboard_switch import NOTIONAL

STRATEGIES = {
    "안전형 + 단일진입": ("conservative", "hold"),
    "안전형 + 스위칭 + 단일진입": ("conservative", "switch"),
    "안전형 + 추가진입": ("conservative", "independent"),
    "안전형 + 스위칭 + 추가진입": ("conservative", "pyramid"),
    "공격형 + 단일진입": ("aggressive", "hold"),
    "공격형 + 스위칭 + 단일진입": ("aggressive", "switch"),
    "공격형 + 추가진입": ("aggressive", "independent"),
    "공격형 + 스위칭 + 추가진입": ("aggressive", "pyramid"),
}


def positions(trades, params, policy):
    """[(trade, legs)] as each position actually ends, like switch.run but keeping the legs."""
    done, open_pos = [], []
    for t in trades:
        still = []
        for ptr, plegs in open_pos:
            k = t.i - ptr.i - 1
            if max(b for _, b, _, _ in plegs) > k:
                still.append((ptr, plegs, k))
            else:
                done.append((ptr, plegs))
        open_pos = [(ptr, plegs) for ptr, plegs, _ in still]
        if policy != "independent" and still:
            same = still[0][0].side == t.side
            if policy == "hold" or (policy == "switch" and same):
                continue
            if not same:
                for ptr, plegs, k in still:
                    done.append((ptr, [(q, b, p, r) if b <= k else (q, k, ptr.close[k], grid.TAKER)
                                       for q, b, p, r in plegs]))
                open_pos = []
        open_pos.append((t, grid.legs(t, *params)))
    return done + open_pos


def equity(pos, n_bars):
    """Account value per candle (USDT): realized legs plus open legs at the close."""
    eq = np.zeros(n_bars)
    for tr, legs in pos:
        scale = NOTIONAL / tr.entry / grid.QTY
        start = tr.i + 1
        for q, b, pts, rate in legs:
            if not q:
                continue
            held = min(b, tr.n)
            eq[start:start + held] += (q * tr.close[:held] - tr.entry * q * grid.TAKER) * scale
            eq[start + held:] += grid.leg(tr, q, pts, rate) * scale
    return eq


def max_open(pos):
    events = []
    for tr, legs in pos:
        events += [(tr.i, 1), (tr.i + 1 + max(b for _, b, _, _ in legs), -1)]
    cur = peak = 0
    for _, d in sorted(events):
        cur += d
        peak = max(peak, cur)
    return peak


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", nargs="+", default=SIGNAL_FILES)
    ap.add_argument("--candles", required=True)
    ap.add_argument("--from", dest="since", help="first signal date (KST, YYYY-MM-DD)")
    ap.add_argument("--to", dest="until", help="last signal date (KST, YYYY-MM-DD)")
    ap.add_argument("--max-bars", type=int, default=288 * 7)
    a = ap.parse_args()

    candles = load_candles_csv(a.candles)
    times = sorted(candles)[:-1]
    idx = {t: i for i, t in enumerate(times)}
    sigs = [s for s in load_signals(a.signals, 9) if s["ts"] in idx
            and (not a.since or s["label"][:10] >= a.since)
            and (not a.until or s["label"][:10] <= a.until)]
    trades = [grid.Trade(s, candles, times, idx, a.max_bars) for s in sigs]
    print(f"{len(trades)} signals {trades[0].label} ~ {trades[-1].label}, {NOTIONAL} USDT per trade")
    print(f"{'strategy':<26} {'net':>8} {'MDD':>8} {'net/MDD':>8} {'trades':>6} {'losses':>6} {'max pos':>7}")
    for name, (preset, policy) in STRATEGIES.items():
        pos = positions(trades, grid.PRESETS[preset], policy)
        eq = equity(pos, len(times))[trades[0].i:]
        dd = float(np.max(np.maximum.accumulate(eq) - eq))
        nets = [sum(grid.leg(tr, q, p, r) for q, _, p, r in legs if q) * NOTIONAL / tr.entry / grid.QTY
                for tr, legs in pos]
        ratio = f"{eq[-1] / dd:>8.1f}" if dd else f"{'-':>8}"
        print(f"{name:<26} {eq[-1]:>8.0f} {-dd:>8.0f} {ratio} {len(pos):>6} "
              f"{sum(x < 0 for x in nets):>6} {max_open(pos):>7}")


if __name__ == "__main__":
    main()
