"""Collect BTC Atropos signals from the @zetro_ath_bot Telegram chat.

Runs as your own Telegram account (a bot cannot read another bot's messages),
so it needs an api_id / api_hash from https://my.telegram.org. The first run
asks for your phone number and login code and stores a session file; keep
that file and the keys private, they give full access to the account.

Every message from the chat goes to telegram_raw.jsonl (so signals can be
re-parsed later), and recognised BTC Atropos signals go to
telegram_signals.csv in the same format as signals.csv, so
  python3 atropos/backtest.py --signals atropos/telegram_signals.csv
works on them directly.

The candle a signal belongs to is guessed as the 5m candle that closed just
before the message (message 05:50:07 -> candle 05:45). Check it against a
known signal after the first --backfill and adjust --candle-offset.

Usage (on an always-on machine)
  pip install telethon
  export TG_API_ID=... TG_API_HASH=...
  python3 atropos/telegram_collector.py --backfill   # whole history, then exit
  python3 atropos/telegram_collector.py --push       # keep listening, git push each signal
"""
import argparse
import asyncio
import csv
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
KST = timezone(timedelta(hours=9))
RAW = os.path.join(HERE, "telegram_raw.jsonl")
OUT = os.path.join(HERE, "telegram_signals.csv")

LONG = re.compile(r"롱|long|buy|매수", re.I)
SHORT = re.compile(r"숏|short|sell|매도", re.I)


def parse(text):
    """'long' / 'short' for a BTC Atropos signal, else None."""
    if not re.search(r"아트로포스|atropos", text, re.I) or not re.search(r"BTC|비트", text, re.I):
        return None
    is_long, is_short = bool(LONG.search(text)), bool(SHORT.search(text))
    if is_long == is_short:
        return None
    return "long" if is_long else "short"


def candle_time(msg_dt, offset_min):
    t = msg_dt.astimezone(KST).replace(second=0, microsecond=0)
    t -= timedelta(minutes=t.minute % 5)
    return t - timedelta(minutes=offset_min)


def seen_ids():
    if not os.path.exists(RAW):
        return set()
    with open(RAW, encoding="utf-8") as f:
        return {json.loads(line)["id"] for line in f if line.strip()}


def record(msg, offset_min, seen):
    """Append one message; return the signal row if it is one."""
    if msg.id in seen or not msg.message:
        return None
    seen.add(msg.id)
    with open(RAW, "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": msg.id, "date": msg.date.isoformat(), "text": msg.message},
                           ensure_ascii=False) + "\n")
    side = parse(msg.message)
    if not side:
        return None
    row = {"time_kst": candle_time(msg.date, offset_min).strftime("%Y-%m-%d %H:%M"),
           "side": side, "note": "", "msg_id": msg.id,
           "msg_kst": msg.date.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")}
    new = not os.path.exists(OUT)
    with open(OUT, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)
    return row


def git_push(msg):
    repo = os.path.dirname(HERE)
    rel = [os.path.relpath(p, repo) for p in (RAW, OUT) if os.path.exists(p)]
    subprocess.run(["git", "-C", repo, "add", *rel], check=True)
    if subprocess.run(["git", "-C", repo, "diff", "--cached", "--quiet"]).returncode:
        subprocess.run(["git", "-C", repo, "commit", "-qm", msg], check=True)
        subprocess.run(["git", "-C", repo, "push", "-q"], check=False)


async def main():
    from telethon import TelegramClient, events

    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", default=os.environ.get("TG_CHAT", "zetro_ath_bot"))
    ap.add_argument("--session", default=os.path.join(HERE, "atropos_collector"))
    ap.add_argument("--backfill", action="store_true", help="read the whole history and exit")
    ap.add_argument("--candle-offset", type=int, default=5,
                    help="minutes between the floored message time and the signal candle")
    ap.add_argument("--push", action="store_true", help="git commit + push after each signal")
    a = ap.parse_args()

    client = TelegramClient(a.session, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await client.start()
    seen = seen_ids()

    if a.backfill:
        n = 0
        async for msg in client.iter_messages(a.chat, reverse=True):
            if record(msg, a.candle_offset, seen):
                n += 1
        print(f"backfill done: {n} signals -> {OUT}")
        if a.push:
            git_push(f"Backfill {n} Atropos signals from Telegram")
        return

    @client.on(events.NewMessage(chats=a.chat))
    async def on_message(event):
        row = record(event.message, a.candle_offset, seen)
        if row:
            print(f"signal {row['time_kst']} {row['side']} (msg {row['msg_kst']})", flush=True)
            if a.push:
                git_push(f"Atropos signal {row['time_kst']} {row['side']}")

    print(f"listening to {a.chat} ...", flush=True)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
