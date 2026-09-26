#!/usr/bin/env python3
"""
EPUB Translator Bot — Master (single file)
==========================================
Run this on the Oracle VM.  Translation workers (app.py) run on Render / Vercel.

Highlights
----------
* Minimal reply keyboard for users, full inline admin panel for the owner
* SQLite persistence (users, payments, workers, jobs) — survives restarts
* Priority job queue (premium first), per-job cancel button, live progress bar
* Formatting-safe translation: EPUB is processed at ZIP level, only text nodes
  are translated so bold/italic/links/images/CSS stay 100 % intact
* Smart worker pool: health checks, latency tracking, auto-disable on failure,
  least-loaded routing, keep-alive pings, direct Google fallback
* Razorpay payment links with automatic verification ("I've paid" button)
* Free-tier daily limits, premium expiry, ban system, broadcast, force-sub

Environment variables (see .env.example)
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
import warnings
from bs4 import BeautifulSoup, NavigableString
from bs4.element import PreformattedString, Script, Stylesheet, TemplateString

try:  # bs4 >= 4.11 warns when XHTML is parsed with an HTML parser — intended here
    from bs4 import XMLParsedAsHTMLWarning

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except Exception:  # pragma: no cover
    pass
from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import FloodWait, MessageNotModified, UserNotParticipant
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv is optional
    pass

try:
    import razorpay  # type: ignore
except Exception:  # razorpay is optional
    razorpay = None

# ═══════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return _env(key, str(default)).lower() in ("1", "true", "yes", "on")


class Config:
    API_ID = _env_int("API_ID", 0)
    API_HASH = _env("API_HASH")
    BOT_TOKEN = _env("BOT_TOKEN")
    OWNER_ID = _env_int("OWNER_ID", 0)
    # optional extra admins: "123,456"
    ADMIN_IDS = {int(x) for x in _env("ADMIN_IDS").split(",") if x.strip().isdigit()}

    RAZORPAY_KEY_ID = _env("RAZORPAY_KEY_ID")
    RAZORPAY_KEY_SECRET = _env("RAZORPAY_KEY_SECRET")
    PREMIUM_PRICE_INR = _env_int("PREMIUM_PRICE_INR", 100)
    PREMIUM_DAYS = _env_int("PREMIUM_DAYS", 30)

    FREE_DAILY_LIMIT = _env_int("FREE_DAILY_LIMIT", 2)          # files / day
    FREE_MAX_FILE_MB = _env_int("FREE_MAX_FILE_MB", 20)
    PREMIUM_MAX_FILE_MB = _env_int("PREMIUM_MAX_FILE_MB", 200)

    MAX_CONCURRENT_JOBS = _env_int("MAX_CONCURRENT_JOBS", 2)
    # ── throughput tuning ──────────────────────────────────────────────
    # Total in-flight translate requests per job (across all workers + direct).
    MAX_PARALLEL_REQUESTS = _env_int("MAX_PARALLEL_REQUESTS", 48)
    # How many requests a single remote worker may serve at once.
    WORKER_CONCURRENCY = _env_int("WORKER_CONCURRENCY", 8)
    # How many requests the master itself sends straight to Google in parallel
    # (0 = master only used as fallback when every worker is down).
    DIRECT_CONCURRENCY = _env_int("DIRECT_CONCURRENCY", 6)
    # Bigger batches = far fewer round-trips.  Google's gtx endpoint copes fine
    # with ~10k chars / ~150 items per POST.
    BATCH_MAX_ITEMS = _env_int("BATCH_MAX_ITEMS", 150)
    BATCH_MAX_CHARS = _env_int("BATCH_MAX_CHARS", 9000)
    REQUEST_TIMEOUT = _env_int("REQUEST_TIMEOUT", 60)
    WORKER_FAIL_THRESHOLD = _env_int("WORKER_FAIL_THRESHOLD", 5)
    WORKER_PING_INTERVAL = _env_int("WORKER_PING_INTERVAL", 480)  # seconds
    DIRECT_FALLBACK = _env_bool("DIRECT_FALLBACK", True)
    WORKER_SECRET = _env("WORKER_SECRET")                       # must match workers' WORKER_SECRET

    FORCE_SUB_CHANNEL = _env("FORCE_SUB_CHANNEL")               # "@channel" or id
    SUPPORT_CONTACT = _env("SUPPORT_CONTACT", "@admin")

    DATA_DIR = Path(_env("DATA_DIR", "data"))
    DB_PATH = DATA_DIR / "bot.db"
    LOG_PATH = DATA_DIR / "bot.log"
    SESSION_NAME = _env("SESSION_NAME", "translator_bot")
    DEFAULT_LANG = _env("DEFAULT_LANG", "hi")

    @classmethod
    def validate(cls) -> None:
        missing = [k for k in ("API_ID", "API_HASH", "BOT_TOKEN", "OWNER_ID") if not getattr(cls, k)]
        if missing:
            sys.exit(f"[config] Missing required env vars: {', '.join(missing)}")
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)

    @classmethod
    def is_admin(cls, user_id: int) -> bool:
        return user_id == cls.OWNER_ID or user_id in cls.ADMIN_IDS


Config.validate()

# ═══════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(Config.LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
    ],
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# ═══════════════════════════════════════════════════════════════════════════
#  LANGUAGES
# ═══════════════════════════════════════════════════════════════════════════

LANGUAGES: Dict[str, str] = {
    "hi": "🇮🇳 Hindi", "bn": "🇧🇩 Bengali", "ta": "🇮🇳 Tamil", "te": "🇮🇳 Telugu",
    "mr": "🇮🇳 Marathi", "gu": "🇮🇳 Gujarati", "kn": "🇮🇳 Kannada", "ml": "🇮🇳 Malayalam",
    "pa": "🇮🇳 Punjabi", "ur": "🇵🇰 Urdu", "ne": "🇳🇵 Nepali", "en": "🇬🇧 English",
    "es": "🇪🇸 Spanish", "fr": "🇫🇷 French", "de": "🇩🇪 German", "ar": "🇸🇦 Arabic",
    "ru": "🇷🇺 Russian", "id": "🇮🇩 Indonesian", "pt": "🇧🇷 Portuguese", "zh-CN": "🇨🇳 Chinese",
}


def lang_name(code: str) -> str:
    return LANGUAGES.get(code, code)


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════════════════════
#  DATABASE (SQLite, thread-safe, tiny synchronous ops)
# ═══════════════════════════════════════════════════════════════════════════


class Database:
    def __init__(self, path: Path):
        self._lock = threading.RLock()
        self._con = sqlite3.connect(path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self) -> None:
        with self._lock, self._con:
            self._con.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    username TEXT,
                    lang TEXT DEFAULT 'hi',
                    premium_until INTEGER DEFAULT 0,
                    daily_used INTEGER DEFAULT 0,
                    daily_date TEXT DEFAULT '',
                    total_files INTEGER DEFAULT 0,
                    banned INTEGER DEFAULT 0,
                    joined INTEGER
                );
                CREATE TABLE IF NOT EXISTS workers (
                    url TEXT PRIMARY KEY,
                    enabled INTEGER DEFAULT 1,
                    added INTEGER
                );
                CREATE TABLE IF NOT EXISTS payments (
                    link_id TEXT PRIMARY KEY,
                    user_id INTEGER,
                    amount INTEGER,
                    days INTEGER,
                    status TEXT DEFAULT 'created',
                    created INTEGER
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    file_name TEXT,
                    lang TEXT,
                    status TEXT,
                    segments INTEGER DEFAULT 0,
                    chars INTEGER DEFAULT 0,
                    seconds REAL DEFAULT 0,
                    created INTEGER
                );
                """
            )

    # ── generic helpers ────────────────────────────────────────────────────
    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock, self._con:
            self._con.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._con.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._con.execute(sql, params).fetchall()

    # ── users ──────────────────────────────────────────────────────────────
    def upsert_user(self, uid: int, name: str, username: Optional[str]) -> sqlite3.Row:
        row = self._one("SELECT * FROM users WHERE id=?", (uid,))
        if row is None:
            self._exec(
                "INSERT INTO users(id,name,username,lang,joined) VALUES(?,?,?,?,?)",
                (uid, name, username or "", Config.DEFAULT_LANG, int(time.time())),
            )
        else:
            self._exec("UPDATE users SET name=?, username=? WHERE id=?", (name, username or "", uid))
        return self._one("SELECT * FROM users WHERE id=?", (uid,))  # type: ignore

    def get_user(self, uid: int) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM users WHERE id=?", (uid,))

    def set_lang(self, uid: int, lang: str) -> None:
        self._exec("UPDATE users SET lang=? WHERE id=?", (lang, uid))

    def is_premium(self, uid: int) -> bool:
        if Config.is_admin(uid):
            return True
        row = self.get_user(uid)
        return bool(row and row["premium_until"] > time.time())

    def add_premium(self, uid: int, days: int) -> int:
        row = self.get_user(uid)
        base = max(int(time.time()), row["premium_until"] if row else 0)
        until = base + days * 86400
        if row is None:
            self._exec(
                "INSERT INTO users(id,name,username,lang,premium_until,joined) VALUES(?,?,?,?,?,?)",
                (uid, "", "", Config.DEFAULT_LANG, until, int(time.time())),
            )
        else:
            self._exec("UPDATE users SET premium_until=? WHERE id=?", (until, uid))
        return until

    def revoke_premium(self, uid: int) -> None:
        self._exec("UPDATE users SET premium_until=0 WHERE id=?", (uid,))

    def daily_used(self, uid: int) -> int:
        row = self.get_user(uid)
        today = today_str()
        if not row or row["daily_date"] != today:
            return 0
        return row["daily_used"]

    def record_usage(self, uid: int) -> None:
        today = today_str()
        row = self.get_user(uid)
        used = (row["daily_used"] if row and row["daily_date"] == today else 0) + 1
        self._exec(
            "UPDATE users SET daily_used=?, daily_date=?, total_files=total_files+1 WHERE id=?",
            (used, today, uid),
        )

    def set_banned(self, uid: int, banned: bool) -> None:
        self._exec("UPDATE users SET banned=? WHERE id=?", (1 if banned else 0, uid))

    def is_banned(self, uid: int) -> bool:
        row = self.get_user(uid)
        return bool(row and row["banned"])

    def all_user_ids(self) -> List[int]:
        return [r["id"] for r in self._all("SELECT id FROM users WHERE banned=0")]

    def stats(self) -> dict:
        now = int(time.time())
        return {
            "users": self._one("SELECT COUNT(*) c FROM users")["c"],
            "premium": self._one("SELECT COUNT(*) c FROM users WHERE premium_until>?", (now,))["c"],
            "banned": self._one("SELECT COUNT(*) c FROM users WHERE banned=1")["c"],
            "today_users": self._one("SELECT COUNT(*) c FROM users WHERE joined>?", (now - 86400,))["c"],
            "jobs_total": self._one("SELECT COUNT(*) c FROM jobs")["c"],
            "jobs_done": self._one("SELECT COUNT(*) c FROM jobs WHERE status='done'")["c"],
            "jobs_failed": self._one("SELECT COUNT(*) c FROM jobs WHERE status='failed'")["c"],
            "chars": self._one("SELECT COALESCE(SUM(chars),0) c FROM jobs WHERE status='done'")["c"],
            "payments": self._one("SELECT COUNT(*) c FROM payments WHERE status='paid'")["c"],
            "revenue": self._one("SELECT COALESCE(SUM(amount),0) c FROM payments WHERE status='paid'")["c"],
        }

    # ── workers ────────────────────────────────────────────────────────────
    def workers(self) -> List[sqlite3.Row]:
        return self._all("SELECT * FROM workers ORDER BY added")

    def add_worker(self, url: str) -> bool:
        if self._one("SELECT 1 FROM workers WHERE url=?", (url,)):
            return False
        self._exec("INSERT INTO workers(url,enabled,added) VALUES(?,1,?)", (url, int(time.time())))
        return True

    def remove_worker(self, url: str) -> bool:
        if not self._one("SELECT 1 FROM workers WHERE url=?", (url,)):
            return False
        self._exec("DELETE FROM workers WHERE url=?", (url,))
        return True

    def set_worker_enabled(self, url: str, enabled: bool) -> None:
        self._exec("UPDATE workers SET enabled=? WHERE url=?", (1 if enabled else 0, url))

    # ── payments ───────────────────────────────────────────────────────────
    def add_payment(self, link_id: str, uid: int, amount: int, days: int) -> None:
        self._exec(
            "INSERT OR REPLACE INTO payments(link_id,user_id,amount,days,status,created) VALUES(?,?,?,?,'created',?)",
            (link_id, uid, amount, days, int(time.time())),
        )

    def get_payment(self, link_id: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM payments WHERE link_id=?", (link_id,))

    def mark_paid(self, link_id: str) -> None:
        self._exec("UPDATE payments SET status='paid' WHERE link_id=?", (link_id,))

    # ── jobs ───────────────────────────────────────────────────────────────
    def add_job(self, uid: int, file_name: str, lang: str) -> int:
        with self._lock, self._con:
            cur = self._con.execute(
                "INSERT INTO jobs(user_id,file_name,lang,status,created) VALUES(?,?,?,'queued',?)",
                (uid, file_name, lang, int(time.time())),
            )
            return int(cur.lastrowid)

    def finish_job(self, job_id: int, status: str, segments: int = 0, chars: int = 0, seconds: float = 0) -> None:
        self._exec(
            "UPDATE jobs SET status=?, segments=?, chars=?, seconds=? WHERE id=?",
            (status, segments, chars, round(seconds, 1), job_id),
        )


db = Database(Config.DB_PATH)

# ═══════════════════════════════════════════════════════════════════════════
#  WORKER POOL
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class Worker:
    url: str
    enabled: bool = True
    healthy: bool = True
    inflight: int = 0
    fails: int = 0
    ok: int = 0
    latency: float = 0.0  # moving average, seconds
    last_seen: float = 0.0
    disabled_until: float = 0.0
    capacity: int = 0     # 0 → Config.WORKER_CONCURRENCY

    @property
    def available(self) -> bool:
        return self.enabled and (self.healthy or time.time() > self.disabled_until)

    @property
    def max_inflight(self) -> int:
        return self.capacity or Config.WORKER_CONCURRENCY

    @property
    def has_slot(self) -> bool:
        return self.inflight < self.max_inflight

    def score(self) -> float:
        # lower is better – prefer least-loaded (relative to capacity) & fastest
        return self.inflight / max(self.max_inflight, 1) + min(self.latency, 5.0) / 10

    def report(self, ok: bool, latency: float = 0.0) -> None:
        if ok:
            self.ok += 1
            self.fails = 0
            self.healthy = True
            self.last_seen = time.time()
            self.latency = latency if self.latency == 0 else self.latency * 0.7 + latency * 0.3
        else:
            self.fails += 1
            if self.fails >= Config.WORKER_FAIL_THRESHOLD:
                self.healthy = False
                self.disabled_until = time.time() + 120  # retry after 2 min
                log.warning("Worker %s marked unhealthy", self.url)


class WorkerPool:
    def __init__(self) -> None:
        self.workers: Dict[str, Worker] = {}
        self._lock = asyncio.Lock()
        self.reload()

    def reload(self) -> None:
        rows = db.workers()
        current = {r["url"]: bool(r["enabled"]) for r in rows}
        for url in list(self.workers):
            if url not in current:
                del self.workers[url]
        for url, enabled in current.items():
            w = self.workers.setdefault(url, Worker(url))
            w.enabled = enabled

    @staticmethod
    def normalize(url: str) -> str:
        url = url.strip()
        if not url.startswith("http"):
            url = "https://" + url
        return url.rstrip("/")

    def add(self, url: str) -> bool:
        url = self.normalize(url)
        ok = db.add_worker(url)
        self.reload()
        return ok

    def remove(self, url: str) -> bool:
        ok = db.remove_worker(url)
        self.reload()
        return ok

    def toggle(self, url: str) -> None:
        w = self.workers.get(url)
        if w:
            db.set_worker_enabled(url, not w.enabled)
            self.reload()

    def available(self) -> List[Worker]:
        return [w for w in self.workers.values() if w.available]

    def pick(self, exclude: Optional[set] = None, need_slot: bool = True) -> Optional[Worker]:
        avail = [w for w in self.available() if not exclude or w.url not in exclude]
        if need_slot:
            avail = [w for w in avail if w.has_slot]
        if not avail:
            return None
        return min(avail, key=Worker.score)

    def total_capacity(self) -> int:
        return sum(w.max_inflight for w in self.available())

    async def health_check(self, session: aiohttp.ClientSession) -> None:
        for w in list(self.workers.values()):
            if not w.enabled:
                continue
            t0 = time.monotonic()
            try:
                async with session.get(f"{w.url}/", timeout=aiohttp.ClientTimeout(total=20)) as r:
                    ok = r.status == 200
            except Exception:
                ok = False
            lat = time.monotonic() - t0
            if ok:
                w.healthy = True
                w.fails = 0
                w.last_seen = time.time()
                w.latency = lat if w.latency == 0 else w.latency * 0.7 + lat * 0.3
            else:
                w.report(False)

    def summary(self) -> str:
        if not self.workers:
            return "No workers configured."
        lines = []
        for i, w in enumerate(self.workers.values(), 1):
            if not w.enabled:
                state = "⏸ disabled"
            elif w.healthy:
                state = "🟢 online"
            else:
                state = "🔴 down"
            lines.append(
                f"{i}. <code>{html.escape(w.url)}</code>\n"
                f"   {state} · {w.latency*1000:.0f} ms · ok {w.ok} · busy {w.inflight}/{w.max_inflight}"
            )
        if Config.DIRECT_CONCURRENCY > 0:
            lines.append(
                f"⭐ <i>master → Google direct</i> · {direct_worker.latency*1000:.0f} ms · "
                f"ok {direct_worker.ok} · busy {direct_worker.inflight}/{direct_worker.max_inflight}"
            )
        return "\n".join(lines)


pool = WorkerPool()

# The master itself is also a translation endpoint ("direct" → Google).  It is
# used *in parallel* with the remote workers, not only as a fallback, so the
# Oracle VM's own bandwidth is never idle.
DIRECT_URL = "direct"
direct_worker = Worker(url=DIRECT_URL, capacity=max(Config.DIRECT_CONCURRENCY, 1))
direct_worker.enabled = Config.DIRECT_CONCURRENCY > 0 or Config.DIRECT_FALLBACK

# ═══════════════════════════════════════════════════════════════════════════
#  TRANSLATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════

GOOGLE_URL = "https://translate.googleapis.com/translate_a/t"
GOOGLE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


class TranslationError(Exception):
    pass


class Translator:
    """Batches text → workers + direct Google in parallel, least-loaded routing,
    per-endpoint concurrency caps, retries and fallback."""

    def __init__(self, session: aiohttp.ClientSession, lang: str):
        self.session = session
        self.lang = lang
        self.sem = asyncio.Semaphore(Config.MAX_PARALLEL_REQUESTS)
        self.cache: Dict[str, str] = {}
        self._slot_freed = asyncio.Event()

    # ── endpoint selection ─────────────────────────────────────────────────────

    def _candidates(self, tried: set) -> List[Worker]:
        cands = [w for w in pool.available() if w.url not in tried]
        if Config.DIRECT_CONCURRENCY > 0 and direct_worker.available and DIRECT_URL not in tried:
            cands.append(direct_worker)
        return cands

    async def _acquire(self, tried: set) -> Optional[Worker]:
        """Return the best endpoint that has a free slot, waiting if all are saturated."""
        deadline = time.monotonic() + Config.REQUEST_TIMEOUT
        while True:
            cands = self._candidates(tried)
            if not cands:
                return None
            free = [w for w in cands if w.has_slot]
            if free:
                w = min(free, key=Worker.score)
                w.inflight += 1
                return w
            if time.monotonic() > deadline:
                # everything saturated for a long time – just queue on the least loaded
                w = min(cands, key=Worker.score)
                w.inflight += 1
                return w
            self._slot_freed.clear()
            try:
                await asyncio.wait_for(self._slot_freed.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    def _release(self, w: Worker) -> None:
        w.inflight = max(0, w.inflight - 1)
        self._slot_freed.set()

    # ── transports ───────────────────────────────────────────────────────────────

    async def _via_worker(self, w: Worker, texts: List[str]) -> Optional[List[str]]:
        t0 = time.monotonic()
        try:
            headers = {"X-Worker-Key": Config.WORKER_SECRET} if Config.WORKER_SECRET else None
            async with self.session.post(
                f"{w.url}/translate",
                json={"text_list": texts, "lang": self.lang},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=Config.REQUEST_TIMEOUT),
            ) as r:
                if r.status != 200:
                    raise TranslationError(f"HTTP {r.status}")
                data = await r.json(content_type=None)
            out = data.get("translated") if data.get("success") else None
            if not isinstance(out, list) or len(out) != len(texts):
                raise TranslationError(data.get("error") or "bad worker response")
            w.report(True, time.monotonic() - t0)
            return [str(x) for x in out]
        except Exception as e:
            w.report(False)
            log.debug("worker %s failed: %s", w.url, e)
            return None

    async def _direct(self, texts: List[str]) -> Optional[List[str]]:
        t0 = time.monotonic()
        try:
            params = {"client": "gtx", "sl": "auto", "tl": self.lang}
            payload = [("q", t) for t in texts]
            async with self.session.post(
                GOOGLE_URL, params=params, data=payload, headers=GOOGLE_HEADERS,
                timeout=aiohttp.ClientTimeout(total=Config.REQUEST_TIMEOUT),
            ) as r:
                if r.status == 429:
                    # rate-limited: back off this endpoint briefly so workers take the load
                    direct_worker.healthy = False
                    direct_worker.disabled_until = time.time() + 15
                    return None
                if r.status != 200:
                    return None
                res = await r.json(content_type=None)
            if len(texts) == 1 and isinstance(res, list) and res and isinstance(res[0], str):
                out = [res[0]]
            else:
                out = [x[0] if isinstance(x, list) else x for x in res]
            if len(out) != len(texts):
                return None
            direct_worker.report(True, time.monotonic() - t0)
            return [str(x) for x in out]
        except Exception as e:
            log.debug("direct google failed: %s", e)
            return None

    async def _send(self, w: Worker, texts: List[str]) -> Optional[List[str]]:
        try:
            if w.url == DIRECT_URL:
                return await self._direct(texts)
            return await self._via_worker(w, texts)
        finally:
            self._release(w)

    async def translate_batch(self, texts: List[str]) -> List[str]:
        if not texts:
            return []
        async with self.sem:
            tried: set = set()
            for attempt in range(5):
                w = await self._acquire(tried)
                if w is None:
                    break
                tried.add(w.url)
                out = await self._send(w, texts)
                if out is not None:
                    return out
                await asyncio.sleep(0.3 * (attempt + 1))
            # last resort: hit Google directly even if DIRECT_CONCURRENCY == 0
            if Config.DIRECT_FALLBACK:
                for attempt in range(2):
                    direct_worker.inflight += 1
                    try:
                        out = await self._direct(texts)
                    finally:
                        self._release(direct_worker)
                    if out is not None:
                        return out
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise TranslationError("All workers failed")

    async def translate_many(self, texts: List[str], progress=None) -> List[str]:
        """Translate a flat list of strings preserving order; dedupes + batches."""
        result: List[Optional[str]] = [None] * len(texts)
        unique: Dict[str, List[int]] = {}
        for i, t in enumerate(texts):
            if t in self.cache:
                result[i] = self.cache[t]
            else:
                unique.setdefault(t, []).append(i)

        keys = list(unique)
        batches: List[List[str]] = []
        cur, cur_len = [], 0
        for k in keys:
            if cur and (len(cur) >= Config.BATCH_MAX_ITEMS or cur_len + len(k) > Config.BATCH_MAX_CHARS):
                batches.append(cur)
                cur, cur_len = [], 0
            cur.append(k)
            cur_len += len(k)
        if cur:
            batches.append(cur)

        done = failed = 0

        async def translate_or_split(batch: List[str]) -> List[str]:
            """Big batches are fast, but if one fails we halve it instead of
            dropping 150 segments back to the source language."""
            try:
                return await self.translate_batch(batch)
            except TranslationError:
                if len(batch) <= 4:
                    raise
                mid = len(batch) // 2
                left, right = await asyncio.gather(
                    translate_or_split(batch[:mid]), translate_or_split(batch[mid:]),
                    return_exceptions=True,
                )
                if isinstance(left, BaseException) and isinstance(right, BaseException):
                    raise TranslationError("batch failed")
                if isinstance(left, BaseException):
                    left = batch[:mid]
                if isinstance(right, BaseException):
                    right = batch[mid:]
                return list(left) + list(right)

        async def run(batch: List[str]) -> None:
            nonlocal done, failed
            try:
                out = await translate_or_split(batch)
            except TranslationError:
                failed += 1
                out = batch  # keep original text rather than lose content
            for src, dst in zip(batch, out):
                self.cache[src] = dst
                for idx in unique[src]:
                    result[idx] = dst
            done += 1
            if progress:
                await progress(done, len(batches))

        await asyncio.gather(*(run(b) for b in batches))
        if batches and failed == len(batches):
            raise TranslationError("Translation service unavailable — all workers failed")
        if failed:
            log.warning("%d/%d batches failed, kept original text", failed, len(batches))
        return [r if r is not None else t for r, t in zip(result, texts)]


# ═══════════════════════════════════════════════════════════════════════════
#  EPUB PROCESSOR (formatting-safe, zip level)
# ═══════════════════════════════════════════════════════════════════════════

SKIP_TAGS = {"script", "style", "code", "pre", "svg", "math", "head", "title", "meta", "link"}
DOC_EXT = (".xhtml", ".html", ".htm", ".xml")
_ws_re = re.compile(r"^(\s*)(.*?)(\s*)$", re.S)

# Non-text string nodes that must never be translated or counted.
#   PreformattedString = Comment, CData, ProcessingInstruction (<?xml ...?>),
#                        Declaration, Doctype (<!DOCTYPE ...>)
#   Script / Stylesheet / TemplateString = contents of <script>/<style>/<template>
# Different parsers expose these differently (html.parser yields the <?xml ?>
# prolog as a ProcessingInstruction, lxml as a Comment) — filtering by type
# makes segment/char counts identical no matter which parser is in use.
_NON_TEXT_TYPES = (PreformattedString, Script, Stylesheet, TemplateString)

# lxml is ~5-10x faster than html.parser on big chapters and is the reference
# parser for reproducible counts; fall back only if it is missing.
try:
    import lxml  # noqa: F401

    HTML_PARSER = "lxml"
except Exception:  # pragma: no cover
    HTML_PARSER = "html.parser"
    logging.getLogger("epubbot").warning(
        "lxml not installed — falling back to html.parser (slower). Install lxml for best results."
    )


class EpubTranslator:
    """Translates every text node of every XHTML doc inside the EPUB,
    keeping tags, attributes, CSS, images and package structure intact.

    All CPU-heavy work (unzip, HTML parse, serialise, re-zip) runs in a thread
    so the event loop stays free to drive dozens of concurrent HTTP requests."""

    def __init__(self, translator: Translator):
        self.tr = translator
        self.segments = 0
        self.chars = 0

    @staticmethod
    def _is_doc(name: str) -> bool:
        low = name.lower()
        return low.endswith(DOC_EXT) and not low.endswith(("container.xml", ".opf", ".ncx")) and "meta-inf/" not in low

    @staticmethod
    def _collect(soup: BeautifulSoup) -> List[NavigableString]:
        """Return only *human-readable* text nodes, in document order.

        Deterministic across parsers: the XML prolog, DOCTYPE, comments, CDATA,
        <script>/<style> bodies and anything under SKIP_TAGS are excluded, so the
        same EPUB always yields the same segment and character counts."""
        nodes: List[NavigableString] = []
        for node in soup.find_all(string=True):
            if isinstance(node, _NON_TEXT_TYPES):
                continue
            text = str(node)
            if not text.strip():
                continue
            if not any(ch.isalpha() for ch in text):  # numbers / punctuation only
                continue
            if any(p.name in SKIP_TAGS for p in node.parents if p.name):
                continue
            nodes.append(node)
        return nodes

    # ── blocking helpers (run via asyncio.to_thread) ───────────────────────────────────────

    def _parse_all(self, src: Path):
        with zipfile.ZipFile(src) as zin:
            names = zin.namelist()
            docs = [n for n in names if self._is_doc(n)]
            if not docs:
                raise TranslationError("No readable chapters found in EPUB")
            parsed: List[Tuple[str, BeautifulSoup, List[NavigableString]]] = []
            all_texts: List[str] = []
            for n in docs:
                raw = zin.read(n)
                soup = BeautifulSoup(raw, HTML_PARSER)
                nodes = self._collect(soup)
                parsed.append((n, soup, nodes))
                for node in nodes:
                    m = _ws_re.match(str(node))
                    all_texts.append(m.group(2) if m else str(node))

            ncx_docs: List[Tuple[str, BeautifulSoup, list]] = []
            for n in names:
                if n.lower().endswith(".ncx"):
                    try:
                        ncx = BeautifulSoup(zin.read(n), "xml")
                        labels = [t for t in ncx.find_all("text") if t.string and t.string.strip()]
                        if labels:
                            ncx_docs.append((n, ncx, labels))
                    except Exception as e:
                        log.debug("ncx skip: %s", e)
        return names, parsed, all_texts, ncx_docs

    @staticmethod
    def _apply_and_write(src: Path, dst: Path, names, parsed, translated, ncx_docs, ncx_out) -> None:
        i = 0
        new_content: Dict[str, bytes] = {}
        for n, soup, nodes in parsed:
            for node in nodes:
                m = _ws_re.match(str(node))
                lead, _, trail = (m.group(1), m.group(2), m.group(3)) if m else ("", "", "")
                node.replace_with(NavigableString(f"{lead}{translated[i]}{trail}"))
                i += 1
            new_content[n] = str(soup).encode("utf-8")

        j = 0
        for n, ncx, labels in ncx_docs:
            for t in labels:
                t.string = ncx_out[j]
                j += 1
            new_content[n] = str(ncx).encode("utf-8")

        # rebuild zip – mimetype MUST be first and stored
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
            if "mimetype" in names:
                zout.writestr("mimetype", zin.read("mimetype"), compress_type=zipfile.ZIP_STORED)
            for info in zin.infolist():
                if info.filename == "mimetype":
                    continue
                data = new_content.get(info.filename, zin.read(info.filename))
                zout.writestr(info.filename, data, compress_type=zipfile.ZIP_DEFLATED)

    # ── public ───────────────────────────────────────────────────────────────────────

    async def translate(self, src: Path, dst: Path, progress=None) -> None:
        # 1) parse (thread)
        names, parsed, all_texts, ncx_docs = await asyncio.to_thread(self._parse_all, src)
        self.segments = len(all_texts)
        self.chars = sum(len(t) for t in all_texts)
        if self.segments == 0:
            raise TranslationError("EPUB contains no translatable text")

        # 2) translate everything in one pooled pass (chapters + TOC labels together)
        ncx_texts = [t.string.strip() for _, _, labels in ncx_docs for t in labels]
        out = await self.tr.translate_many(all_texts + ncx_texts, progress)
        translated, ncx_out = out[: len(all_texts)], out[len(all_texts):]

        # 3) write back + re-zip (thread)
        await asyncio.to_thread(self._apply_and_write, src, dst, names, parsed, translated, ncx_docs, ncx_out)

# ═══════════════════════════════════════════════════════════════════════════
#  JOB QUEUE
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(order=True)
class Job:
    priority: int
    created: float
    id: int = field(compare=False)
    user_id: int = field(compare=False)
    chat_id: int = field(compare=False)
    file_path: Path = field(compare=False)
    file_name: str = field(compare=False)
    lang: str = field(compare=False)
    status_msg: Message = field(compare=False)
    cancelled: bool = field(default=False, compare=False)
    task: Optional[asyncio.Task] = field(default=None, compare=False)


class JobQueue:
    def __init__(self) -> None:
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.jobs: Dict[int, Job] = {}       # job_id -> Job (queued or running)
        self.by_user: Dict[int, int] = {}    # user_id -> job_id
        self.running: Dict[int, Job] = {}
        self.session: Optional[aiohttp.ClientSession] = None

    def user_has_job(self, uid: int) -> bool:
        return uid in self.by_user

    def position(self, job_id: int) -> int:
        queued = sorted((j for j in self.jobs.values() if j.id not in self.running), key=lambda j: (j.priority, j.created))
        for i, j in enumerate(queued, 1):
            if j.id == job_id:
                return i
        return 0

    async def submit(self, job: Job) -> None:
        self.jobs[job.id] = job
        self.by_user[job.user_id] = job.id
        await self.queue.put(job)

    def cancel(self, job_id: int) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job.cancelled = True
        if job.task and not job.task.done():
            job.task.cancel()
        return True

    def clear_stuck(self) -> int:
        n = len(self.running)
        for job in list(self.running.values()):
            self.cancel(job.id)
        return n

    def _cleanup(self, job: Job) -> None:
        self.jobs.pop(job.id, None)
        self.running.pop(job.id, None)
        if self.by_user.get(job.user_id) == job.id:
            self.by_user.pop(job.user_id, None)
        try:
            if job.file_path.parent.name.startswith("epub_"):
                shutil.rmtree(job.file_path.parent, ignore_errors=True)
            else:
                job.file_path.unlink(missing_ok=True)
        except Exception:
            pass

    async def worker_loop(self, app: Client, n: int) -> None:
        log.info("Job worker #%d started", n)
        while True:
            job: Job = await self.queue.get()
            if job.cancelled:
                self._cleanup(job)
                self.queue.task_done()
                continue
            self.running[job.id] = job
            job.task = asyncio.create_task(self._process(app, job))
            try:
                await job.task
            except asyncio.CancelledError:
                if not job.cancelled:
                    raise  # the worker loop itself is being shut down
                db.finish_job(job.id, "cancelled")
                await safe_edit(job.status_msg, "🚫 <b>Translation cancelled.</b>")
            except Exception as e:  # noqa: BLE001
                log.exception("job %d failed", job.id)
                db.finish_job(job.id, "failed")
                await safe_edit(job.status_msg, f"❌ <b>Translation failed</b>\n<code>{html.escape(str(e)[:300])}</code>")
            finally:
                self._cleanup(job)
                self.queue.task_done()

    async def _process(self, app: Client, job: Job) -> None:
        assert self.session is not None
        t0 = time.monotonic()
        last_edit = 0.0
        translator = Translator(self.session, job.lang)
        epub = EpubTranslator(translator)
        out_path = job.file_path.with_name(f"{Path(job.file_name).stem} [{job.lang}].epub")

        async def progress(done: int, total: int) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if now - last_edit < 3 and done != total:
                return
            last_edit = now
            pct = int(done * 100 / max(total, 1))
            eta = speed = ""
            elapsed_now = now - t0
            if done and elapsed_now > 0:
                remaining = elapsed_now / done * (total - done)
                eta = f" · ETA {int(remaining)}s"
                speed = f" · {int(epub.chars * done / total / elapsed_now / 1000)}k ch/s"
            endpoints = len(pool.available()) + (1 if Config.DIRECT_CONCURRENCY > 0 else 0)
            await safe_edit(
                job.status_msg,
                f"⚙️ <b>Translating…</b> {progress_bar(pct)} {pct}%\n"
                f"📄 {html.escape(job.file_name)}\n"
                f"🌐 → {lang_name(job.lang)} · {epub.segments:,} segments · {endpoints} endpoint(s){speed}{eta}",
                cancel_kb(job.id),
            )

        await safe_edit(job.status_msg, f"🔍 <b>Analysing EPUB…</b>\n📄 {html.escape(job.file_name)}", cancel_kb(job.id))
        await epub.translate(job.file_path, out_path, progress)

        elapsed = time.monotonic() - t0
        await safe_edit(job.status_msg, "📤 <b>Uploading translated book…</b>")
        caption = (
            f"✅ <b>Translation complete</b>\n"
            f"📄 {html.escape(job.file_name)}\n"
            f"🌐 {lang_name(job.lang)} · {epub.segments:,} segments · {epub.chars:,} chars\n"
            f"⏱ {int(elapsed // 60)}m {int(elapsed % 60)}s · {int(epub.chars / max(elapsed, 1) / 1000)}k chars/s"
        )
        await app.send_document(job.chat_id, str(out_path), caption=caption, file_name=out_path.name)
        try:
            await job.status_msg.delete()
        except Exception:
            pass
        out_path.unlink(missing_ok=True)
        db.finish_job(job.id, "done", epub.segments, epub.chars, elapsed)
        db.record_usage(job.user_id)
        log.info("job %d done: user=%d file=%s %.1fs", job.id, job.user_id, job.file_name, elapsed)


jobs = JobQueue()

# ═══════════════════════════════════════════════════════════════════════════
#  PAYMENTS (Razorpay)
# ═══════════════════════════════════════════════════════════════════════════


class Payments:
    def __init__(self) -> None:
        self.client = None
        if razorpay and Config.RAZORPAY_KEY_ID and Config.RAZORPAY_KEY_SECRET:
            self.client = razorpay.Client(auth=(Config.RAZORPAY_KEY_ID, Config.RAZORPAY_KEY_SECRET))

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def create_link(self, uid: int) -> Tuple[str, str]:
        amount = Config.PREMIUM_PRICE_INR * 100
        data = {
            "amount": amount,
            "currency": "INR",
            "accept_partial": False,
            "description": f"EPUB Translator Premium ({Config.PREMIUM_DAYS} days)",
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {"user_id": str(uid)},
        }
        link = await asyncio.to_thread(self.client.payment_link.create, data)
        db.add_payment(link["id"], uid, Config.PREMIUM_PRICE_INR, Config.PREMIUM_DAYS)
        return link["id"], link["short_url"]

    async def verify(self, link_id: str) -> bool:
        try:
            link = await asyncio.to_thread(self.client.payment_link.fetch, link_id)
            return link.get("status") == "paid"
        except Exception as e:
            log.warning("razorpay fetch failed: %s", e)
            return False


payments = Payments()

# ═══════════════════════════════════════════════════════════════════════════
#  UI HELPERS
# ═══════════════════════════════════════════════════════════════════════════

BTN_LANG = "🌐 Language"
BTN_PREMIUM = "⭐ Premium"
BTN_STATUS = "📊 Status"
BTN_HELP = "❓ Help"
BTN_ADMIN = "🛠 Admin"


def main_kb(uid: int) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(BTN_LANG), KeyboardButton(BTN_PREMIUM)], [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_HELP)]]
    if Config.is_admin(uid):
        rows.append([KeyboardButton(BTN_ADMIN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def lang_kb(current: str) -> InlineKeyboardMarkup:
    btns = [InlineKeyboardButton(("✅ " if c == current else "") + n, callback_data=f"lang:{c}") for c, n in LANGUAGES.items()]
    return InlineKeyboardMarkup([btns[i:i + 2] for i in range(0, len(btns), 2)])


def cancel_kb(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Cancel", callback_data=f"cancel:{job_id}")]])


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🖥 Workers", callback_data="adm:workers"), InlineKeyboardButton("📈 Stats", callback_data="adm:stats")],
            [InlineKeyboardButton("📋 Queue", callback_data="adm:queue"), InlineKeyboardButton("🧹 Clear stuck", callback_data="adm:clear")],
            [InlineKeyboardButton("📣 Broadcast", callback_data="adm:bcast"), InlineKeyboardButton("🔄 Health check", callback_data="adm:health")],
            [InlineKeyboardButton("✖ Close", callback_data="adm:close")],
        ]
    )


def workers_kb() -> InlineKeyboardMarkup:
    rows = []
    for i, w in enumerate(pool.workers.values(), 1):
        rows.append(
            [
                InlineKeyboardButton(f"{'⏸' if w.enabled else '▶'} #{i}", callback_data=f"wrk:toggle:{i-1}"),
                InlineKeyboardButton(f"🗑 #{i}", callback_data=f"wrk:del:{i-1}"),
            ]
        )
    rows.append([InlineKeyboardButton("➕ Add worker", callback_data="wrk:add"), InlineKeyboardButton("« Back", callback_data="adm:menu")])
    return InlineKeyboardMarkup(rows)


def progress_bar(pct: int, width: int = 12) -> str:
    filled = int(width * pct / 100)
    return "▰" * filled + "▱" * (width - filled)


def fmt_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y") if ts else "—"


async def safe_edit(msg: Message, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await msg.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception as e:  # noqa: BLE001
        log.debug("edit failed: %s", e)


def user_line(row: sqlite3.Row) -> str:
    uid = row["id"]
    if Config.is_admin(uid):
        plan = "👑 Admin"
    elif row["premium_until"] > time.time():
        plan = f"⭐ Premium till {fmt_dt(row['premium_until'])}"
    else:
        plan = f"🆓 Free · {Config.FREE_DAILY_LIMIT - db.daily_used(uid)}/{Config.FREE_DAILY_LIMIT} left today"
    return plan


# per-admin pending input (e.g. waiting for worker URL / broadcast text)
pending_input: Dict[int, str] = {}

# ═══════════════════════════════════════════════════════════════════════════
#  BOT
# ═══════════════════════════════════════════════════════════════════════════

app = Client(
    Config.SESSION_NAME,
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    workdir=str(Config.DATA_DIR),
    parse_mode=ParseMode.HTML,
)


async def check_force_sub(client: Client, uid: int) -> bool:
    if not Config.FORCE_SUB_CHANNEL or Config.is_admin(uid):
        return True
    try:
        m = await client.get_chat_member(Config.FORCE_SUB_CHANNEL, uid)
        return m.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
    except UserNotParticipant:
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("force-sub check failed: %s", e)
        return True


async def guard(client: Client, message: Message) -> Optional[sqlite3.Row]:
    """Common per-message checks. Returns user row or None if blocked."""
    u = message.from_user
    if not u:
        return None
    row = db.upsert_user(u.id, u.first_name or "", u.username)
    if row["banned"]:
        await message.reply_text("🚫 You are banned from using this bot.")
        return None
    if not await check_force_sub(client, u.id):
        ch = Config.FORCE_SUB_CHANNEL
        link = f"https://t.me/{ch.lstrip('@')}" if ch.startswith("@") else None
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Join channel", url=link)]]) if link else None
        await message.reply_text("📢 Please join our channel first, then send /start again.", reply_markup=kb)
        return None
    return row


# ── /start ─────────────────────────────────────────────────────────────────
@app.on_message(filters.private & filters.command("start"))
async def cmd_start(client: Client, message: Message) -> None:
    row = await guard(client, message)
    if not row:
        return
    await message.reply_text(
        f"👋 <b>Welcome, {html.escape(message.from_user.first_name or 'there')}!</b>\n\n"
        "I translate <b>EPUB books</b> into your language while keeping the original "
        "formatting, images and chapters intact.\n\n"
        f"🌐 Target language: <b>{lang_name(row['lang'])}</b>\n"
        f"💼 Plan: {user_line(row)}\n\n"
        "📎 <b>Just send me an .epub file to begin.</b>",
        reply_markup=main_kb(message.from_user.id),
    )


# ── /help ──────────────────────────────────────────────────────────────────
@app.on_message(filters.private & (filters.command("help") | filters.regex(f"^{re.escape(BTN_HELP)}$")))
async def cmd_help(client: Client, message: Message) -> None:
    if not await guard(client, message):
        return
    await message.reply_text(
        "📖 <b>How it works</b>\n"
        "1. Choose your language with <b>🌐 Language</b>\n"
        "2. Send an <b>.epub</b> file\n"
        "3. Watch live progress, get the translated book back\n\n"
        "✨ <b>What's preserved</b>: chapters, bold/italic, links, images, table of contents, CSS.\n\n"
        f"🆓 Free: {Config.FREE_DAILY_LIMIT} files/day, up to {Config.FREE_MAX_FILE_MB} MB\n"
        f"⭐ Premium: unlimited, priority queue, up to {Config.PREMIUM_MAX_FILE_MB} MB\n\n"
        "<b>Commands</b>\n"
        "/start · /help · /lang · /status · /premium · /cancel\n\n"
        f"💬 Support: {html.escape(Config.SUPPORT_CONTACT)}",
        disable_web_page_preview=True,
    )


# ── language ───────────────────────────────────────────────────────────────
@app.on_message(filters.private & (filters.command("lang") | filters.regex(f"^{re.escape(BTN_LANG)}$")))
async def cmd_lang(client: Client, message: Message) -> None:
    row = await guard(client, message)
    if not row:
        return
    await message.reply_text("🌐 <b>Choose target language</b>", reply_markup=lang_kb(row["lang"]))


@app.on_callback_query(filters.regex(r"^lang:(.+)$"))
async def cb_lang(client: Client, cq: CallbackQuery) -> None:
    code = cq.matches[0].group(1)
    if code not in LANGUAGES:
        return await cq.answer("Unknown language", show_alert=True)
    db.upsert_user(cq.from_user.id, cq.from_user.first_name or "", cq.from_user.username)
    db.set_lang(cq.from_user.id, code)
    await cq.answer(f"Language set: {lang_name(code)}")
    await safe_edit(cq.message, f"🌐 Target language: <b>{lang_name(code)}</b>\n\n📎 Now send me an .epub file.")


# ── status ─────────────────────────────────────────────────────────────────
@app.on_message(filters.private & (filters.command("status") | filters.regex(f"^{re.escape(BTN_STATUS)}$")))
async def cmd_status(client: Client, message: Message) -> None:
    row = await guard(client, message)
    if not row:
        return
    uid = message.from_user.id
    mine = ""
    if uid in jobs.by_user:
        jid = jobs.by_user[uid]
        pos = jobs.position(jid)
        mine = "\n\n📌 <b>Your file</b>: " + ("processing now" if jid in jobs.running else f"queue position #{pos}")
    await message.reply_text(
        "📊 <b>Status</b>\n"
        f"🌐 Language: <b>{lang_name(row['lang'])}</b>\n"
        f"💼 Plan: {user_line(row)}\n"
        f"📚 Files translated: {row['total_files']}\n\n"
        f"🖥 Workers online: {len(pool.available())}/{len(pool.workers)}\n"
        f"⚙️ Processing: {len(jobs.running)} · Queued: {jobs.queue.qsize()}" + mine
    )


# ── premium ────────────────────────────────────────────────────────────────
@app.on_message(filters.private & (filters.command(["premium", "pay"]) | filters.regex(f"^{re.escape(BTN_PREMIUM)}$")))
async def cmd_premium(client: Client, message: Message) -> None:
    row = await guard(client, message)
    if not row:
        return
    uid = message.from_user.id
    if db.is_premium(uid):
        return await message.reply_text(f"⭐ You already have Premium.\n{user_line(row)}")
    if not payments.enabled:
        return await message.reply_text(f"⭐ <b>Premium</b> — ₹{Config.PREMIUM_PRICE_INR}/{Config.PREMIUM_DAYS} days\n\nContact {html.escape(Config.SUPPORT_CONTACT)} to upgrade.")
    try:
        link_id, url = await payments.create_link(uid)
    except Exception as e:  # noqa: BLE001
        log.error("payment link error: %s", e)
        return await message.reply_text("⚠️ Payment service temporarily unavailable. Please try later.")
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"💳 Pay ₹{Config.PREMIUM_PRICE_INR}", url=url)],
            [InlineKeyboardButton("✅ I've paid — verify", callback_data=f"pay:{link_id}")],
        ]
    )
    await message.reply_text(
        f"⭐ <b>Premium — {Config.PREMIUM_DAYS} days</b>\n\n"
        "• Unlimited translations\n• Priority queue\n"
        f"• Files up to {Config.PREMIUM_MAX_FILE_MB} MB\n\n"
        "Pay via the button, then tap <b>verify</b>. Activation is instant.",
        reply_markup=kb,
    )


