# EPUB Translator Bot

Telegram bot that translates **EPUB · PDF · DOCX · TXT/MD · HTML** files while preserving formatting.
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
| Formats: EPUB, PDF (layout kept, Noto fonts auto-downloaded), DOCX, TXT/MD, HTML | Output is returned in the same format |

All settings via environment variables — see `.env.example`.

## Supported formats

| Input | How it is translated | What stays intact |
|---|---|---|
| `.epub` | every text node of every chapter + NCX labels | chapters, CSS, images, links, TOC |
| `.pdf` | each text block is redacted and re-typed in the same box with a Unicode Noto font (auto-shrinks to fit) | page layout, images, vector art, links. Scanned/image-only PDFs are rejected (no OCR) |
| `.docx` | `<w:t>` runs of body, headers, footers, foot/endnotes (runs of one paragraph are merged so sentences translate as a whole) | styles, tables, images, numbering |
| `.txt` `.md` | line by line | blank lines, indentation, CRLF |
| `.html` `.htm` `.xhtml` | text nodes + `<title>` | tags, attributes, scripts, CSS |

Fonts for PDF output are fetched from Google Noto on first use and cached in `DATA_DIR/fonts`.

## Performance

The master fans a book out to **all workers and Google directly at the same time**:

| Knob | Default | Meaning |
|---|---|---|
| `MAX_PARALLEL_REQUESTS` | 48 | total in-flight requests per job |
| `WORKER_CONCURRENCY` | 8 | parallel requests each Render/Vercel worker gets |
| `DIRECT_CONCURRENCY` | 6 | requests the master itself sends to Google in parallel (0 = fallback only) |
| `BATCH_MAX_ITEMS` / `BATCH_MAX_CHARS` | 150 / 9000 | segments / chars per request |

EPUB parsing and re-zipping run in a worker thread (lxml), so the event loop is
never blocked while requests are in flight. Failed big batches are split in half
and retried instead of being dropped. Typical: 14M-char novel ≈ 4–6 min with
6 workers + direct.
