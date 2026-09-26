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

try:
    import pymupdf  # type: ignore  # PDF support (PyMuPDF)
except Exception:  # pragma: no cover
    pymupdf = None

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

    # absolute so Pyrogram's download_media (which resolves relative paths
    # against *its own* parent dir) and our temp files always agree
    DATA_DIR = Path(_env("DATA_DIR", "data")).expanduser().resolve()
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
        url = url.strip().strip("<>").rstrip("/")
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        return url

    @staticmethod
    def is_valid(url: str) -> bool:
        # scheme + a real host name (with a dot or a port) — rejects things like
        # "https:///addworker" that came from a stray command while waiting for input
        return bool(re.match(r"^https?://(?:[\w-]+\.)+[\w-]+(?::\d+)?(?:/[^\s]*)?$", url, re.I)) or bool(
            re.match(r"^https?://(?:localhost|\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?(?:/[^\s]*)?$", url, re.I)
        )

    def add(self, url: str) -> bool:
        url = self.normalize(url)
        if not self.is_valid(url):
            return False
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

    async def _ping(self, session: aiohttp.ClientSession, w: Worker) -> None:
        t0 = time.monotonic()
        try:
            # Render free instances need up to ~60 s to wake from sleep
            async with session.get(f"{w.url}/", timeout=aiohttp.ClientTimeout(total=75)) as r:
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
            # a failed ping means "down right now"; retry on the next request
            # after a short cool-down instead of waiting for N failed jobs
            w.fails += 1
            w.healthy = False
            w.disabled_until = time.time() + 60

    async def health_check(self, session: aiohttp.ClientSession) -> None:
        targets = [w for w in list(self.workers.values()) if w.enabled]
        if targets:
            await asyncio.gather(*(self._ping(session, w) for w in targets))

    def summary(self) -> str:
        if not self.workers and Config.DIRECT_CONCURRENCY <= 0:
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

    @staticmethod
    def _clean(out: List[str]) -> List[str]:
        # Google's gtx endpoint returns HTML-escaped text ("It&#39;s", "&quot;",
        # "&amp;").  If we insert that as-is, BeautifulSoup escapes the '&'
        # again and the reader shows a literal "&#39;".  Unescape once here so
        # the text node holds real characters and is serialised correctly.
        return [html.unescape(x) for x in out]

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
                    return self._clean(out)
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
                        return self._clean(out)
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
# Original `<?xml ...?>` prolog of a chapter (bytes, before parsing).
_XML_PROLOG_RE = re.compile(rb"^\s*<\?xml[^>]*\?>")
# What the prolog looks like after a round-trip through the parsers:
#   lxml         -> "<!--?xml version=... ?-->"  (turned into a comment — invalid XHTML!)
#   html.parser  -> "<?xml version=... ?>"        (kept as a processing instruction)
_OUT_PROLOG_RE = re.compile(r"^\s*(?:<!--\?xml[^>]*\?-->|<\?xml[^>]*\?>)\s*")
XML_PROLOG = '<?xml version="1.0" encoding="utf-8"?>\n'

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
        if low.endswith("/"):  # zip directory entry
            return False
        return low.endswith(DOC_EXT) and not low.endswith(("container.xml", ".opf", ".ncx")) and "meta-inf/" not in low

    @staticmethod
    def _looks_like_xhtml(name: str, raw: bytes) -> bool:
        """Generic *.xml files (page-map.xml, encryption.xml, nav.xml …) are only
        chapters if they actually contain an <html> root; otherwise running them
        through the HTML parser would corrupt them."""
        if not name.lower().endswith(".xml"):
            return True
        head = raw[:4096].lower()
        return b"<html" in head or b"1999/xhtml" in head

    @staticmethod
    def _serialize(soup: BeautifulSoup, raw: bytes) -> bytes:
        out = str(soup)
        if _XML_PROLOG_RE.match(raw):
            # drop whatever the parser made of the prolog and emit a clean one
            out = XML_PROLOG + _OUT_PROLOG_RE.sub("", out, count=1)
        return out.encode("utf-8")

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
            parsed: List[Tuple[str, BeautifulSoup, List[NavigableString], bytes]] = []
            all_texts: List[str] = []
            for n in docs:
                raw = zin.read(n)
                if not self._looks_like_xhtml(n, raw):
                    continue
                soup = BeautifulSoup(raw, HTML_PARSER)
                nodes = self._collect(soup)
                if not nodes:
                    continue  # nothing to translate → copy the file through untouched
                parsed.append((n, soup, nodes, raw))
                for node in nodes:
                    m = _ws_re.match(str(node))
                    all_texts.append(m.group(2) if m else str(node))

            ncx_docs: List[Tuple[str, BeautifulSoup, list]] = []
            for n in names:
                if n.lower().endswith(".ncx") and not n.endswith("/"):
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
        for n, soup, nodes, raw in parsed:
            for node in nodes:
                m = _ws_re.match(str(node))
                lead, _, trail = (m.group(1), m.group(2), m.group(3)) if m else ("", "", "")
                node.replace_with(NavigableString(f"{lead}{translated[i]}{trail}"))
                i += 1
            new_content[n] = EpubTranslator._serialize(soup, raw)

        j = 0
        for n, ncx, labels in ncx_docs:
            for t in labels:
                t.string = ncx_out[j]
                j += 1
            new_content[n] = str(ncx).encode("utf-8")

        # rebuild zip – mimetype MUST be first and stored
        seen: set = set()
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
            if "mimetype" in names:
                zout.writestr("mimetype", zin.read("mimetype"), compress_type=zipfile.ZIP_STORED)
                seen.add("mimetype")
            for info in zin.infolist():
                if info.filename in seen or info.filename.endswith("/"):
                    continue  # duplicates / directory entries
                seen.add(info.filename)
                data = new_content.get(info.filename)
                if data is None:
                    # untouched file: copy bytes 1:1 keeping the original metadata
                    zout.writestr(info, zin.read(info.filename))
                    continue
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = info.external_attr
                zout.writestr(zi, data)

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
#  OTHER FORMATS: TXT · HTML · DOCX · PDF  (same Translator, same progress API)
# ═══════════════════════════════════════════════════════════════════════════