@app.on_callback_query(filters.regex(r"^pay:(.+)$"))
async def cb_pay(client: Client, cq: CallbackQuery) -> None:
    link_id = cq.matches[0].group(1)
    p = db.get_payment(link_id)
    if not p or p["user_id"] != cq.from_user.id:
        return await cq.answer("Payment not found.", show_alert=True)
    if p["status"] == "paid":
        return await cq.answer("Already activated ✅", show_alert=True)
    await cq.answer("Checking payment…")
    if await payments.verify(link_id):
        db.mark_paid(link_id)
        until = db.add_premium(p["user_id"], p["days"])
        await safe_edit(cq.message, f"🎉 <b>Premium activated!</b>\nValid till <b>{fmt_dt(until)}</b>. Enjoy unlimited translations.")
        if Config.OWNER_ID:
            try:
                await client.send_message(Config.OWNER_ID, f"💰 New payment ₹{p['amount']} from <code>{p['user_id']}</code> (@{cq.from_user.username or '-'})")
            except Exception:
                pass
    else:
        await cq.answer("Payment not received yet. Complete the payment and try again in a minute.", show_alert=True)


# ── cancel ─────────────────────────────────────────────────────────────────
@app.on_message(filters.private & filters.command("cancel"))
async def cmd_cancel(client: Client, message: Message) -> None:
    uid = message.from_user.id
    jid = jobs.by_user.get(uid)
    if jid and jobs.cancel(jid):
        await message.reply_text("🚫 Your translation has been cancelled.")
    else:
        await message.reply_text("You have no active translation.")


