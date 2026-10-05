---
title: EPUB Translator Worker
emoji: 📚
colorFrom: indigo
colorTo: green
sdk: gradio
sdk_version: 6.28.0
python_version: "3.12"
app_file: hf_app.py
pinned: false
license: mit
---

# EPUB Translator Bot

Telegram bot that translates **EPUB · PDF · DOCX · TXT/MD · HTML** files while preserving formatting.

* **Master** (`bot.py`) — runs **only on your Oracle VPS**: Telegram, SQLite, queue, payments, admin panel.
* **Workers** — stateless translate nodes, all speaking the same tiny HTTP API.
  Deploy on **Hugging Face (free ZeroGPU) · Cloudflare Workers · Render · Vercel · PythonAnywhere · Deno Deploy · Railway · Koyeb · Fly · Docker** — as many as you like, **no credit card on any of them**.

```
Telegram ──► bot.py (Oracle VPS) ──┬─► HF Space (Gradio/ZeroGPU) ──┐
                 │ SQLite, queue,  ├─► Cloudflare Worker (JS)      │
                 │ payments, admin ├─► Render / Vercel             ├─► Google Translate
                 │                 ├─► PythonAnywhere / Deno       │
                 │                 └─► Railway / Koyeb / Docker  ──┘
                 └─ direct Google fallback if all workers are down
```

> The YAML block at the top of this file is only for Hugging Face Spaces (Gradio SDK → `hf_app.py`). GitHub just shows it as a small table — ignore it.

### ⚠️ Hugging Face changed its free tier (mid-2026)

* **Docker Spaces** now require a billing method, **Gradio on CPU Basic** needs PRO.
* The only free route is a **Gradio Space on ZeroGPU** — allowed for free accounts that are
  **>30 days old with a verified e-mail** (max 2 Spaces). `hf_app.py` handles the ZeroGPU
  quirks (mandatory `@spaces.GPU` probe + startup report) while still serving the plain
  FastAPI `/translate` endpoint on CPU — **no GPU quota is used**.
* Everything else here (Cloudflare, Render, Vercel, PythonAnywhere, Deno) is independent of
  HF, so you're never blocked by one provider.

---

## 1. Workers — deploy anywhere

All platforms run the same `app.py`. Only the entry point differs:

| Platform | Entry file | Card? | Free tier notes |
|---|---|---|---|
| **Hugging Face Spaces** | `hf_app.py` (Gradio SDK, ZeroGPU) | ❌ | always-on; account must be 30+ days old, e-mail verified; max 2 Spaces |
| **Cloudflare Workers** | `cloudflare/worker.js` + `wrangler.toml` | ❌ | 100k req/day, global edge, never sleeps — **fastest & most reliable free worker** |
| **Render** | `render.yaml` | ❌ | 750 h/month, sleeps after 15 min; bot pings it every 8 min |
| **Vercel** | `api/index.py` + `vercel.json` | ❌ | serverless, 60 s/call, scales to zero |
| **PythonAnywhere** | `wsgi.py` (a2wsgi adapter) | ❌ | `*.googleapis.com` allow-listed, proxy handled; renew every 3 months |
| **Deno Deploy** | `deno_worker.ts` (re-uses CF worker) | ❌ | 1M req/month, edge, never sleeps |
| **Railway / Koyeb / Heroku-like** | `Procfile` | varies | inject `$PORT` automatically |
| **Fly.io / any Docker host / VPS** | `Dockerfile` | varies | `docker run -p 7860:7860` |

Each worker exposes `GET /` (health) and `POST /translate`. Optional `WORKER_SECRET`
(header `X-Worker-Key`) — set the **same value** on every worker and in the bot's `.env`.

### 1a. Hugging Face Spaces — free **Gradio + ZeroGPU** (no card)

1. <https://huggingface.co/new-space> → name it → **SDK: Gradio** → template **Blank** →
   hardware **ZeroGPU (Free)** → Public → *Create Space*.
   (Docker is greyed out / "Paid" — that's expected now; don't pick it.)
2. Push this repo to the Space (or upload `app.py`, `hf_app.py`, `requirements.txt`, `README.md`):
   ```bash
   git remote add hf https://huggingface.co/spaces/<user>/<space>
   git push hf main --force
   ```
   The README front-matter already says `sdk: gradio` / `app_file: hf_app.py`.
3. *(optional)* Settings → **Variables and secrets** → add secret `WORKER_SECRET`.
4. Worker URL: `https://<user>-<space>.hf.space` → `/addworker` in Telegram.
   Open the URL in a browser: you are redirected to a small Gradio test page at
   `/ui`; `/` (JSON for non-browser clients), `/health` and `/translate` are the
   API the bot uses.