# extension -> (kind, human label).  Input & output keep the same extension.
SUPPORTED_FORMATS: Dict[str, Tuple[str, str]] = {
    ".epub": ("epub", "EPUB"),
    ".txt": ("txt", "Text"),
    ".md": ("txt", "Markdown"),
    ".html": ("html", "HTML"),
    ".htm": ("html", "HTML"),
    ".xhtml": ("html", "XHTML"),
    ".docx": ("docx", "Word"),
    ".pdf": ("pdf", "PDF"),
}
MIME_TO_EXT = {
    "application/epub+zip": ".epub",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
    "application/xhtml+xml": ".xhtml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/pdf": ".pdf",
}


def detect_format(file_name: str, mime: Optional[str]) -> Optional[str]:
    """Return the canonical extension ('.epub', '.pdf', …) or None if unsupported."""
    ext = Path(file_name or "").suffix.lower()
    if ext in SUPPORTED_FORMATS:
        return ext
    return MIME_TO_EXT.get((mime or "").split(";")[0].strip().lower())


def _decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class BaseDocTranslator:
    def __init__(self, translator: Translator):
        self.tr = translator
        self.segments = 0
        self.chars = 0

    def _count(self, texts: List[str], what: str) -> None:
        self.segments = len(texts)
        self.chars = sum(len(t) for t in texts)
        if self.segments == 0:
            raise TranslationError(f"{what} contains no translatable text")

    async def translate(self, src: Path, dst: Path, progress=None) -> None:  # pragma: no cover
        raise NotImplementedError