@app.on_callback_query(filters.regex(r"^cancel:(\d+)$"))
async def cb_cancel(client: Client, cq: CallbackQuery) -> None:
    jid = int(cq.matches[0].group(1))
    job = jobs.jobs.get(jid)
    if not job:
        return await cq.answer("Job already finished.", show_alert=True)
    if job.user_id != cq.from_user.id and not Config.is_admin(cq.from_user.id):
        return await cq.answer("Not your job.", show_alert=True)
    jobs.cancel(jid)
    await cq.answer("Cancelling…")
    if jid not in jobs.running:
        await safe_edit(cq.message, "🚫 <b>Translation cancelled.</b>")


# ── documents ──────────────────────────────────────────────────────────────
@app.on_message(filters.private & filters.document)
async def on_document(client: Client, message: Message) -> None:
    row = await guard(client, message)
    if not row:
        return
    uid = message.from_user.id
    if uid in pending_input:  # admin is sending a broadcast attachment
        return
    doc = message.document
    name = doc.file_name or "book.epub"
    if not name.lower().endswith(".epub") and doc.mime_type != "application/epub+zip":
        return await message.reply_text("⚠️ Only <b>.epub</b> files are supported.")

    premium = db.is_premium(uid)
    max_mb = Config.PREMIUM_MAX_FILE_MB if premium else Config.FREE_MAX_FILE_MB
    if doc.file_size > max_mb * 1024 * 1024:
        return await message.reply_text(
            f"⚠️ File too large ({doc.file_size / 1048576:.1f} MB). Limit for your plan: <b>{max_mb} MB</b>."
            + ("" if premium else "\n⭐ Upgrade with /premium for bigger files.")
        )
    if not premium and db.daily_used(uid) >= Config.FREE_DAILY_LIMIT:
        return await message.reply_text(
            f"⏳ Daily free limit reached ({Config.FREE_DAILY_LIMIT} files).\n⭐ Upgrade with /premium for unlimited access."
        )
    if jobs.user_has_job(uid):
        return await message.reply_text("⚠️ You already have a file in progress. Use /cancel to stop it first.")
    if not pool.available() and not Config.DIRECT_FALLBACK:
        return await message.reply_text("⚠️ Translation service is offline right now. Please try again later.")

    status = await message.reply_text("📥 <b>Downloading…</b>")
    tmp_dir = Path(tempfile.mkdtemp(prefix="epub_", dir=Config.DATA_DIR))
    path = await message.download(file_name=str(tmp_dir / re.sub(r"[^\w.\- ]", "_", name)))
    if not path or not zipfile.is_zipfile(path):
        await safe_edit(status, "❌ This file is not a valid EPUB.")
        return

    job_id = db.add_job(uid, name, row["lang"])
    job = Job(
        priority=0 if premium else 1,
        created=time.time(),
        id=job_id,
        user_id=uid,
        chat_id=message.chat.id,
        file_path=Path(path),
        file_name=name,
        lang=row["lang"],
        status_msg=status,
    )
    await jobs.submit(job)
    pos = jobs.position(job_id)
    await safe_edit(
        status,
        f"✅ <b>Queued</b> · position #{pos}\n📄 {html.escape(name)}\n🌐 → {lang_name(row['lang'])}"
        + ("" if premium else "\n\n⭐ Premium users skip the queue — /premium"),
        cancel_kb(job_id),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  ADMIN
# ═══════════════════════════════════════════════════════════════════════════

admin_filter = filters.private & filters.create(lambda _, __, m: bool(m.from_user and Config.is_admin(m.from_user.id)))


def admin_text() -> str:
    s = db.stats()
    return (
        "🛠 <b>Admin panel</b>\n\n"
        f"👥 Users: {s['users']} (⭐ {s['premium']} · 🚫 {s['banned']})\n"
        f"🖥 Workers: {len(pool.available())}/{len(pool.workers)} online\n"
        f"⚙️ Processing: {len(jobs.running)} · Queued: {jobs.queue.qsize()}\n\n"
        "<b>Commands</b>\n"
        "<code>/addworker URL</code> · <code>/delworker URL</code>\n"
        "<code>/addpremium USER_ID [days]</code> · <code>/revoke USER_ID</code>\n"
        "<code>/ban USER_ID</code> · <code>/unban USER_ID</code> · <code>/user USER_ID</code>\n"
        "<code>/broadcast TEXT</code> (or reply to a message)"
    )


@app.on_message(admin_filter & (filters.command("admin") | filters.regex(f"^{re.escape(BTN_ADMIN)}$")))
async def cmd_admin(client: Client, message: Message) -> None:
    await message.reply_text(admin_text(), reply_markup=admin_kb())


@app.on_callback_query(filters.regex(r"^adm:(\w+)$"))
async def cb_admin(client: Client, cq: CallbackQuery) -> None:
    if not Config.is_admin(cq.from_user.id):
        return await cq.answer("Admins only.", show_alert=True)
    action = cq.matches[0].group(1)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="adm:menu")]])
    toast, alert = "", False
    if action == "menu":
        await safe_edit(cq.message, admin_text(), admin_kb())
    elif action == "close":
        await cq.message.delete()
    elif action == "workers":
        await safe_edit(cq.message, "🖥 <b>Workers</b>\n\n" + pool.summary(), workers_kb())
    elif action == "health":
        toast = "Workers pinged"
        if jobs.session:
            await pool.health_check(jobs.session)
        await safe_edit(cq.message, "🖥 <b>Workers</b> (fresh check)\n\n" + pool.summary(), workers_kb())
    elif action == "stats":
        s = db.stats()
        await safe_edit(
            cq.message,
            "📈 <b>Statistics</b>\n\n"
            f"👥 Users: {s['users']} · new 24h: {s['today_users']}\n"
            f"⭐ Premium: {s['premium']} · 🚫 Banned: {s['banned']}\n\n"
            f"📚 Jobs: {s['jobs_total']} · ✅ {s['jobs_done']} · ❌ {s['jobs_failed']}\n"
            f"🔤 Characters translated: {s['chars']:,}\n\n"
            f"💰 Payments: {s['payments']} · Revenue: ₹{s['revenue']:,}",
            back,
        )
    elif action == "queue":
        lines = []
        for j in sorted(jobs.jobs.values(), key=lambda j: (j.id not in jobs.running, j.priority, j.created)):
            state = "⚙️" if j.id in jobs.running else "⏳"
            lines.append(f"{state} #{j.id} · <code>{j.user_id}</code> · {html.escape(j.file_name[:30])} → {j.lang}")
        await safe_edit(cq.message, "📋 <b>Queue</b>\n\n" + ("\n".join(lines) or "Empty."), back)
    elif action == "clear":
        n = jobs.clear_stuck()
        toast, alert = f"Cancelled {n} running job(s).", True
        await safe_edit(cq.message, admin_text(), admin_kb())
    elif action == "bcast":
        pending_input[cq.from_user.id] = "broadcast"
        await safe_edit(cq.message, "📣 Send the broadcast message now (text/photo). Send /cancel_input to abort.", back)
    try:
        await cq.answer(toast, show_alert=alert)
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^wrk:(\w+)(?::(\d+))?$"))
async def cb_workers(client: Client, cq: CallbackQuery) -> None:
    if not Config.is_admin(cq.from_user.id):
        return await cq.answer("Admins only.", show_alert=True)
    action, idx = cq.matches[0].group(1), cq.matches[0].group(2)
    urls = list(pool.workers)
    if action == "add":
        pending_input[cq.from_user.id] = "add_worker"
        await safe_edit(cq.message, "➕ Send the worker URL (e.g. <code>https://xyz.onrender.com</code>).\nSend /cancel_input to abort.")
        return await cq.answer()
    if idx is None or int(idx) >= len(urls):
        return await cq.answer("Worker not found.", show_alert=True)
    url = urls[int(idx)]
    if action == "toggle":
        pool.toggle(url)
    elif action == "del":
        pool.remove(url)
    await cq.answer("Done")
    await safe_edit(cq.message, "🖥 <b>Workers</b>\n\n" + pool.summary(), workers_kb())


