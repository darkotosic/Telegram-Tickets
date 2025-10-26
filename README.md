# Telegram Tickets

Builds three football betting tickets from API-Football odds and posts them to Telegram channels. No Airtable. OpenAI provides short reasoning per ticket.

## Secrets expected
- `API_FOOTBALL_KEY`
- `OPENAI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHANNELS` — comma or newline separated list of channel usernames (e.g. `@ch1, @ch2`).

Optional:
- `OPENAI_MODEL` (default: `gpt-4.1-mini`)
- `TIMEZONE` (default: `Europe/Belgrade`)

## Run locally
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export API_FOOTBALL_KEY=... OPENAI_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHANNELS='@ch1,@ch2'
python main.py
```
