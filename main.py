#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, sys, json, time, random
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram_all_tips_ticket import build_tickets_and_reasoning, post_to_telegram

TIMEZONE = os.getenv("TIMEZONE", "Europe/Belgrade")
TZ = ZoneInfo(TIMEZONE)

def main() -> None:
    date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    tickets, reasonings = build_tickets_and_reasoning(date_str=date_str)

    channels_raw = os.getenv("TELEGRAM_CHANNELS", "")
    channels = [c.strip() for c in channels_raw.replace("\n", ",").split(",") if c.strip()]
    if not channels:
        print("⛔ TELEGRAM_CHANNELS not set.", file=sys.stderr)
        sys.exit(1)

    results = []
    for idx, (ticket_text, reasoning) in enumerate(zip(tickets, reasonings), start=1):
        msg = f"{ticket_text}\n\n🧠 Reasoning:\n{reasoning}".strip()
        for ch in channels:
            ok, resp = post_to_telegram(message=msg, channel=ch)
            results.append({"ticket": idx, "channel": ch, "ok": ok, "resp": resp})
            time.sleep(0.6 + random.random()*0.4)

    print(json.dumps({"sent": results, "tickets": len(tickets)}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