@app.on_message(admin_filter & filters.command("addworker"))
async def cmd_addworker(client: Client, message: Message) -> None:
    if len(message.command) < 2:
        return await message.reply_text("Usage: <code>/addworker https://xyz.onrender.com</code>")
    added = [u for u in message.command[1:] if pool.add(u)]
    await message.reply_text(f"✅ Added {len(added)} worker(s).\n\n" + pool.summary())


@app.on_message(admin_filter & filters.command("delworker"))
async def cmd_delworker(client: Client, message: Message) -> None:
    if len(message.command) < 2:
        return await message.reply_text("Usage: <code>/delworker URL</code>")
    ok = pool.remove(pool.normalize(message.command[1]))
    await message.reply_text("✅ Removed." if ok else "⚠️ Not found.")


@app.on_message(admin_filter & filters.command("addpremium"))
async def cmd_addpremium(client: Client, message: Message) -> None:
    try:
        uid = int(message.command[1])
        days = int(message.command[2]) if len(message.command) > 2 else Config.PREMIUM_DAYS
    except (IndexError, ValueError):
        return await message.reply_text("Usage: <code>/addpremium USER_ID [days]</code>")
    until = db.add_premium(uid, days)
    await message.reply_text(f"⭐ Premium for <code>{uid}</code> till {fmt_dt(until)}.")
    try:
        await client.send_message(uid, f"🎉 <b>Premium activated!</b> Valid till <b>{fmt_dt(until)}</b>.")
    except Exception:
        pass