Notes
* ZeroGPU Spaces need at least one `@spaces.GPU` function to exist and a startup
  report — `hf_app.py` does both. The probe is never called, so **0 s of GPU quota** is used.
* If you get *"you can't host ZeroGPU Spaces yet"* your account is younger than 30 days /
  e-mail not verified. Use Cloudflare or Render meanwhile.
* If the Space shows `No @spaces.GPU function detected` → Settings → **Factory rebuild**.

### 1a′. Cloudflare Workers (recommended — free, no card, never sleeps)

```bash
cd cloudflare
npx wrangler login               # opens browser, free Cloudflare account is enough
npx wrangler deploy              # → https://epub-translate-worker.<you>.workers.dev
npx wrangler secret put WORKER_SECRET   # optional, same value as the bot
```

Or via dashboard: **Workers & Pages → Create → Import a repository** → pick this repo →
root directory `cloudflare` → Deploy. Test: `curl https://…workers.dev/health`.

Free plan: 100 000 requests/day, 10 ms CPU per request (network wait to Google is
not counted) — plenty for several books a day.

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

### 1d′. Deno Deploy (free, no card)

<https://dash.deno.com> → **New Project** → link this GitHub repo → entry point
`deno_worker.ts` → Deploy. Set `WORKER_SECRET` under *Environment Variables* if used.
Worker URL: `https://<project>.deno.dev`

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
# {"status":"ok","platform":"huggingface","proxy":false,"uptime":12,...}   (or "cloudflare", "render", …)
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
/addworker https://user-space.hf.space https://epub-translate-worker.user.workers.dev https://proj.onrender.com https://proj.vercel.app https://user.pythonanywhere.com https://proj.deno.dev
```

The bot load-balances by latency/load, auto-disables dead workers and pings them
every `WORKER_PING_INTERVAL` seconds to keep sleepy free tiers awake.

---

## Features

| User | Admin |
|---|---|
| **Coloured** persistent menu: 🔵 **👑 Premium** (full-width) · 🟢 🌐 Language · 🔵 📂 Output · 🟢 📊 Status · 🔵 📖 Help · 🔴 ❌ Cancel — zero inline nav/close buttons (only real actions like 🟢 Start / 🔵 Pay / 🔴 Cancel stay inline, coloured too) | Inline panel: Workers · Stats · Queue · Broadcast · Health check |
| 20 languages, live progress bar + ETA, cancel button | Add / pause / remove workers (stable ids), refresh & back buttons everywhere |
| Free tier metered in characters/day, 6 paid plans (Razorpay auto-verify + Telegram ⭐ Stars) | `/addpremium /addplan /addcredits /revoke /ban /unban /user /broadcast` |
| Formatting preserved (bold, links, images, TOC, CSS) | SQLite persistence, rotating logs, force-sub (public **or** private channel) |
| Formats: EPUB, PDF (layout kept, Noto fonts auto-downloaded), DOCX, TXT/MD, HTML | Output format & split size selectable per file or as defaults |
| **Novel clean-up**: crawler summary/synopsis page, “Source / Generated by” lines and the TOC page are removed so TTS starts at chapter 1 (toggle in 📂 Output) | **Sticker-style button icons**: `/seticon premium 👑` with a *custom (Premium) emoji* puts that animated emoji in front of the button text; `/icons` lists / resets them |

All settings via environment variables — see `.env.example`.

### Coloured buttons & custom-emoji icons

`bot.py` uses **[kurigram](https://pypi.org/project/kurigram/)** (the maintained Pyrogram fork, same `import pyrogram`) so it
can send Bot API 9.4 button styles:

* **Colours** work for every bot, no extra requirement — blue (`primary`), green (`success`), red (`danger`).
  The mapping lives in `BTN_COLOR` in `bot.py`.
* **Custom-emoji icons** (the animated sticker-style emoji you see in Premium emoji packs, e.g. the 👑 / 🔥 / 💎 ones)
  are shown *in front of* the button text. Telegram only renders them if the **bot owner's account has Telegram Premium**
  (or the bot bought a username on Fragment). Set them at runtime, no redeploy:

  ```
  /seticon premium 👑      ← the 👑 must be picked from a *custom* emoji pack, not the normal emoji keyboard
  /seticon cancel  ❌       (or reply to any message containing the custom emoji)
  /seticon premium off     remove one · /icons reset  remove all · /icons  show status
  ```

  Keys: `premium lang output status help cancel admin start save pay verify retry join joined support`.
  If Telegram rejects the icons (no Premium), the bot logs a warning, drops the icons automatically and keeps the
  coloured buttons — nothing breaks. Users see the new keyboard after their next `/start`.
* Still on the old `pyrogram==2.0.106` wheel? Everything degrades to plain white buttons.

### Robustness (bot.py)

* **Every handler is wrapped** (`@guarded`): an unexpected exception is logged and the
  user gets *"Something went wrong… try again"* instead of a button that spins forever.
* `safe_reply / safe_edit / safe_answer / safe_delete / safe_send` never raise — deleted
  messages, expired callback queries, flood-waits and users who blocked the bot are all handled.
* Reply-keyboard taps are matched **after Unicode normalisation** (`⚙️` vs `⚙`, stray spaces),
  so old cached keyboards keep working on every client. Non-admins tapping a cached
  **🛠 Admin** button get a proper answer; unknown `/commands`, photos and stickers too.
* Double-tap / timer race on **▶️ Start** can no longer queue the same file twice or delete
  the file of a live job; the options panel extends its deadline while you type a custom size
  and always auto-starts eventually (no more stuck "file in progress").
* A user waiting for a custom split size can still send a new document (was silently ignored).
* `/ban` also discards the user's waiting file; banned / cancelled users are re-checked
  after a slow download.
* Payment **verify** is serialised per link (no double activation); Razorpay link creation
  answers the callback first so the button never times out.
* On boot: stale `epub_*` temp dirs are removed and jobs left `queued/running` by a crash
  are marked failed; on shutdown, users with waiting/queued files are told to resend.
* Telegram **`/` command menu** is registered automatically (richer list for admins).

## Plans

| Plan | Price | What you get | Max file | Queue |
|---|---|---|---|---|
| 🆓 Free | — | **1,000,000 characters / day** (any number of files) | 20 MB | normal |
| 🎟 Starter Pack | ₹10 | **5 file credits, never expire** | 50 MB | faster |
| 🎫 Bulk Pack | ₹40 | **25 file credits, never expire** | 50 MB | faster |
| 🔹 Basic | ₹50 / 30 days | 5 files **every day** | 50 MB | faster |
| ⭐ Premium | ₹100 / 30 days | unlimited | 200 MB | priority |
| 👑 Premium 3 Months | ₹250 / 90 days | unlimited | 200 MB | priority |
| 🌟 Premium · Stars | **60 ⭐ Telegram Stars** / 30 days | unlimited (same as Premium) | 200 MB | priority |

### Free tier = characters, not files

The free plan is metered in **characters per day** (`FREE_DAILY_CHARS`, default 1,000,000).
Any number of files can be sent; once a document is parsed its character count is compared
with what is left today **before** a single request is sent. If it doesn't fit, the user is
told how many characters the file has and how many remain, and nothing is consumed. Usage is
added only after a job finishes successfully (credits / subscriptions are not metered).

### Telegram Stars (⭐ XTR)

`Premium · Stars` is paid **inside Telegram** — no Razorpay account, card or UPI needed, and
it works from every country. Flow: tap the plan → the bot sends a native invoice
(`messages.SendMedia` + `InputMediaInvoice(currency="XTR")`) → Telegram asks the bot via
`UpdateBotPrecheckoutQuery` (we check payload, user, amount) → on
`MessageActionPaymentSentMe` the plan is activated automatically, idempotently, and the
Stars `charge_id` is stored for refunds (`payments.charge_id`). Stars revenue is shown
separately in 📈 Stats. Configure with `STARS_ENABLED`, `STARS_PREMIUM_PRICE`,
`STARS_PREMIUM_DAYS`. A Stars Premium is stored as the regular `premium` plan, so
upgrades/extensions behave identically.

How entitlements are consumed for each file (`resolve_access`):

1. admin → 2. active subscription while its daily quota lasts → 3. credits → 4. free quota.

* Credits are only spent **after** the daily quota is used up, so they are never wasted,
  and they are refunded automatically if a job fails or is cancelled.
* Buying a higher subscription while another is active upgrades immediately and carries
  the remaining days over; buying a lower one while on Premium never downgrades.
* Every price / limit / duration is an env var (`STARTER_*`, `BULK_*`, `BASIC_*`,
  `PREMIUM_*`, `PREMIUM3_*`, `CREDIT_MAX_FILE_MB`), and admins can grant anything
  manually: `/addplan USER_ID basic`, `/addplan USER_ID starter`, `/addcredits USER_ID 10`.
* Existing databases are migrated on start-up (new `plan` / `credits` / `daily_chars` /
  `strip_extras` / `payments.currency` / `payments.charge_id` columns; old Premium users
  are mapped to the `premium` plan).

## Novel EPUB clean-up (summary / Source / TOC pages)

EPUBs made by novel crawlers (Lightnovel Crawler and its Telegram bots, WebToEpub, …) start
with an *intro* page (synopsis/summary, `Author:`, `Source: https://…`, `Generated by …`,
`Made by BOT @…`) followed by a **table-of-contents page inside the reading order**, and every
chapter ends with a `Source / Generated by` footer. Text-to-speech readers read all of that
aloud before the story begins.

