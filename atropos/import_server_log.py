"""Turn the server's Atropos log into BTC signal files for the backtest.

monitor.py on the bot server appends every Atropos alert (Telegram and Zetro
webhook alike) to /opt/pantera_gate_bot/atropos_signals.jsonl, one JSON object
per line with msg_time, text, source and pairs (or parsed.pairs) such as
[["BTC", "long"], ...]. This keeps only BTC, merges the Telegram/webhook copies
of one signal, and writes
  server_signals.csv     time_kst,side,note,msg_kst,sources,price  (backtest format)
  server_btc_raw.jsonl   the BTC log lines as they were, for re-parsing later

Usage
  python3 atropos/import_server_log.py --src /opt/pantera_gate_bot/atropos_signals.jsonl
"""
import argparse
import csv
import json
import os
import re
from datetime import datetime

from telegram_collector import HERE, KST, candle_time

SIDES = {"long": "long", "롱": "long", "buy": "long", "short": "short", "숏": "short", "sell": "short"}


def btc_side(row):
    pairs = (row.get("parsed") or {}).get("pairs") or row.get("pairs") or []
    sides = {SIDES.get(str(s).strip().lower()) for coin, s in pairs if str(coin).upper() == "BTC"}
    sides.discard(None)
    return sides.pop() if len(sides) == 1 else None


def signal_price(text):
    """Price Zetro quotes in the alert, e.g. '아트로포스 롱 · BTC 5 · 78947.6'."""
    m = re.search(r"BTC\s*\d+\s*·\s*([\d.,]+)", text or "")
    return m.group(1).replace(",", "") if m else ""


def msg_dt(row, naive_tz):
    dt = datetime.fromisoformat(str(row["msg_time"]).replace("Z", "+00:00"))
    return dt.replace(tzinfo=naive_tz) if dt.tzinfo is None else dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/opt/pantera_gate_bot/atropos_signals.jsonl")
    ap.add_argument("--out", default=os.path.join(HERE, "server_signals.csv"))
    ap.add_argument("--raw-out", default=os.path.join(HERE, "server_btc_raw.jsonl"))
    ap.add_argument("--candle-offset", type=int, default=5,
                    help="minutes between the floored message time and the signal candle")
    ap.add_argument("--merge-min", type=int, default=10,
                    help="same-side messages this close together are one signal")
    a = ap.parse_args()

    raw, sigs = [], []
    with open(a.src, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            side = btc_side(row)
            if not side:
                continue
            raw.append(line.rstrip("\n"))
            sigs.append((msg_dt(row, KST), side, row.get("source", "telegram"),
                         signal_price(row.get("text"))))
    sigs.sort(key=lambda s: s[0])

    merged = []
    for dt, side, src, price in sigs:
        last = merged[-1] if merged else None
        if last and last["side"] == side and (dt - last["dt"]).total_seconds() <= a.merge_min * 60:
            last["sources"].add(src)
            last["price"] = last["price"] or price
            continue
        merged.append({"dt": dt, "side": side, "sources": {src}, "price": price})

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time_kst", "side", "note", "msg_kst", "sources", "price"])
        for m in merged:
            w.writerow([candle_time(m["dt"], a.candle_offset).strftime("%Y-%m-%d %H:%M"), m["side"], "",
                        m["dt"].astimezone(KST).strftime("%Y-%m-%d %H:%M:%S"), "+".join(sorted(m["sources"])), m["price"]])
    with open(a.raw_out, "w", encoding="utf-8") as f:
        f.write("\n".join(raw) + ("\n" if raw else ""))
    print(f"{len(raw)} BTC log lines -> {len(merged)} signals -> {a.out}")


if __name__ == "__main__":
    main()