@app.on_message(admin_filter & filters.command("revoke"))
async def cmd_revoke(client: Client, message: Message) -> None:
    try:
        uid = int(message.command[1])
    except (IndexError, ValueError):
        return await message.reply_text("Usage: <code>/revoke USER_ID</code>")
    db.revoke_premium(uid)
    await message.reply_text(f"Premium revoked for <code>{uid}</code>.")


@app.on_message(admin_filter & filters.command(["ban", "unban"]))
async def cmd_ban(client: Client, message: Message) -> None:
    try:
        uid = int(message.command[1])
    except (IndexError, ValueError):
        return await message.reply_text(f"Usage: <code>/{message.command[0]} USER_ID</code>")
    if uid == Config.OWNER_ID:
        return await message.reply_text("Cannot ban the owner.")
    ban = message.command[0] == "ban"
    if db.get_user(uid) is None:
        db.upsert_user(uid, "", None)
    db.set_banned(uid, ban)
    if ban and uid in jobs.by_user:
        jobs.cancel(jobs.by_user[uid])
    await message.reply_text(f"{'🚫 Banned' if ban else '✅ Unbanned'} <code>{uid}</code>.")


@app.on_message(admin_filter & filters.command("user"))
async def cmd_user(client: Client, message: Message) -> None:
    try:
        uid = int(message.command[1])
    except (IndexError, ValueError):
        return await message.reply_text("Usage: <code>/user USER_ID</code>")
    row = db.get_user(uid)
    if not row:
        return await message.reply_text("User not found.")
    await message.reply_text(
        f"👤 <b>{html.escape(row['name'] or '-')}</b> @{row['username'] or '-'} · <code>{uid}</code>\n"
        f"🌐 {lang_name(row['lang'])} · 📚 {row['total_files']} files\n"
        f"💼 {user_line(row)}\n"
        f"🚫 Banned: {'yes' if row['banned'] else 'no'} · joined {fmt_dt(row['joined'])}"
    )