`docconv.strip_crawler_extras()` (on by default, toggle **🧹 Remove summary / Source / TOC
pages** in the per-file panel or ⚙️ Output) rewrites the EPUB before translation:

* intro / synopsis and TOC pages are removed from the **spine** (only within the first four
  items — never deeper); the `nav` document stays in the manifest as EPUB 3 requires;
* dangling entries are pruned from `nav.xhtml` and `toc.ncx`;
* small footer blocks carrying `Source:` / `Generated by` / `Downloaded from` + a URL are
  dropped from chapters, prose is never touched;
* safety rails: a page whose heading reads *Chapter N* or that still has > 3000 characters of
  real prose is never treated as an intro; a real *Foreword / Prologue* without crawler marks
  stays; the last remaining document is never removed; books without clutter are copied
  byte-for-byte and the pass is idempotent.

Because the clutter is removed *before* counting, it is also not translated or billed against
the free character budget. Tests: `python tests/test_strip_extras.py`.

## Supported formats

| Input | How it is translated | What stays intact |
|---|---|---|
| `.epub` | every text node of every chapter + NCX labels | chapters, CSS, images, links, TOC |
| `.pdf` | each text block is redacted and re-typed in the same box with a Unicode Noto font (auto-shrinks to fit) | page layout, images, vector art, links. Scanned/image-only PDFs are rejected (no OCR) |
| `.docx` | `<w:t>` runs of body, headers, footers, foot/endnotes (runs of one paragraph are merged so sentences translate as a whole) | styles, tables, images, numbering |
| `.txt` `.md` | line by line | blank lines, indentation, CRLF |
| `.html` `.htm` `.xhtml` | text nodes + `<title>` | tags, attributes, scripts, CSS |

