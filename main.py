#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, json, time, random
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram_all_tips_ticket import build_tickets_and_reasoning, post_to_telegram

TIMEZONE = os.getenv("TIMEZONE", "Europe/Belgrade")
TZ = ZoneInfo(TIMEZONE)

def main() -> None:
    date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    tickets, reasonings = build_tickets_and_reasoning(date_str=date_str)

    chans_raw = os.getenv("TELEGRAM_CHANNELS", "")
    channels = [c.strip() for c in chans_raw.replace("\n", ",").split(",") if c.strip()]
    if not channels:
        print("⛔ TELEGRAM_CHANNELS not set.", file=sys.stderr)
        sys.exit(1)

    sent = []
    for idx, (t, r) in enumerate(zip(tickets, reasonings), start=1):
        msg = f"{t}\n\n🧠 Reasoning:\n{r}".strip()
        for ch in channels:
            ok, resp = post_to_telegram(msg, ch)
            sent.append({"ticket": idx, "channel": ch, "ok": ok, "resp": resp})
            time.sleep(0.6 + random.random()*0.4)

    print(json.dumps({"sent": sent, "tickets": len(tickets)}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