async def do_broadcast(client: Client, src: Message, status: Message) -> None:
    ids = db.all_user_ids()
    ok = fail = 0
    for i, uid in enumerate(ids, 1):
        try:
            await src.copy(uid)
            ok += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await src.copy(uid)
                ok += 1
            except Exception:
                fail += 1
        except Exception:
            fail += 1
        if i % 25 == 0:
            await safe_edit(status, f"📣 Broadcasting… {i}/{len(ids)}")
        await asyncio.sleep(0.05)
    await safe_edit(status, f"📣 <b>Broadcast done</b>\n✅ {ok} · ❌ {fail}")


@app.on_message(admin_filter & filters.command("broadcast"))
async def cmd_broadcast(client: Client, message: Message) -> None:
    src = message.reply_to_message
    if not src:
        text = message.text.split(None, 1)
        if len(text) < 2:
            return await message.reply_text("Reply to a message with /broadcast, or <code>/broadcast TEXT</code>.")
        src = await message.reply_text(text[1])
    status = await message.reply_text("📣 Broadcasting…")
    asyncio.create_task(do_broadcast(client, src, status))


@app.on_message(admin_filter & filters.command("cancel_input"))
async def cmd_cancel_input(client: Client, message: Message) -> None:
    pending_input.pop(message.from_user.id, None)
    await message.reply_text("Input cancelled.")


