"""Atropos BTC 5m signal backtest (OKX BTC-USDT-SWAP).

Rules
- Entry: market order at the close of the 5m candle the signal is stamped on.
- Size: 0.1 BTC.
- SL: entry -/+ 1000 points, stop-market (taker fee).
- TP: limit order (maker fee) at the price that only offsets the round-trip
  fees ("be"), or at a fixed number of points (--tp 1000).
- If TP and SL are both inside the same candle, SL is assumed to fill first.

Candles are fetched from the OKX public API, or read from a CSV
(--candles, columns: time,open,high,low,close; time in unix s/ms or ISO UTC).

Usage
  python3 atropos/backtest.py
  python3 atropos/backtest.py --tp 1000
  python3 atropos/backtest.py --candles okx_btc_5m.csv
"""
import argparse
import csv
import json
import math
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BAR_MS = 5 * 60 * 1000
TICK = 0.1


def parse_time(s):
    s = s.strip()
    if s.replace(".", "").isdigit():
        v = float(s)
        return int(v if v > 1e12 else v * 1000)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def load_signals(path, tz_hours):
    tz = timezone(timedelta(hours=tz_hours))
    out = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            dt = datetime.strptime(row["time_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            out.append({"label": row["time_kst"], "ts": int(dt.timestamp() * 1000),
                        "side": row["side"].strip().lower(), "note": row.get("note", "")})
    return sorted(out, key=lambda s: s["ts"])


def load_candles_csv(path):
    candles = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): v for k, v in row.items()}
            ts = parse_time(row["time"])
            candles[ts] = tuple(float(row[k]) for k in ("open", "high", "low", "close"))
    return candles


def fetch_okx(start_ms, end_ms, inst="BTC-USDT-SWAP"):
    """Page backwards through OKX history-candles until start_ms is covered."""
    candles = {}
    after = end_ms + BAR_MS
    while True:
        url = ("https://www.okx.com/api/v5/market/history-candles"
               f"?instId={inst}&bar=5m&limit=100&after={after}")
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)["data"]
        if not data:
            break
        for c in data:
            candles[int(c[0])] = tuple(float(x) for x in c[1:5])
        after = min(int(c[0]) for c in data)
        if after <= start_ms:
            break
        time.sleep(0.12)
    return candles


def be_tp(entry, side, maker, taker):
    """Limit price where gross PnL equals entry (taker) + exit (maker) fees."""
    if side == "long":
        return math.ceil(entry * (1 + taker) / (1 - maker) / TICK) * TICK
    return math.floor(entry * (1 - taker) / (1 + maker) / TICK) * TICK


def run(signals, candles, qty, sl_pts, tp, maker, taker, max_bars):
    times = sorted(candles)
    idx = {t: i for i, t in enumerate(times)}
    results = []
    for s in signals:
        if s["ts"] not in idx:
            results.append({**s, "status": "no candle"})
            continue
        i = idx[s["ts"]]
        entry = candles[s["ts"]][3]
        sgn = 1 if s["side"] == "long" else -1
        sl = entry - sgn * sl_pts
        tp_px = be_tp(entry, s["side"], maker, taker) if tp == "be" else entry + sgn * float(tp)
        status, exit_px, fee_rate, exit_ts = "open", None, taker, None
        mae = 0.0
        for t in times[i + 1:i + 1 + max_bars]:
            o, h, l, c = candles[t]
            adverse = (entry - l) if sgn == 1 else (h - entry)
            mae = max(mae, adverse)
            hit_sl = l <= sl if sgn == 1 else h >= sl
            hit_tp = h >= tp_px if sgn == 1 else l <= tp_px
            if hit_sl:
                # gap through the stop fills at the open, not the stop price
                gap = (o < sl) if sgn == 1 else (o > sl)
                status, exit_px, fee_rate, exit_ts = "SL", (o if gap else sl), taker, t
                break
            if hit_tp:
                status, exit_px, fee_rate, exit_ts = "TP", tp_px, maker, t
                break
        if exit_px is None:
            exit_px = candles[times[min(i + max_bars, len(times) - 1)]][3]
        gross = sgn * (exit_px - entry) * qty
        fees = entry * qty * taker + exit_px * qty * fee_rate
        results.append({**s, "status": status, "entry": entry, "tp": tp_px, "sl": sl,
                        "exit": exit_px, "gross": gross, "fees": fees, "net": gross - fees,
                        "bars": (idx[exit_ts] - i) if exit_ts else None, "mae": mae})
    return results


def report(results):
    print(f"{'signal(KST)':<17} {'side':<5} {'entry':>9} {'TP':>9} {'SL':>9} "
          f"{'result':<6} {'bars':>4} {'MAE':>6} {'net USDT':>9}")
    total = wins = losses = 0
    for r in results:
        if r["status"] == "no candle":
            print(f"{r['label']:<17} {r['side']:<5} {'(no candle data)':>29}")
            continue
        total += r["net"]
        wins += r["status"] == "TP"
        losses += r["status"] == "SL"
        bars = "-" if r["bars"] is None else r["bars"]
        print(f"{r['label']:<17} {r['side']:<5} {r['entry']:>9.1f} {r['tp']:>9.1f} {r['sl']:>9.1f} "
              f"{r['status']:<6} {bars:>4} {r['mae']:>6.0f} {r['net']:>9.2f}")
    done = wins + losses
    print()
    print(f"closed {done} | TP {wins} | SL {losses} | "
          f"win rate {wins / done * 100:.1f}%" if done else "no closed trades")
    print(f"total net PnL: {total:.2f} USDT")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default=os.path.join(HERE, "signals.csv"))
    ap.add_argument("--candles", help="CSV of 5m candles instead of the OKX API")
    ap.add_argument("--save", default=os.path.join(HERE, "okx_btc_5m.csv"))
    ap.add_argument("--tz", type=float, default=9, help="UTC offset of signal times")
    ap.add_argument("--qty", type=float, default=0.1)
    ap.add_argument("--sl", type=float, default=1000)
    ap.add_argument("--tp", default="be", help="'be' (fee break-even) or points")
    ap.add_argument("--maker", type=float, default=0.0002)
    ap.add_argument("--taker", type=float, default=0.0005)
    ap.add_argument("--max-bars", type=int, default=288 * 7, help="give up after N bars")
    a = ap.parse_args()

    signals = load_signals(a.signals, a.tz)
    if a.candles:
        candles = load_candles_csv(a.candles)
    else:
        end = min(signals[-1]["ts"] + a.max_bars * BAR_MS, int(time.time() * 1000))
        candles = fetch_okx(signals[0]["ts"] - BAR_MS, end)
        with open(a.save, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "open", "high", "low", "close"])
            for t in sorted(candles):
                w.writerow([t, *candles[t]])
    report(run(signals, candles, a.qty, a.sl, a.tp, a.maker, a.taker, a.max_bars))


if __name__ == "__main__":
    main()