Fonts for PDF output are fetched from Google Noto on first use and cached in `DATA_DIR/fonts`.

## Output format & splitting

After a file is translated the user can get it back **in a different format** and/or
**cut into several files of a maximum size** (`docconv.py`):

* **Format** — any input (EPUB · PDF · DOCX · TXT/MD · HTML) → **EPUB · PDF · DOCX · TXT · HTML**.
  Conversion goes through a small intermediate model (chapters → XHTML fragments + image
  store), so headings, bold/italic, lists, tables, links and images survive where the target
  format allows it. "Same as input" is the default and skips conversion entirely.
* **Split** — parts are *native* files of the same format, each ≤ the chosen size:
  EPUB by chapters (cover + TOC only in part 1, long chapters cut at paragraph level),
  PDF by page ranges (bookmarks re-based), DOCX by body paragraphs (styles/headers kept),
  HTML by body children, TXT by lines. Parts are named `book (part 2 of 5).epub`.
  Presets come from `SPLIT_PRESETS_MB`; users can also type a custom size (`500kb`,
  `25mb`, `1.5gb`; minimum `SPLIT_MIN_KB`, maximum `TG_MAX_FILE_MB`).
* **How it is asked** — after each upload a panel shows the format/split buttons and
  ▶️ *Start*; it auto-starts with the defaults after `OPTIONS_TIMEOUT` seconds.
  `/settings` (📂 Output button) stores per-user defaults and can switch the panel off
  (`Ask for every file: OFF`) so files start immediately. `/cancel` also discards a file
  waiting on the panel.
* If a conversion or split fails the original translated file is still delivered with a
  short ℹ️ note in the caption — nothing is lost.

`python tests/make_fixtures.py && python tests/test_docconv.py` exercises every input → every
output plus splitting for all five formats.

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
app.py              worker (FastAPI) — identical on every Python platform
hf_app.py           Hugging Face Gradio/ZeroGPU entry (mounts Gradio UI on top of app.py)
cloudflare/         Cloudflare Worker (worker.js + wrangler.toml) — JS port of app.py
deno_worker.ts      Deno Deploy entry (re-uses cloudflare/worker.js)
api/index.py        Vercel serverless entry (re-exports app)
wsgi.py             PythonAnywhere WSGI entry (a2wsgi)
Dockerfile          Fly / Koyeb / any Docker host / VPS
Procfile            Railway / Koyeb / Heroku-style
render.yaml         Render blueprint
vercel.json         Vercel config
requirements.txt    worker deps (gradio only needed on HF)
bot.py              master bot — Oracle VPS only
requirements-bot.txt, epub-bot.service, oracle_setup.sh   bot deps / systemd / installer
```