# admin pending-input consumer (must be registered after commands; group=1)
@app.on_message(admin_filter & ~filters.command(["start", "help", "lang", "status", "premium", "pay", "cancel", "admin", "cancel_input"]), group=1)
async def on_admin_input(client: Client, message: Message) -> None:
    mode = pending_input.get(message.from_user.id)
    if not mode:
        return
    if message.text and message.text in (BTN_LANG, BTN_PREMIUM, BTN_STATUS, BTN_HELP, BTN_ADMIN):
        return
    pending_input.pop(message.from_user.id, None)
    if mode == "add_worker" and message.text:
        added = [u for u in message.text.split() if pool.add(u)]
        await message.reply_text(f"✅ Added {len(added)} worker(s).\n\n" + pool.summary(), reply_markup=workers_kb())
    elif mode == "broadcast":
        status = await message.reply_text("📣 Broadcasting…")
        asyncio.create_task(do_broadcast(client, message, status))
    message.stop_propagation()


# ── fallback for random text ───────────────────────────────────────────────
@app.on_message(filters.private & filters.text & ~filters.command(["start", "help", "lang", "status", "premium", "pay", "cancel"]), group=2)
async def on_text(client: Client, message: Message) -> None:
    if message.text in (BTN_LANG, BTN_PREMIUM, BTN_STATUS, BTN_HELP, BTN_ADMIN) or message.text.startswith("/"):
        return
    if message.from_user and Config.is_admin(message.from_user.id) and message.from_user.id in pending_input:
        return
    await message.reply_text("📎 Send me an <b>.epub</b> file to translate, or use the menu below.", reply_markup=main_kb(message.from_user.id))


