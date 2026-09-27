---
title: EPUB Translator Worker
emoji: 📚
colorFrom: indigo
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# EPUB Translator Bot

Telegram bot that translates **EPUB · PDF · DOCX · TXT/MD · HTML** files while preserving formatting.

* **Master** (`bot.py`) — runs **only on your Oracle VPS**: Telegram, SQLite, queue, payments, admin panel.
* **Workers** (`app.py`) — stateless translate nodes; deploy the *same file* on
  **Hugging Face · PythonAnywhere · Vercel · Render · Railway · Koyeb · Fly · Docker** — as many as you like.

```
Telegram ──► bot.py (Oracle VPS) ──┬─► HF Space          ──┐
                 │ SQLite, queue,  ├─► PythonAnywhere     ├─► Google Translate
                 │ payments, admin ├─► Vercel / Render    │
                 │                 └─► Railway / Koyeb…  ──┘
                 └─ direct Google fallback if all workers are down
```

> The YAML block at the top of this file is only for Hugging Face Spaces (Docker SDK, port 7860). GitHub just shows it as a small table — ignore it.

---

## 1. Workers — deploy anywhere

All platforms run the same `app.py`. Only the entry point differs:

| Platform | Entry file | Free tier notes |
|---|---|---|
| **Hugging Face Spaces** | `Dockerfile` (+ README front-matter) | always-on, 2 vCPU, best free worker |
| **PythonAnywhere** | `wsgi.py` (a2wsgi adapter) | free tier OK — `*.googleapis.com` is allow-listed, proxy handled automatically |
| **Vercel** | `api/index.py` + `vercel.json` | serverless, 60 s/call, scales to zero |
| **Render** | `render.yaml` | sleeps after 15 min; bot pings it every 8 min |
| **Railway / Koyeb / Heroku-like** | `Procfile` | inject `$PORT` automatically |
| **Fly.io / any Docker host** | `Dockerfile` | `docker run -p 7860:7860` |

Each worker exposes `GET /` (health) and `POST /translate`. Optional `WORKER_SECRET`
(header `X-Worker-Key`) — set the **same value** on every worker and in the bot's `.env`.

### 1a. Hugging Face Spaces (recommended)

1. <https://huggingface.co/new-space> → name it, **SDK: Docker**, hardware *CPU basic (free)*, Public.
2. Push this repo to the Space (or upload `app.py`, `requirements.txt`, `Dockerfile`, `README.md`):
   ```bash
   git remote add hf https://huggingface.co/spaces/<user>/<space>
   git push hf main
   ```
3. *(optional)* Settings → **Variables and secrets** → add secret `WORKER_SECRET`.
4. Worker URL: `https://<user>-<space>.hf.space` → `/addworker` in Telegram.

HF Spaces stay awake (no cold-start) — the bot's keep-alive ping keeps them from
sleeping after 48 h of inactivity as well.

### 1b. PythonAnywhere (free account works)

1. **Bash console:**
   ```bash
   git clone https://github.com/Nitesh99390/Translate-render-vercel-.git worker
   cd worker && pip3 install --user -r requirements.txt
   # optional secret (no env-var UI on PA, wsgi.py reads .env):
   echo 'WORKER_SECRET=same-as-bot' > .env
   ```
2. **Web** tab → *Add a new web app* → **Manual configuration** → Python 3.10 (or newer).
3. Click the **WSGI configuration file** link and replace everything with:
   ```python
   import sys
   sys.path.insert(0, "/home/<username>/worker")
   from wsgi import application
   ```
4. **Reload**. Worker URL: `https://<username>.pythonanywhere.com`

Notes: free accounts go through `proxy.server:3128` — `app.py` honours
`HTTP(S)_PROXY` automatically. `translate.googleapis.com` is on the
[allow-list](https://www.pythonanywhere.com/whitelist/). Free apps need a
"Run until 3 months from today" click every 3 months.

### 1c. Vercel

Import the repo → framework **Other** → Deploy. `vercel.json` rewrites every route
to `api/index.py` (60 s `maxDuration`). Add `WORKER_SECRET` under *Environment Variables* if used.
Worker URL: `https://<project>.vercel.app`

### 1d. Render

New **Web Service** → this repo → Render reads `render.yaml`
(or set start command `uvicorn app:app --host 0.0.0.0 --port $PORT`).
Free instances sleep; the bot wakes them with periodic pings (75 s ping timeout).

### 1e. Railway / Koyeb / Fly / Docker

* **Railway / Koyeb**: connect repo → they use `Procfile` (or the `Dockerfile`). Nothing else to set.
* **Fly.io**: `fly launch --no-deploy` (detects `Dockerfile`) → set `internal_port = 7860` → `fly deploy`.
* **Docker anywhere**:
  ```bash
  docker build -t epub-worker .
  docker run -d -p 7860:7860 -e WORKER_SECRET=xyz --restart unless-stopped epub-worker
  ```

### Verify a worker

```bash
curl https://<worker-url>/
# {"status":"ok","platform":"huggingface","proxy":false,"uptime":12,...}
curl -X POST https://<worker-url>/translate -H 'content-type: application/json' \
     -H 'X-Worker-Key: <secret>' -d '{"text_list":["Hello"],"lang":"hi"}'
# {"success":true,"translated":["नमस्ते"]}
```

---

## 2. Master bot — Oracle VPS only

One-shot installer (Ubuntu / Oracle Linux, run as your normal user):

```bash
git clone https://github.com/Nitesh99390/Translate-render-vercel-.git
cd Translate-render-vercel-
bash oracle_setup.sh          # creates venv, installs deps, writes systemd unit
nano .env                     # fill API_ID, API_HASH, BOT_TOKEN, OWNER_ID (+ WORKER_SECRET)
sudo systemctl restart epub-bot && journalctl -u epub-bot -f
```

Manual steps are the same as before:

```bash
sudo apt install -y python3-venv git
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-bot.txt
cp .env.example .env && nano .env
python bot.py                            # test run
sudo cp epub-bot.service /etc/systemd/system/ && sudo systemctl enable --now epub-bot
```

Then in Telegram: `/admin` → **Workers** → **Add worker** → paste each worker URL
(HF, PythonAnywhere, Vercel, Render…). Or in one go:

```
/addworker https://user-space.hf.space https://user.pythonanywhere.com https://proj.vercel.app
```

The bot load-balances by latency/load, auto-disables dead workers and pings them
every `WORKER_PING_INTERVAL` seconds to keep sleepy free tiers awake.

---

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
| `WORKER_CONCURRENCY` | 8 | parallel requests each worker gets |
| `DIRECT_CONCURRENCY` | 6 | requests the master itself sends to Google in parallel (0 = fallback only) |
| `BATCH_MAX_ITEMS` / `BATCH_MAX_CHARS` | 150 / 9000 | segments / chars per request |

EPUB parsing and re-zipping run in a worker thread (lxml), so the event loop is
never blocked while requests are in flight. Failed big batches are split in half
and retried instead of being dropped. Typical: 14M-char novel ≈ 4–6 min with
6 workers + direct.

## Repo layout

```
app.py              worker (FastAPI) — identical on every platform
api/index.py        Vercel serverless entry (re-exports app)
wsgi.py             PythonAnywhere WSGI entry (a2wsgi)
Dockerfile          Hugging Face / Fly / Koyeb / any Docker host
Procfile            Railway / Koyeb / Heroku-style
render.yaml         Render blueprint
vercel.json         Vercel config
requirements.txt    worker deps
bot.py              master bot — Oracle VPS only
requirements-bot.txt, epub-bot.service, oracle_setup.sh   bot deps / systemd / installer
```
