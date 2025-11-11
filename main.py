#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, json, time, random, traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram_all_tips_ticket import build_tickets_and_reasoning, post_to_telegram

TIMEZONE = os.getenv("TIMEZONE", "Europe/Belgrade")
TZ = ZoneInfo(TIMEZONE)

def debug(msg: str):
    print(f"[DEBUG] {datetime.now(TZ).strftime('%H:%M:%S')} | {msg}", flush=True)

def main() -> None:
    debug("=== START main.py run ===")
    try:
        date_str = datetime.now(TZ).strftime("%Y-%m-%d")
        debug(f"Using date: {date_str}")

        api = os.getenv("API_FOOTBALL_KEY")
        tele = os.getenv("TELEGRAM_BOT_TOKEN")
        chans_raw = os.getenv("TELEGRAM_CHANNELS", "")
        chans = [c.strip() for c in chans_raw.replace("\n", ",").split(",") if c.strip()]
        openai_key = os.getenv("OPENAI_API_KEY")

        debug(f"API_FOOTBALL_KEY present: {bool(api)}")
        debug(f"OPENAI_API_KEY present: {bool(openai_key)}")
        debug(f"TELEGRAM_BOT_TOKEN present: {bool(tele)}")
        debug(f"Channels: {chans}")

        tickets, reasonings = build_tickets_and_reasoning(date_str=date_str, debug=True)
        debug(f"Tickets built: {len(tickets)}")

        results = []
        if not tickets:
            debug("⚠️ No tickets were generated.")
        else:
            for idx, (ticket_text, reasoning) in enumerate(zip(tickets, reasonings), start=1):
                msg = f"{ticket_text}\n\n🧠 Reasoning:\n{reasoning}".strip()
                debug(f"Sending Ticket #{idx} ({len(msg)} chars)")
                for ch in chans:
                    ok, resp = post_to_telegram(message=msg, channel=ch)
                    debug(f"→ Channel {ch}: ok={ok}, resp={str(resp)[:120]}")
                    results.append({"ticket": idx, "channel": ch, "ok": ok, "resp": resp})
                    time.sleep(0.6 + random.random() * 0.4)

        payload = {"sent": results, "tickets": len(tickets)}
        debug(f"Finished. Payload summary:\n{json.dumps(payload, ensure_ascii=False, indent=2)}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    except Exception as e:
        debug(f"❌ Exception: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