# ═══════════════════════════════════════════════════════════════════════════
#  BACKGROUND TASKS & ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════


async def keep_alive_loop() -> None:
    """Ping workers to keep Render free instances awake and refresh health."""
    await asyncio.sleep(5)
    while True:
        try:
            if jobs.session:
                await pool.health_check(jobs.session)
                log.info("health: %d/%d workers online", len(pool.available()), len(pool.workers))
        except Exception as e:  # noqa: BLE001
            log.warning("keep-alive error: %s", e)
        await asyncio.sleep(Config.WORKER_PING_INTERVAL)


async def main() -> None:
    await app.start()
    me = await app.get_me()
    # enough sockets for every job slot to run at full parallelism, keep-alive on
    connector = aiohttp.TCPConnector(
        limit=Config.MAX_PARALLEL_REQUESTS * Config.MAX_CONCURRENT_JOBS + 16,
        limit_per_host=0,
        ttl_dns_cache=300,
        keepalive_timeout=60,
    )
    jobs.session = aiohttp.ClientSession(connector=connector, headers={"User-Agent": "EpubTranslatorBot/2.0"})
    tasks = [asyncio.create_task(keep_alive_loop())]
    tasks += [asyncio.create_task(jobs.worker_loop(app, i + 1)) for i in range(Config.MAX_CONCURRENT_JOBS)]
    log.info("Bot @%s started · %d workers · %d job slots", me.username, len(pool.workers), Config.MAX_CONCURRENT_JOBS)
    if Config.OWNER_ID:
        try:
            await app.send_message(Config.OWNER_ID, f"🟢 Bot restarted · {len(pool.available())}/{len(pool.workers)} workers online")
        except Exception:
            pass
    await idle()
    for t in tasks:
        t.cancel()
    await jobs.session.close()
    await app.stop()
    log.info("Bot stopped")


if __name__ == "__main__":
    app.run(main())