class TxtTranslator(BaseDocTranslator):
    """Plain text / Markdown: translate paragraph by paragraph, keep blank lines,
    indentation and line endings exactly as they were."""

    _para_re = re.compile(r"([^\n]+)")

    async def translate(self, src: Path, dst: Path, progress=None) -> None:
        text = _decode_text(await asyncio.to_thread(src.read_bytes))
        nl = "\r\n" if "\r\n" in text else "\n"
        lines = text.split("\n")
        idx: List[int] = []
        texts: List[str] = []
        for i, line in enumerate(lines):
            core = line.strip("\r")
            m = _ws_re.match(core)
            body = m.group(2) if m else core
            if body and any(ch.isalpha() for ch in body):
                idx.append(i)
                texts.append(body)
        self._count(texts, "File")
        out = await self.tr.translate_many(texts, progress)
        for i, t in zip(idx, out):
            core = lines[i].strip("\r")
            m = _ws_re.match(core)
            lead, trail = (m.group(1), m.group(3)) if m else ("", "")
            lines[i] = f"{lead}{t}{trail}"
        await asyncio.to_thread(dst.write_bytes, nl.join(lines).encode("utf-8"))


class HtmlTranslator(BaseDocTranslator):
    """Stand-alone HTML/XHTML file: same text-node approach as EPUB chapters."""

    async def translate(self, src: Path, dst: Path, progress=None) -> None:
        raw = await asyncio.to_thread(src.read_bytes)
        soup = await asyncio.to_thread(BeautifulSoup, raw, HTML_PARSER)
        nodes = EpubTranslator._collect(soup)
        texts = []
        for node in nodes:
            m = _ws_re.match(str(node))
            texts.append(m.group(2) if m else str(node))
        # <title> is skipped by _collect (inside <head>) — translate it too
        title = soup.title if soup.title and soup.title.string and soup.title.string.strip() else None
        if title:
            texts.append(title.string.strip())
        self._count(texts, "HTML")
        out = await self.tr.translate_many(texts, progress)

        def apply() -> None:
            for node, t in zip(nodes, out):
                m = _ws_re.match(str(node))
                lead, trail = (m.group(1), m.group(3)) if m else ("", "")
                node.replace_with(NavigableString(f"{lead}{t}{trail}"))
            if title:
                title.string = out[-1]
            dst.write_bytes(EpubTranslator._serialize(soup, raw))

        await asyncio.to_thread(apply)


class DocxTranslator(BaseDocTranslator):
    """DOCX is a zip of XML.  We translate every <w:t> run text in
    word/document.xml, headers, footers, footnotes and endnotes.  Styles,
    images, tables, numbering, comments … are byte-for-byte untouched.

    Runs that belong to the same paragraph are merged (Word splits a
    sentence into many <w:t> for spell-check / formatting reasons) so the
    translation sees whole sentences; the result is written back into the
    first run of each group and the rest are emptied — the group keeps the
    formatting of its first run."""

    _parts_re = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")

    def _parse(self, src: Path):
        # lxml keeps namespace prefixes / mc:Ignorable declarations intact;
        # stdlib ElementTree would rewrite them to ns0:, which Word rejects.
        from lxml import etree as ET

        W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        parsed = []  # (name, tree, groups)  groups: list of list[Element w:t]
        texts: List[str] = []
        with zipfile.ZipFile(src) as z:
            names = z.namelist()
            for n in names:
                if not self._parts_re.match(n):
                    continue
                tree = ET.fromstring(z.read(n))
                groups: List[List] = []
                for p in tree.iter(f"{W}p"):
                    runs = [t for t in p.iter(f"{W}t") if t.text]
                    if not runs:
                        continue
                    # split a paragraph into groups at tabs/breaks so layout survives
                    group: List = []
                    for r in p.iter():
                        if r.tag == f"{W}t" and r.text:
                            group.append(r)
                        elif r.tag in (f"{W}tab", f"{W}br", f"{W}cr") and group:
                            groups.append(group)
                            group = []
                    if group:
                        groups.append(group)
                kept: List[List] = []
                for g in groups:
                    joined = "".join(t.text for t in g)
                    if joined.strip() and any(ch.isalpha() for ch in joined):
                        kept.append(g)
                        texts.append(joined.strip())
                if kept:
                    parsed.append((n, tree, kept))
        return names, parsed, texts

    @staticmethod
    def _write(src: Path, dst: Path, names, parsed, out: List[str]) -> None:
        from lxml import etree as ET

        XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
        i = 0
        new: Dict[str, bytes] = {}
        for n, tree, groups in parsed:
            for g in groups:
                joined = "".join(t.text for t in g)
                lead = joined[: len(joined) - len(joined.lstrip())]
                trail = joined[len(joined.rstrip()):]
                g[0].text = f"{lead}{out[i]}{trail}"
                g[0].set(XML_SPACE, "preserve")
                for t in g[1:]:
                    t.text = ""
                i += 1
            new[n] = ET.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename.endswith("/"):
                    continue
                data = new.get(info.filename)
                if data is None:
                    zout.writestr(info, zin.read(info.filename))
                else:
                    zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zout.writestr(zi, data)

    async def translate(self, src: Path, dst: Path, progress=None) -> None:
        if not zipfile.is_zipfile(src):
            raise TranslationError("Not a valid DOCX file (old .doc format is not supported — save as .docx)")
        names, parsed, texts = await asyncio.to_thread(self._parse, src)
        self._count(texts, "Document")
        out = await self.tr.translate_many(texts, progress)
        await asyncio.to_thread(self._write, src, dst, names, parsed, out)


