"""Rows for the dashboard's '비트 아트로포스' tab: position policies on the presets.

Each rule takes the safe (conservative) or aggressive exits. Except under
hold, an opposite signal closes everything at that candle's close and opens
the new side.
  hold     one position at a time; every signal while holding is ignored
  switch   one position at a time; a same-side signal while holding is ignored
  pyramid  a same-side signal adds another 10000 USDT position
Rows use the tab's btc_atro schema, with net scaled to 10000 USDT of BTC per
trade.

The plain presets (every signal its own trade) go to btc_atro.json, the
dataset the tab started with, and the rest to btc_atro_sw.json. Rules whose
preset is in PCT_PRESETS set their distances in basis points of the entry
(pct_grid.py).

Usage
  python3 atropos/dashboard_switch.py --candles okx_btc_5m.csv --out-dir DIR
  -> DIR/btc_atro.json, DIR/btc_atro_status.json,
     DIR/btc_atro_sw.json, DIR/btc_atro_sw_status.json
"""
import argparse
import json
import os
from datetime import datetime

import grid
import switch
from backtest import SIGNAL_FILES, load_candles_csv, load_signals
from telegram_collector import KST

NOTIONAL = 10000
HOLD_TEXT = "포지션 1개 · 들고 있는 동안 오는 신호는 반대 방향까지 모두 무시"
SWITCH_TEXT = "포지션 1개 · 반대 신호가 오면 전량 청산 후 반대 진입 · 같은 방향 신호는 무시"
PYRAMID_TEXT = "같은 방향 신호마다 10000 USDT 추가 진입 · 반대 신호가 오면 전량 청산 후 반대 진입"
# percent rules from pct_grid.py, in basis points of the entry (260 = 2.6%)
PCT_PRESETS = {
    "pct_mid": (260, 320, 0.2, "trail", 420, 80),
    "pct_aggr": (360, 320, 0.2, "trail", 420, 80),
}
PCT_TEXT = "+3.2% 에서 20% 익절 · 나머지 80% 는 +4.2% 부터 최고가 −0.8% 트레일"
# rule id: (preset, policy, label, text); the page names safe/aggr itself
BASE_RULES = {
    "safe": ("conservative", "independent", "안전형",
             "10000 USDT · 손절 −1,200 · +2,200 에서 30% 익절 · 나머지 70% 최고가 −400 트레일"),
    "aggr": ("aggressive", "independent", "공격형",
             "10000 USDT · 손절 −2,400 · +2,600 에서 70% 익절 · 나머지 30% 최고가 −400 트레일"),
}
RULES = {
    "hold": ("conservative", "hold", "안전형 + 단일진입",
             f"10000 USDT · 안전형과 같은 손절·익절·트레일 · {HOLD_TEXT}"),
    "aggr_hold": ("aggressive", "hold", "공격형 + 단일진입",
                  f"10000 USDT · 공격형과 같은 손절·익절·트레일 · {HOLD_TEXT}"),
    "switch": ("conservative", "switch", "안전형 + 스위칭 + 단일진입",
               f"10000 USDT · 안전형과 같은 손절·익절·트레일 · {SWITCH_TEXT}"),
    "pyramid": ("conservative", "pyramid", "안전형 + 스위칭 + 추가진입",
                f"10000 USDT · 안전형과 같은 손절·익절·트레일 · {PYRAMID_TEXT}"),
    "aggr_switch": ("aggressive", "switch", "공격형 + 스위칭 + 단일진입",
                    f"10000 USDT · 공격형과 같은 손절·익절·트레일 · {SWITCH_TEXT}"),
    "aggr_pyramid": ("aggressive", "pyramid", "공격형 + 스위칭 + 추가진입",
                     f"10000 USDT · 공격형과 같은 손절·익절·트레일 · {PYRAMID_TEXT}"),
    "pct_mid_pyramid": ("pct_mid", "pyramid", "% 균형형 + 스위칭 + 추가진입",
                        f"10000 USDT · 손절 −2.6% · {PCT_TEXT} · {PYRAMID_TEXT}"),
    "pct_aggr_pyramid": ("pct_aggr", "pyramid", "% 공격형 + 스위칭 + 추가진입",
                         f"10000 USDT · 손절 −3.6% · {PCT_TEXT} · {PYRAMID_TEXT}"),
}


def kst(ms):
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%Y-%m-%dT%H:%M")


def rows_for(rule, params, policy, trades, times, max_bars):
    out = []
    for tr, net_qty, how in switch.run(trades, params, policy)[0]:
        legs = grid.legs(tr, *params)
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
        price = getattr(tr, "price", tr.entry)  # percent trades are rescaled to entry 10000
        net = net_qty / grid.QTY * NOTIONAL / tr.entry
        out.append({
            "rule": rule, "trade_id": f"{rule}_{tr.label}_{tr.side}",
            "signal_at": tr.label.replace(" ", "T"), "side": tr.side, "entry": price,
            "result": result, "closed": closed,
            "exit_at": kst(times[tr.i + 1 + exit_bar]) if exit_bar is not None else "",
            "net": round(net, 2), "net_1btc": round(net * price / NOTIONAL, 2),
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
    from pct_grid import pct_trade  # pct_grid -> compare -> this module
    pct_trades = [pct_trade(s, candles, times, idx, a.max_bars) for s in sigs]
    asof = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    first = trades[0].label.replace(" ", "T") if trades else ""
    for (rows_name, status_name), rules in ((("btc_atro.json", "btc_atro_status.json"), BASE_RULES),
                                            (("btc_atro_sw.json", "btc_atro_sw_status.json"), RULES)):
        rows, status = [], []
        for rule, (preset, policy, label, text) in rules.items():
            pct = preset in PCT_PRESETS
            params = PCT_PRESETS[preset] if pct else grid.PRESETS[preset]
            ts = pct_trades if pct else trades
            rs = rows_for(rule, params, policy, ts, times, a.max_bars)
            rows += rs
            st = {"rule": rule, "label": label, "text": text, "signals": len(trades),
                  "first_signal": first, "asof": asof}
            if policy != "independent":
                st["max_open"] = switch.run(ts, params, policy)[1]
            status.append(st)
            print(f"{rule}: {len(trades)} signals -> {len(rs)} trades, "
                  f"net {sum(r['net'] for r in rs):.2f} USDT at {NOTIONAL} USDT each")
        for name, data in ((rows_name, rows), (status_name, status)):
            with open(os.path.join(a.out_dir, name), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
