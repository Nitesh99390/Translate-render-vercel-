"""
EPUB Translator — Worker node
=============================
Stateless FastAPI service that translates a batch of strings.
Deploy on Render (web service) or Vercel (serverless) — same file works on both.

Endpoints
  GET  /            health check (used by the master for keep-alive & latency)
  POST /translate   {"text_list": [...], "lang": "hi"}  ->  {"success": true, "translated": [...]}

Optional env
  WORKER_SECRET   if set, master must send header  X-Worker-Key: <secret>
  MAX_ITEMS       max strings per request (default 400)
  MAX_CHARS       max total characters per request (default 30000)
  (the master sends ~150 items / ~9000 chars per batch by default; keep
   these limits above the master's BATCH_MAX_ITEMS / BATCH_MAX_CHARS)
"""

import asyncio
import os
import time
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

app = FastAPI(title="EPUB Translator Worker", version="2.0", docs_url=None, redoc_url=None)
_started = time.time()
_stats = {"requests": 0, "strings": 0, "errors": 0}
_session: Optional[aiohttp.ClientSession] = None


class TranslateIn(BaseModel):
    text_list: List[str] = Field(default_factory=list)
    lang: str = "hi"
    source: str = "auto"


async def session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit=64, limit_per_host=0, ttl_dns_cache=300, keepalive_timeout=60)
        _session = aiohttp.ClientSession(
            headers=HEADERS, connector=connector, timeout=aiohttp.ClientTimeout(total=40)
        )
    return _session


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _session and not _session.closed:
        await _session.close()


@app.get("/")
async def health() -> dict:
    return {"status": "ok", "uptime": int(time.time() - _started), **_stats}


async def _google(texts: List[str], lang: str, source: str) -> List[str]:
    s = await session()
    params = {"client": "gtx", "sl": source or "auto", "tl": lang}
    payload = [("q", t) for t in texts]
    last_err = "unknown"
    for attempt in range(3):
        try:
            async with s.post(GOOGLE_URL, params=params, data=payload) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if isinstance(data, list) and len(data) == len(texts):
                        return [x[0] if isinstance(x, list) else str(x) for x in data]
                    if len(texts) == 1 and isinstance(data, list) and data:
                        return [data[0] if isinstance(data[0], str) else data[0][0]]
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
    texts = body.text_list
    if not texts:
        return {"success": True, "translated": []}
    if len(texts) > MAX_ITEMS or sum(len(t) for t in texts) > MAX_CHARS:
        raise HTTPException(status_code=413, detail="batch too large")

    _stats["requests"] += 1
    _stats["strings"] += len(texts)
    try:
        out = await _google(texts, body.lang, body.source)
        return {"success": True, "translated": out}
    except Exception as e:  # noqa: BLE001
        _stats["errors"] += 1
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