# Fonts for PDF output.  PDF text is drawn with the font embedded in the file,
# which almost never has Devanagari / Bengali / Arabic … glyphs, so we bring
# our own (Google Noto, OFL licence).  Downloaded once, cached in DATA_DIR/fonts.
_NOTO = "https://github.com/notofonts/notofonts.github.io/raw/main/fonts/{0}/hinted/ttf/{0}-Regular.ttf"
PDF_FONTS: Dict[str, Tuple[str, str]] = {  # lang -> (file name, url)
    "hi": ("NotoSansDevanagari-Regular.ttf", _NOTO.format("NotoSansDevanagari")),
    "mr": ("NotoSansDevanagari-Regular.ttf", _NOTO.format("NotoSansDevanagari")),
    "ne": ("NotoSansDevanagari-Regular.ttf", _NOTO.format("NotoSansDevanagari")),
    "bn": ("NotoSansBengali-Regular.ttf", _NOTO.format("NotoSansBengali")),
    "ta": ("NotoSansTamil-Regular.ttf", _NOTO.format("NotoSansTamil")),
    "te": ("NotoSansTelugu-Regular.ttf", _NOTO.format("NotoSansTelugu")),
    "gu": ("NotoSansGujarati-Regular.ttf", _NOTO.format("NotoSansGujarati")),
    "kn": ("NotoSansKannada-Regular.ttf", _NOTO.format("NotoSansKannada")),
    "ml": ("NotoSansMalayalam-Regular.ttf", _NOTO.format("NotoSansMalayalam")),
    "pa": ("NotoSansGurmukhi-Regular.ttf", _NOTO.format("NotoSansGurmukhi")),
    "ur": ("NotoNastaliqUrdu-Regular.ttf", _NOTO.format("NotoNastaliqUrdu")),
    "ar": ("NotoSansArabic-Regular.ttf", _NOTO.format("NotoSansArabic")),
    "zh-CN": (
        "NotoSansCJKsc-Regular.otf",
        "https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
    ),
}
_LATIN_FONT = ("NotoSans-Regular.ttf", _NOTO.format("NotoSans"))
_font_locks: Dict[str, asyncio.Lock] = {}


