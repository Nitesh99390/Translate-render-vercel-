"""
EPUB Translator — Worker node
=============================
Stateless FastAPI service that translates a batch of strings.

The SAME file runs everywhere — no platform-specific code paths:

  Platform          Entry point                         Notes
  ─────────────────────────────────────────────────────────────────────────────
  Render            render.yaml  → uvicorn app:app      free plan sleeps, bot pings it
  Vercel            vercel.json  → app.py (ASGI)        serverless, 60 s max per call
  Hugging Face      Dockerfile   → uvicorn :7860         Docker Space, always-on
  PythonAnywhere    wsgi.py      → a2wsgi(app)          WSGI only, outbound via proxy
  Railway / Koyeb / Fly / Heroku-like   Procfile        `web: uvicorn app:app ...`
  Any VPS / Docker  python app.py  or  docker run

Endpoints
  GET  /            health check (used by the master for keep-alive & latency)
  GET  /health      same as / (some platforms expect this path)
  POST /translate   {"text_list": [...], "lang": "hi"}  ->  {"success": true, "translated": [...]}

Optional env
  WORKER_SECRET   if set, master must send header  X-Worker-Key: <secret>
  MAX_ITEMS       max strings per request (default 400)
  MAX_CHARS       max total characters per request (default 30000)
  PORT            listen port when run directly (default 8000; HF uses 7860)
  HTTPS_PROXY / HTTP_PROXY / https_proxy
                  honoured automatically (PythonAnywhere free accounts route
                  outbound traffic through proxy.server:3128)
  (the master sends ~150 items / ~9000 chars per batch by default; keep
   these limits above the master's BATCH_MAX_ITEMS / BATCH_MAX_CHARS)
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import List, Optional

import aiohttp
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "400"))
MAX_CHARS = int(os.environ.get("MAX_CHARS", "30000"))
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")
GOOGLE_URL = "https://translate.googleapis.com/translate_a/t"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def _detect_platform() -> str:
    """Best-effort name of the hosting platform (informational only)."""
    env = os.environ
    if env.get("VERCEL") or env.get("VERCEL_ENV"):
        return "vercel"
    if env.get("RENDER") or env.get("RENDER_SERVICE_ID"):
        return "render"
    if env.get("SPACE_ID") or env.get("SPACE_HOST"):
        return "huggingface"
    if env.get("PYTHONANYWHERE_DOMAIN") or env.get("PYTHONANYWHERE_SITE"):
        return "pythonanywhere"
    if env.get("RAILWAY_ENVIRONMENT") or env.get("RAILWAY_PROJECT_ID"):
        return "railway"
    if env.get("KOYEB_APP_NAME") or env.get("KOYEB_SERVICE_NAME"):
        return "koyeb"
    if env.get("FLY_APP_NAME"):
        return "fly"
    if env.get("DYNO"):
        return "heroku"
    return "generic"


PLATFORM = _detect_platform()

# Outbound proxy — PythonAnywhere free tier only allows internet access through
# proxy.server:3128 and sets these variables for every process. aiohttp needs
# trust_env=True to pick them up; on other platforms they are simply unset.
_PROXY = (
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("http_proxy")
    or None
)

_started = time.time()
_stats = {"requests": 0, "strings": 0, "errors": 0}
_session: Optional[aiohttp.ClientSession] = None
_session_loop: Optional[asyncio.AbstractEventLoop] = None


@asynccontextmanager
async def _lifespan(_: FastAPI):
    yield
    if _session and not _session.closed:
        try:
            await _session.close()
        except Exception:  # noqa: BLE001  (loop may already be gone on serverless)
            pass


app = FastAPI(title="EPUB Translator Worker", version="2.1", docs_url=None, redoc_url=None, lifespan=_lifespan)


class TranslateIn(BaseModel):
    text_list: List[str] = Field(default_factory=list)
    lang: str = "hi"
    source: str = "auto"


async def session() -> aiohttp.ClientSession:
    global _session, _session_loop
    loop = asyncio.get_running_loop()
    # Serverless hosts (Vercel) and a2wsgi (PythonAnywhere) may run successive
    # requests on a *different* event loop while the module stays imported.  An
    # aiohttp session is bound to the loop it was created on, so reusing it
    # raises "attached to a different loop" / "Event loop is closed".
    if _session is not None and (_session.closed or _session_loop is not loop):
        if not _session.closed and _session_loop is not None and not _session_loop.is_closed():
            try:
                await _session.close()
            except Exception:  # noqa: BLE001
                pass
        _session = None
    if _session is None:
        _session_loop = loop
        connector = aiohttp.TCPConnector(limit=64, limit_per_host=0, ttl_dns_cache=300, keepalive_timeout=60)
        _session = aiohttp.ClientSession(
            headers=HEADERS,
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=40),
            trust_env=True,  # honour HTTP(S)_PROXY (PythonAnywhere)
        )
    return _session


def _health_payload() -> dict:
    return {
        "status": "ok",
        "platform": PLATFORM,
        "proxy": bool(_PROXY),
        "uptime": int(time.time() - _started),
        "limits": {"max_items": MAX_ITEMS, "max_chars": MAX_CHARS},
        **_stats,
    }


@app.get("/")
async def health() -> dict:
    return _health_payload()


@app.get("/health")
async def health_alias() -> dict:
    return _health_payload()


@app.head("/")
async def health_head() -> dict:
    return {}


async def _google(texts: List[str], lang: str, source: str) -> List[str]:
    s = await session()
    params = {"client": "gtx", "sl": source or "auto", "tl": lang}
    payload = [("q", t) for t in texts]
    last_err = "unknown"
    for attempt in range(3):
        try:
            async with s.post(GOOGLE_URL, params=params, data=payload, proxy=_PROXY) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    # single item: Google returns ["text"] (a plain string, not a list)
                    if len(texts) == 1 and isinstance(data, list) and data:
                        first = data[0]
                        return [first if isinstance(first, str) else str(first[0])]
                    if isinstance(data, list) and len(data) == len(texts):
                        return [str(x[0]) if isinstance(x, list) else str(x) for x in data]
                    last_err = "unexpected response shape"
                elif resp.status == 429:
                    last_err = "rate limited"
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                else:
                    last_err = f"HTTP {resp.status}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError(last_err)


@app.post("/translate")
async def translate(body: TranslateIn, x_worker_key: Optional[str] = Header(default=None)) -> dict:
    if WORKER_SECRET and x_worker_key != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    texts = [str(t) for t in body.text_list]
    if not texts:
        return {"success": True, "translated": []}
    if len(texts) > MAX_ITEMS or sum(len(t) for t in texts) > MAX_CHARS:
        raise HTTPException(status_code=413, detail="batch too large")
    lang = (body.lang or "hi").strip() or "hi"

    _stats["requests"] += 1
    _stats["strings"] += len(texts)
    try:
        out = await _google(texts, lang, body.source)
        return {"success": True, "translated": out}
    except Exception as e:  # noqa: BLE001
        _stats["errors"] += 1
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn

    # Hugging Face Docker Spaces expose 7860; everyone else injects $PORT.
    default_port = "7860" if PLATFORM == "huggingface" else "8000"
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", default_port)))
