# EPUB Translator Bot

Telegram bot that translates EPUB books while preserving formatting.
Master runs on an Oracle VM; stateless translation workers run on Render / Vercel.

```
Telegram ──► bot.py (Oracle)  ──► app.py workers (Render / Vercel) ──► Google Translate
                 │  SQLite, queue, payments, admin
                 └─ direct Google fallback if all workers are down
```

## 1. Workers (Render / Vercel)

**Render** – New Web Service → this repo → it picks up `render.yaml` automatically
(or set start command `uvicorn app:app --host 0.0.0.0 --port $PORT`).

**Vercel** – Import repo, framework "Other". `vercel.json` routes everything to `app.py`.

Optionally set `WORKER_SECRET` on each worker (same value as the bot).
Deploy as many as you like; the bot load-balances and auto-disables dead ones.

## 2. Bot (Oracle VM)

```bash
sudo apt install -y python3-venv git
git clone https://github.com/Nitesh99390/Translate-render-vercel-.git
cd Translate-render-vercel-
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-bot.txt
cp .env.example .env && nano .env        # fill API_ID, API_HASH, BOT_TOKEN, OWNER_ID
python bot.py                            # test run
# run forever:
sudo cp epub-bot.service /etc/systemd/system/ && sudo systemctl enable --now epub-bot
journalctl -u epub-bot -f
```

Then in Telegram: `/admin` → **Workers** → **Add worker** → paste the Render/Vercel URL.

## Features

| User | Admin |
|---|---|
| Minimal keyboard: Language · Premium · Status · Help | Inline panel: Workers · Stats · Queue · Broadcast |
| 20 languages, live progress bar + ETA, cancel button | Add / pause / remove workers, health check |
| Free daily limit, premium (Razorpay auto-verify) | `/addpremium /revoke /ban /unban /user /broadcast` |
| Formatting preserved (bold, links, images, TOC, CSS) | SQLite persistence, rotating logs, force-sub |

All settings via environment variables — see `.env.example`.