async def ensure_pdf_font(session: aiohttp.ClientSession, lang: str) -> Optional[Path]:
    """Return a local font file that can render `lang`, downloading it on first use."""
    fname, url = PDF_FONTS.get(lang, _LATIN_FONT)
    fdir = Config.DATA_DIR / "fonts"
    fdir.mkdir(parents=True, exist_ok=True)
    path = fdir / fname
    if path.exists() and path.stat().st_size > 10_000:
        return path
    lock = _font_locks.setdefault(fname, asyncio.Lock())
    async with lock:
        if path.exists() and path.stat().st_size > 10_000:
            return path
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status != 200:
                    raise TranslationError(f"HTTP {r.status}")
                data = await r.read()
            tmp = path.with_suffix(".part")
            await asyncio.to_thread(tmp.write_bytes, data)
            tmp.replace(path)
            log.info("downloaded PDF font %s (%d KB)", fname, len(data) // 1024)
            return path
        except Exception as e:  # noqa: BLE001
            log.warning("font download failed (%s): %s — PDF will use built-in font", fname, e)
            return None


class PdfTranslator(BaseDocTranslator):
    """Layout-preserving PDF translation.

    For every text block on every page we remember its bounding box and font
    size, remove the original glyphs with a redaction (images, vector art,
    links and annotations stay), then type the translation back into the same
    box with a Unicode font.  If the translation is longer than the original
    the font is shrunk (down to 50 %) so it still fits the box."""

    def __init__(self, translator: Translator, font: Optional[Path]):
        super().__init__(translator)
        self.font = font

    @staticmethod
    def _block_text(b: dict) -> Tuple[str, float, Tuple[float, float, float], bool]:
        """Join the lines of a block into one string; return (text, size, rgb, bold)."""
        parts: List[str] = []
        sizes: List[float] = []
        color = 0
        bold = False
        for line in b["lines"]:
            ltxt = "".join(s["text"] for s in line["spans"])
            if not ltxt.strip():
                continue
            for s in line["spans"]:
                if s["text"].strip():
                    sizes.append(s["size"])
                    color = s.get("color", 0)
                    bold = bold or bool(s.get("flags", 0) & 16)
            # de-hyphenate line breaks ("transla-\ntion" → "translation")
            if parts and parts[-1].endswith("-") and ltxt[:1].islower():
                parts[-1] = parts[-1][:-1] + ltxt.strip()
            else:
                parts.append(ltxt.strip())
        text = " ".join(parts)
        size = sorted(sizes)[len(sizes) // 2] if sizes else 11.0
        rgb = ((color >> 16) & 255, (color >> 8) & 255, color & 255)
        return text, size, rgb, bold

    def _parse(self, src: Path):
        doc = pymupdf.open(src)
        if doc.is_encrypted and not doc.authenticate(""):
            raise TranslationError("PDF is password-protected")
        blocks = []  # (page_no, rect, size, rgb, bold)
        texts: List[str] = []
        for pno, page in enumerate(doc):
            d = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES)
            for b in d["blocks"]:
                if b["type"] != 0:
                    continue
                text, size, rgb, bold = self._block_text(b)
                if not text or not any(ch.isalpha() for ch in text):
                    continue
                blocks.append((pno, tuple(b["bbox"]), size, rgb, bold))
                texts.append(text)
        n_pages = len(doc)
        doc.close()
        if n_pages and not texts:
            raise TranslationError("PDF has no selectable text (scanned image PDF) — OCR is not supported")
        return blocks, texts

    def _write(self, src: Path, dst: Path, blocks, out: List[str]) -> None:
        doc = pymupdf.open(src)
        if doc.is_encrypted:
            doc.authenticate("")
        css = "* { margin:0; padding:0; line-height:1.15; }"
        archive = None
        if self.font:
            archive = pymupdf.Archive(str(self.font.parent))
            css = f"@font-face {{ font-family: tr; src: url({self.font.name}); }} * {{ font-family: tr, sans-serif; margin:0; padding:0; line-height:1.15; }}"
        by_page: Dict[int, list] = {}
        for (pno, rect, size, rgb, bold), t in zip(blocks, out):
            by_page.setdefault(pno, []).append((rect, size, rgb, bold, t))
        for pno, items in by_page.items():
            page = doc[pno]
            for rect, *_ in items:
                page.add_redact_annot(pymupdf.Rect(rect))
            # remove the old text only — keep images & vector drawings
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
            for rect, size, rgb, bold, t in items:
                r = pymupdf.Rect(rect)
                # a little slack so slightly longer translations don't get shrunk
                r.x1 = min(r.x1 + 2, page.rect.x1)
                r.y1 = min(r.y1 + size * 0.4, page.rect.y1)
                style = f"font-size:{size:.1f}pt; color:rgb({rgb[0]},{rgb[1]},{rgb[2]});" + (" font-weight:bold;" if bold else "")
                body = f'<div style="{style}">{html.escape(t)}</div>'
                try:
                    page.insert_htmlbox(r, body, css=css, archive=archive, scale_low=0.5)
                except Exception as e:  # noqa: BLE001
                    log.debug("pdf insert failed p%d: %s", pno, e)
        doc.save(dst, garbage=3, deflate=True)
        doc.close()

    async def translate(self, src: Path, dst: Path, progress=None) -> None:
        if pymupdf is None:
            raise TranslationError("PDF support is not installed on the server (pip install pymupdf)")
        blocks, texts = await asyncio.to_thread(self._parse, src)
        self._count(texts, "PDF")
        out = await self.tr.translate_many(texts, progress)
        await asyncio.to_thread(self._write, src, dst, blocks, out)


async def make_doc_translator(ext: str, translator: Translator, session: aiohttp.ClientSession, lang: str):
    kind = SUPPORTED_FORMATS[ext][0]
    if kind == "epub":
        return EpubTranslator(translator)
    if kind == "txt":
        return TxtTranslator(translator)
    if kind == "html":
        return HtmlTranslator(translator)
    if kind == "docx":
        return DocxTranslator(translator)
    if kind == "pdf":
        font = await ensure_pdf_font(session, lang)
        return PdfTranslator(translator, font)
    raise TranslationError(f"Unsupported format {ext}")


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
    ext: str = field(default=".epub", compare=False)
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

    def queued_count(self) -> int:
        # queue.qsize() also counts cancelled entries that were not popped yet
        return max(0, len(self.jobs) - len(self.running))

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
        elif job_id not in self.running:
            # Still waiting in the queue: release the user's slot *now* instead of
            # when the worker loop eventually pops it (otherwise the user is told
            # "you already have a file in progress" after cancelling).
            db.finish_job(job_id, "cancelled")
            self._cleanup(job)
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
            if job.cancelled or job.id not in self.jobs:
                self._cleanup(job)
                self.queue.task_done()
                continue
            if db.is_banned(job.user_id):
                db.finish_job(job.id, "cancelled")
                self._cleanup(job)
                self.queue.task_done()
                continue
            self.running[job.id] = job
            job.task = asyncio.create_task(self._process(app, job))
            try:
                await job.task
            except asyncio.CancelledError:
                if not job.cancelled:
                    db.finish_job(job.id, "failed")
                    self._cleanup(job)
                    self.queue.task_done()
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
        fmt_label = SUPPORTED_FORMATS[job.ext][1]
        epub = await make_doc_translator(job.ext, translator, self.session, job.lang)
        out_path = job.file_path.with_name(f"{Path(job.file_name).stem} [{job.lang}]{job.ext}")

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

        await safe_edit(job.status_msg, f"🔍 <b>Analysing {fmt_label}…</b>\n📄 {html.escape(job.file_name)}", cancel_kb(job.id))
        await epub.translate(job.file_path, out_path, progress)

        elapsed = time.monotonic() - t0
        await safe_edit(job.status_msg, "📤 <b>Uploading translated file…</b>")
        caption = (
            f"✅ <b>Translation complete</b>\n"
            f"📄 {html.escape(job.file_name)}\n"
            f"🌐 {lang_name(job.lang)} · {epub.segments:,} segments · {epub.chars:,} chars\n"
            f"⏱ {int(elapsed // 60)}m {int(elapsed % 60)}s · {int(epub.chars / max(elapsed, 1) / 1000)}k chars/s"
        )
        try:
            await app.send_document(job.chat_id, str(out_path), caption=caption, file_name=out_path.name)
        except FloodWait as e:
            await asyncio.sleep(e.value)
            await app.send_document(job.chat_id, str(out_path), caption=caption, file_name=out_path.name)
        try:
            await job.status_msg.delete()
        except Exception:
            pass
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


async def safe_edit(msg: Optional[Message], text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    if msg is None:
        return
    for _ in range(2):
        try:
            await msg.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
            return
        except MessageNotModified:
            return
        except FloodWait as e:
            await asyncio.sleep(min(e.value, 30))
        except Exception as e:  # noqa: BLE001
            log.debug("edit failed: %s", e)
            return


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
# users whose file is currently being downloaded (slot reserved, job not yet queued)
downloading: set = set()

# ═══════════════════════════════════════════════════════════════════════════
#  BOT
# ═══════════════════════════════════════════════════════════════════════════

# Pyrogram 2.0.x grabs the running loop in Client.__init__ via the deprecated
# asyncio.get_event_loop(); on Python >= 3.12 that warns and on 3.14 it raises
# when no loop exists yet, so create one explicitly first.
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

app = Client(
    Config.SESSION_NAME,
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    workdir=str(Config.DATA_DIR),
    parse_mode=ParseMode.HTML,
)


def _chat_ref(ref: str):
    """'-1001234567890' → int, '@channel' → str (Pyrogram needs ints for numeric ids)."""
    return int(ref) if re.fullmatch(r"-?\d+", ref) else ref


async def check_force_sub(client: Client, uid: int) -> bool:
    if not Config.FORCE_SUB_CHANNEL or Config.is_admin(uid):
        return True
    try:
        m = await client.get_chat_member(_chat_ref(Config.FORCE_SUB_CHANNEL), uid)
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
        "📎 <b>Send me a file to begin</b> — EPUB · PDF · DOCX · TXT · HTML",
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
        "2. Send a file: <b>.epub · .pdf · .docx · .txt · .md · .html</b>\n"
        "3. Watch live progress, get the translated file back in the same format\n\n"
        "✨ <b>What's preserved</b>\n"
        "• EPUB/HTML: chapters, bold/italic, links, images, TOC, CSS\n"
        "• DOCX: styles, tables, images, headers/footers, footnotes\n"
        "• PDF: page layout, images — text is replaced in place (scanned PDFs not supported)\n"
        "• TXT/MD: line breaks and indentation\n\n"
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
    await safe_edit(cq.message, f"🌐 Target language: <b>{lang_name(code)}</b>\n\n📎 Now send me a file (EPUB · PDF · DOCX · TXT · HTML).")


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
        f"⚙️ Processing: {len(jobs.running)} · Queued: {jobs.queued_count()}" + mine
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
    if not payments.enabled:
        return await cq.answer("Payment service is not configured.", show_alert=True)
    # a callback query can only be answered ONCE — so verify first, answer after
    if await payments.verify(link_id):
        # re-check: two quick taps must not grant premium twice
        if (db.get_payment(link_id) or {})["status"] == "paid":
            return await cq.answer("Already activated ✅", show_alert=True)
        db.mark_paid(link_id)
        until = db.add_premium(p["user_id"], p["days"])
        await cq.answer("Payment verified ✅")
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
    if not message.from_user:
        return
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
def _sniff_ok(path: Path, ext: str) -> bool:
    """Cheap magic-byte check so we fail fast on mislabelled files."""
    try:
        if path.stat().st_size == 0:
            return False
        kind = SUPPORTED_FORMATS[ext][0]
        if kind in ("epub", "docx"):
            return zipfile.is_zipfile(path)
        if kind == "pdf":
            with open(path, "rb") as f:
                return f.read(1024).lstrip().startswith(b"%PDF")
        return True  # txt / html: anything goes
    except Exception:
        return False


@app.on_message(filters.private & filters.document)
async def on_document(client: Client, message: Message) -> None:
    row = await guard(client, message)
    if not row:
        return
    uid = message.from_user.id
    if uid in pending_input:  # admin is sending a broadcast attachment
        return
    doc = message.document
    ext = detect_format(doc.file_name or "", doc.mime_type)
    if not ext:
        return await message.reply_text(
            "⚠️ Unsupported file type.\n\nSupported: <b>" + " · ".join(sorted(SUPPORTED_FORMATS)) + "</b>"
        )
    if ext == ".pdf" and pymupdf is None:
        return await message.reply_text("⚠️ PDF support is not installed on this server.")
    name = doc.file_name or f"document{ext}"
    if not name.lower().endswith(ext):
        name += ext

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
    if jobs.user_has_job(uid) or uid in downloading:
        return await message.reply_text("⚠️ You already have a file in progress. Use /cancel to stop it first.")
    if not pool.available() and not Config.DIRECT_FALLBACK and Config.DIRECT_CONCURRENCY <= 0:
        return await message.reply_text("⚠️ Translation service is offline right now. Please try again later.")

    # reserve the user's slot *before* the (slow) download so two files sent
    # back-to-back cannot both slip past the "one job per user" check
    downloading.add(uid)
    status = await message.reply_text("📥 <b>Downloading…</b>")
    tmp_dir = Path(tempfile.mkdtemp(prefix="epub_", dir=Config.DATA_DIR))
    safe_name = re.sub(r"[^\w.\- ]", "_", name).strip() or f"document{ext}"
    if not safe_name.lower().endswith(ext):
        safe_name += ext
    try:
        path = await message.download(file_name=str(tmp_dir / safe_name))
    except Exception as e:  # noqa: BLE001
        log.warning("download failed for %s: %s", uid, e)
        path = None
    if not path or not _sniff_ok(Path(path), ext):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        downloading.discard(uid)
        await safe_edit(status, f"❌ Download failed or this is not a valid {SUPPORTED_FORMATS[ext][1]} file.")
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
        ext=ext,
    )
    await jobs.submit(job)
    downloading.discard(uid)
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
        f"⚙️ Processing: {len(jobs.running)} · Queued: {jobs.queued_count()}\n\n"
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
        ordered = sorted(jobs.jobs.values(), key=lambda j: (j.id not in jobs.running, j.priority, j.created))
        for j in ordered[:40]:
            state = "⚙️" if j.id in jobs.running else "⏳"
            lines.append(f"{state} #{j.id} · <code>{j.user_id}</code> · {html.escape(j.file_name[:30])} → {j.lang}")
        if len(ordered) > 40:
            lines.append(f"… and {len(ordered) - 40} more")
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
    pending_input.pop(message.from_user.id, None)
    if len(message.command) < 2:
        return await message.reply_text("Usage: <code>/addworker https://xyz.onrender.com</code>")
    added = [u for u in message.command[1:] if pool.add(u)]
    if jobs.session and added:
        await pool.health_check(jobs.session)
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
    pending_input.pop(message.from_user.id, None)
    src = message.reply_to_message
    if not src:
        text = (message.text or message.caption or "").split(None, 1)
        if len(text) < 2:
            return await message.reply_text("Reply to a message with /broadcast, or <code>/broadcast TEXT</code>.")
        try:
            src = await message.reply_text(text[1])
        except Exception:  # invalid HTML in the text → send it verbatim
            src = await message.reply_text(text[1], parse_mode=ParseMode.DISABLED)
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
    text = message.text or ""
    if text in (BTN_LANG, BTN_PREMIUM, BTN_STATUS, BTN_HELP, BTN_ADMIN):
        return
    if text.startswith("/"):
        # any other command (/addworker, /ban, …) was already handled in group 0;
        # it must not be swallowed here as a worker URL / broadcast text
        pending_input.pop(message.from_user.id, None)
        return
    pending_input.pop(message.from_user.id, None)
    if mode == "add_worker":
        if not text:
            await message.reply_text("⚠️ Please send the worker URL as text.")
        else:
            urls = text.split()
            added = [u for u in urls if pool.add(u)]
            bad = [u for u in urls if not pool.is_valid(pool.normalize(u))]
            if jobs.session and added:
                await pool.health_check(jobs.session)
            note = f"\n⚠️ Ignored invalid: {html.escape(', '.join(bad))}" if bad else ""
            await message.reply_text(f"✅ Added {len(added)} worker(s).{note}\n\n" + pool.summary(), reply_markup=workers_kb())
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
    await message.reply_text(
        "📎 Send me a file to translate (<b>EPUB · PDF · DOCX · TXT · HTML</b>), or use the menu below.",
        reply_markup=main_kb(message.from_user.id),
    )


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
    tasks += [asyncio.create_task(jobs.worker_loop(app, i + 1)) for i in range(max(1, Config.MAX_CONCURRENT_JOBS))]
    log.info("Bot @%s started · %d workers · %d job slots", me.username, len(pool.workers), Config.MAX_CONCURRENT_JOBS)
    if Config.OWNER_ID:
        try:
            await app.send_message(Config.OWNER_ID, f"🟢 Bot restarted · {len(pool.available())}/{len(pool.workers)} workers online")
        except Exception:
            pass
    await idle()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await jobs.session.close()
    await app.stop()
    log.info("Bot stopped")


if __name__ == "__main__":
    app.run(main())
