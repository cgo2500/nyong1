"""Rows for the dashboard's '비트 아트로포스' tab: safe preset + switching.

One position at a time with the safe (conservative) exits; a same-side signal
while holding is ignored, an opposite one closes everything at that candle's
close and opens the new side. Rows use the tab's btc_atro schema, with net
scaled to 10000 USDT of BTC per trade.

Usage
  python3 atropos/dashboard_switch.py --candles okx_btc_5m.csv --out-dir DIR
  -> DIR/btc_atro_sw.json, DIR/btc_atro_sw_status.json
"""
import argparse
import json
import os
from datetime import datetime

import grid
import switch
from backtest import SIGNAL_FILES, load_candles_csv, load_signals
from telegram_collector import KST

RULE, PARAMS = "switch", grid.PRESETS["conservative"]
NOTIONAL = 10000
LABEL = "안전형 + 스위칭"
TEXT = ("10000 USDT · 안전형과 같은 손절·익절·트레일 · 포지션 1개 · "
        "반대 신호가 오면 전량 청산 후 반대 진입 · 같은 방향 신호는 무시")


def kst(ms):
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%Y-%m-%dT%H:%M")


def rows_for(trades, times, max_bars):
    out = []
    for tr, net_qty, how in switch.run(trades, PARAMS, "switch")[0]:
        legs = grid.legs(tr, *PARAMS)
        tp1_hit = legs[0][3] == grid.MAKER
        end = max(b for _, b, _, _ in legs)
        if how == "switched":
            k = next(t.i for t in trades if t.i > tr.i and t.side != tr.side) - tr.i - 1
            tp1_hit = tp1_hit and legs[0][1] <= k
            result, closed, exit_bar = ("TP1 + 반대 신호" if tp1_hit else "반대 신호 청산"), True, k
        elif end >= tr.n:
            if tr.n < max_bars:
                result, closed, exit_bar = "보유 중", False, None
            else:
                result, closed, exit_bar = ("TP1 + 7일 만료" if tp1_hit else "7일 만료"), True, tr.n - 1
        else:
            result, closed, exit_bar = ("TP1 + 트레일" if tp1_hit else "손절"), True, end
        net_1btc = net_qty / grid.QTY
        out.append({
            "rule": RULE, "trade_id": f"{RULE}_{tr.label}_{tr.side}",
            "signal_at": tr.label.replace(" ", "T"), "side": tr.side, "entry": tr.entry,
            "result": result, "closed": closed,
            "exit_at": kst(times[tr.i + 1 + exit_bar]) if exit_bar is not None else "",
            "net": round(net_1btc * NOTIONAL / tr.entry, 2), "net_1btc": round(net_1btc, 2),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", nargs="+", default=SIGNAL_FILES)
    ap.add_argument("--candles", required=True)
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--max-bars", type=int, default=288 * 7)
    a = ap.parse_args()

    candles = load_candles_csv(a.candles)
    times = sorted(candles)[:-1]  # last candle may still be forming
    idx = {t: i for i, t in enumerate(times)}
    sigs = [s for s in load_signals(a.signals, 9) if s["ts"] in idx]
    trades = [grid.Trade(s, candles, times, idx, a.max_bars) for s in sigs]
    rows = rows_for(trades, times, a.max_bars)
    status = [{"rule": RULE, "label": LABEL, "text": TEXT, "signals": len(trades),
               "first_signal": trades[0].label.replace(" ", "T") if trades else "",
               "asof": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")}]
    for name, data in (("btc_atro_sw.json", rows), ("btc_atro_sw_status.json", status)):
        with open(os.path.join(a.out_dir, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    print(f"{len(trades)} signals -> {len(rows)} switch trades, "
          f"net {sum(r['net'] for r in rows):.2f} USDT at {NOTIONAL} USDT each")


if __name__ == "__main__":
    main()
