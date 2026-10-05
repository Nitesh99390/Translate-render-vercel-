#!/usr/bin/env python3
"""
EPUB Translator Bot — Master (single file)
==========================================
Run this on the Oracle VPS.  Translation workers (app.py) run on Hugging Face /
PythonAnywhere / Vercel / Render / Railway / Koyeb — any host, same file.

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
import functools
import hashlib
import html
import logging
import os
import posixpath
import re
import secrets
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
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote

import aiohttp
import warnings
from bs4 import BeautifulSoup, NavigableString
from bs4.element import PreformattedString, Script, Stylesheet, TemplateString

try:  # bs4 >= 4.11 warns when XHTML is parsed with an HTML parser — intended here
    from bs4 import XMLParsedAsHTMLWarning

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except Exception:  # pragma: no cover
    pass
from pyrogram import Client, ContinuePropagation, StopPropagation, filters, idle, raw
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import FloodWait, MessageNotModified, UserNotParticipant
from pyrogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# ── coloured buttons + custom-emoji icons (Bot API 9.4 / kurigram >= 2.2) ──
# `pyrogram.enums.ButtonStyle` only exists in the maintained fork (kurigram).
# On the old pyrogram 2.0.x wheel everything degrades to plain white buttons.
try:
    from pyrogram.enums import ButtonStyle  # type: ignore

    HAS_BUTTON_STYLE = True
except ImportError:  # pragma: no cover - legacy pyrogram
    class ButtonStyle:  # type: ignore[no-redef]
        DEFAULT = PRIMARY = DANGER = SUCCESS = None

    HAS_BUTTON_STYLE = False

try:
    from pyrogram.types import LinkPreviewOptions  # type: ignore

    _NO_PREVIEW_KW: Dict[str, Any] = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
except ImportError:  # pragma: no cover - legacy pyrogram
    _NO_PREVIEW_KW = {"disable_web_page_preview": True}

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

import docconv  # output-format conversion + size splitting (same repo)

# ═══════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════


def _env(key: str, default: str = "") -> str:
    # systemd's EnvironmentFile (and some dashboards) keep inline "# comments"
    # as part of the value — e.g. DIRECT_FALLBACK="true       # use Google …".
    # Strip them so booleans / ints parse and don't silently fall back to defaults.
    val = os.environ.get(key, default)
    if "#" in val:
        head = val.split("#", 1)[0]
        # only treat it as a comment when preceded by whitespace (or the whole
        # value is a comment); "#channel"-style tokens must stay intact
        if not head.strip() or head != head.rstrip():
            val = head
    return val.strip().strip('"').strip("'")


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

    # ── plans ──────────────────────────────────────────────────────────
    # Free tier is metered in *characters* per day (any number of files);
    # 1,000,000 chars ≈ two average light-novel volumes.
    # (FREE_DAILY_LIMIT — files/day — is no longer used; kept readable for old .env files.)
    FREE_DAILY_CHARS = _env_int("FREE_DAILY_CHARS", 1_000_000)
    FREE_MAX_FILE_MB = _env_int("FREE_MAX_FILE_MB", 20)

    # Telegram Stars (⭐ XTR) — paid inside Telegram, no Razorpay needed.
    # 60 ⭐ → 1 month Premium (same benefits as the ₹ Premium plan).
    STARS_ENABLED = _env_bool("STARS_ENABLED", True)
    STARS_PREMIUM_PRICE = _env_int("STARS_PREMIUM_PRICE", 60)
    STARS_PREMIUM_DAYS = _env_int("STARS_PREMIUM_DAYS", 30)

    # ₹10 · 5 file credits that never expire
    STARTER_PRICE_INR = _env_int("STARTER_PRICE_INR", 10)
    STARTER_CREDITS = _env_int("STARTER_CREDITS", 5)
    # ₹40 · 25 file credits that never expire (bulk discount)
    BULK_PRICE_INR = _env_int("BULK_PRICE_INR", 40)
    BULK_CREDITS = _env_int("BULK_CREDITS", 25)
    # file-size cap when a credit is spent
    CREDIT_MAX_FILE_MB = _env_int("CREDIT_MAX_FILE_MB", 50)

    # ₹50 · 30 days · 5 files/day · 50 MB
    BASIC_PRICE_INR = _env_int("BASIC_PRICE_INR", 50)
    BASIC_DAYS = _env_int("BASIC_DAYS", 30)
    BASIC_DAILY_LIMIT = _env_int("BASIC_DAILY_LIMIT", 5)
    BASIC_MAX_FILE_MB = _env_int("BASIC_MAX_FILE_MB", 50)

    # ₹100 · 30 days · unlimited · 200 MB   (env names kept for backwards compat)
    PREMIUM_PRICE_INR = _env_int("PREMIUM_PRICE_INR", 100)
    PREMIUM_DAYS = _env_int("PREMIUM_DAYS", 30)
    PREMIUM_MAX_FILE_MB = _env_int("PREMIUM_MAX_FILE_MB", 200)
    # ₹250 · 90 days · unlimited · 200 MB   (3 months for the price of 2.5)
    PREMIUM3_PRICE_INR = _env_int("PREMIUM3_PRICE_INR", 250)
    PREMIUM3_DAYS = _env_int("PREMIUM3_DAYS", 90)

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

    # ── output options ─────────────────────────────────────────────────
    # Preset split sizes (MB) offered as buttons; 0 = "no split" is always there.
    SPLIT_PRESETS_MB = [int(x) for x in _env("SPLIT_PRESETS_MB", "10,20,50").split(",") if x.strip().isdigit()] or [10, 20, 50]
    # Smallest custom split a user may ask for (KB) and Telegram's per-file cap (MB).
    SPLIT_MIN_KB = _env_int("SPLIT_MIN_KB", 512)
    TG_MAX_FILE_MB = _env_int("TG_MAX_FILE_MB", 2000)
    # Seconds the "choose output" panel waits before starting with the defaults.
    OPTIONS_TIMEOUT = _env_int("OPTIONS_TIMEOUT", 90)

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
#  PLANS
# ═══════════════════════════════════════════════════════════════════════════
#
# Two kinds of paid products:
#   • "sub"    — time-limited subscription (daily_limit=0 → unlimited)
#   • "credits" — a pack of file credits that NEVER expires
#
# Resolution order when a user sends a file (see resolve_access):
#   admin → active subscription (within its daily limit) → credits → free tier
# Credits are therefore only spent when the subscription is exhausted/missing.


@dataclass(frozen=True)
class Plan:
    key: str
    title: str
    emoji: str
    price: int              # INR, or ⭐ Stars when currency == "XTR"
    kind: str               # "sub" | "credits"
    days: int = 0           # sub only
    daily_limit: int = 0    # sub only, 0 = unlimited
    credits: int = 0        # credits only
    max_mb: int = 50
    priority: int = 1       # queue priority (0 = highest)
    tagline: str = ""
    currency: str = "INR"   # "INR" (Razorpay) | "XTR" (Telegram Stars)

    @property
    def is_sub(self) -> bool:
        return self.kind == "sub"

    @property
    def is_stars(self) -> bool:
        return self.currency == "XTR"

    @property
    def price_label(self) -> str:
        return f"{self.price} ⭐" if self.is_stars else f"₹{self.price}"

    @property
    def button(self) -> str:
        return f"{self.emoji} {self.title} — {self.price_label}"

    def features(self) -> List[str]:
        f: List[str] = []
        if self.is_sub:
            f.append(f"Valid {self.days} days")
            f.append("Unlimited translations" if self.daily_limit == 0 else f"{self.daily_limit} files every day")
        else:
            f.append(f"{self.credits} file credits")
            f.append("Never expires — use anytime")
        f.append(f"Files up to {self.max_mb} MB")
        if self.priority == 0:
            f.append("Priority queue (skip the line)")
        elif self.priority == 1:
            f.append("Faster queue than Free")
        return f


PLANS: Dict[str, Plan] = {
    p.key: p
    for p in (
        Plan(
            key="starter", title="Starter Pack", emoji="🎟", kind="credits",
            price=Config.STARTER_PRICE_INR, credits=Config.STARTER_CREDITS,
            max_mb=Config.CREDIT_MAX_FILE_MB, priority=1,
            tagline="Pay once, use whenever you like",
        ),
        Plan(
            key="bulk", title="Bulk Pack", emoji="🎫", kind="credits",
            price=Config.BULK_PRICE_INR, credits=Config.BULK_CREDITS,
            max_mb=Config.CREDIT_MAX_FILE_MB, priority=1,
            tagline="Best value credits — 20% cheaper per file",
        ),
        Plan(
            key="basic", title="Basic", emoji="🔹", kind="sub",
            price=Config.BASIC_PRICE_INR, days=Config.BASIC_DAYS,
            daily_limit=Config.BASIC_DAILY_LIMIT, max_mb=Config.BASIC_MAX_FILE_MB, priority=1,
            tagline="For regular readers",
        ),
        Plan(
            key="premium", title="Premium", emoji="⭐", kind="sub",
            price=Config.PREMIUM_PRICE_INR, days=Config.PREMIUM_DAYS,
            daily_limit=0, max_mb=Config.PREMIUM_MAX_FILE_MB, priority=0,
            tagline="Most popular — no limits at all",
        ),
        Plan(
            key="premium3", title="Premium 3 Months", emoji="👑", kind="sub",
            price=Config.PREMIUM3_PRICE_INR, days=Config.PREMIUM3_DAYS,
            daily_limit=0, max_mb=Config.PREMIUM_MAX_FILE_MB, priority=0,
            tagline="Save ₹50 vs monthly",
        ),
        Plan(
            key="stars", title="Premium · Stars", emoji="🌟", kind="sub",
            price=Config.STARS_PREMIUM_PRICE, days=Config.STARS_PREMIUM_DAYS,
            daily_limit=0, max_mb=Config.PREMIUM_MAX_FILE_MB, priority=0,
            tagline="1 month Premium paid with Telegram ⭐ Stars — works from any country, no card/UPI needed",
            currency="XTR",
        ),
    )
    if Config.STARS_ENABLED or p.key != "stars"
}


def sub_plan_for_grant(plan_key: str) -> str:
    """Stars Premium is the same product as `premium`; store it under that key so
    `active_sub()` / upgrades / admin tools treat both identically."""
    p = PLANS.get(plan_key)
    return "premium" if p is not None and p.is_stars else plan_key


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
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            # ── migrations for DBs created before multi-plan support ──
            self._add_column("users", "plan", "TEXT DEFAULT ''")          # active subscription key
            self._add_column("users", "credits", "INTEGER DEFAULT 0")     # never-expiring file credits
            self._add_column("payments", "plan", "TEXT DEFAULT ''")
            self._add_column("payments", "units", "INTEGER DEFAULT 0")     # credits bought (credit packs)
            # output preferences: '' = same format as input · split_kb 0 = don't split
            self._add_column("users", "out_format", "TEXT DEFAULT ''")
            self._add_column("users", "split_kb", "INTEGER DEFAULT 0")
            self._add_column("users", "ask_options", "INTEGER DEFAULT 1")   # show the options panel per file
            self._add_column("jobs", "out_format", "TEXT DEFAULT ''")
            self._add_column("jobs", "parts", "INTEGER DEFAULT 1")
            # free tier is metered in characters/day (files/day kept for subs)
            self._add_column("users", "daily_chars", "INTEGER DEFAULT 0")
            # drop crawler-generated "Source / Generated by / Table of contents" pages
            self._add_column("users", "strip_extras", "INTEGER DEFAULT 1")
            # Telegram Stars payments: 'XTR' rows are paid via invoices, not Razorpay
            self._add_column("payments", "currency", "TEXT DEFAULT 'INR'")
            self._add_column("payments", "charge_id", "TEXT DEFAULT ''")   # Telegram Stars charge id (for refunds)
            # old rows with a live premium_until but no plan key → they bought the old Premium
            self._con.execute(
                "UPDATE users SET plan='premium' WHERE (plan='' OR plan IS NULL) AND premium_until>?",
                (int(time.time()),),
            )

    def _add_column(self, table: str, col: str, decl: str) -> None:
        cols = {r[1] for r in self._con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            self._con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

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
    # ── key/value settings (bot-wide, admin-editable at runtime) ──────────
    def get_setting(self, key: str, default: str = "") -> str:
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row and row["value"] is not None else default

    def set_setting(self, key: str, value: str) -> None:
        self._exec("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def del_setting(self, key: str) -> None:
        self._exec("DELETE FROM settings WHERE key=?", (key,))

    def all_settings(self, prefix: str) -> Dict[str, str]:
        return {r["key"]: r["value"] for r in self._all("SELECT key,value FROM settings WHERE key LIKE ?", (prefix + "%",))}

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

    # ── output preferences ─────────────────────────────────────────────────
    def set_out_format(self, uid: int, ext: str) -> None:
        self._exec("UPDATE users SET out_format=? WHERE id=?", (ext, uid))

    def set_split_kb(self, uid: int, kb: int) -> None:
        self._exec("UPDATE users SET split_kb=? WHERE id=?", (max(0, int(kb)), uid))

    def set_ask_options(self, uid: int, ask: bool) -> None:
        self._exec("UPDATE users SET ask_options=? WHERE id=?", (1 if ask else 0, uid))

    def set_strip_extras(self, uid: int, on: bool) -> None:
        self._exec("UPDATE users SET strip_extras=? WHERE id=?", (1 if on else 0, uid))

    def _ensure_user(self, uid: int) -> None:
        if self.get_user(uid) is None:
            self._exec(
                "INSERT INTO users(id,name,username,lang,joined) VALUES(?,?,?,?,?)",
                (uid, "", "", Config.DEFAULT_LANG, int(time.time())),
            )

    # ── subscriptions ──────────────────────────────────────────────────────
    def active_sub(self, uid: int) -> Optional[Plan]:
        """The user's live subscription plan, or None."""
        row = self.get_user(uid)
        if not row or row["premium_until"] <= time.time():
            return None
        return PLANS.get(row["plan"] or "premium") or PLANS["premium"]

    def is_premium(self, uid: int) -> bool:
        """True for admins and users on an *unlimited* subscription."""
        if Config.is_admin(uid):
            return True
        p = self.active_sub(uid)
        return bool(p and p.daily_limit == 0)

    def add_subscription(self, uid: int, plan_key: str, days: int) -> int:
        """Grant/extend a subscription.

        Same plan → extend from current expiry.  Different plan → the higher tier
        wins immediately; remaining days of the old plan are carried over so
        nobody loses paid time when upgrading.
        """
        self._ensure_user(uid)
        row = self.get_user(uid)
        now = int(time.time())
        cur_until = row["premium_until"] or 0
        cur_key = row["plan"] or ""
        new_plan = PLANS.get(plan_key) or PLANS["premium"]
        cur_plan = PLANS.get(cur_key) if cur_until > now else None
        base = max(now, cur_until)
        until = base + days * 86400
        if cur_plan and cur_plan.key != new_plan.key:
            # keep whichever tier is "bigger" (unlimited > limited, then by price)
            def rank(p: Plan) -> Tuple[int, int]:
                return (1 if p.daily_limit == 0 else 0, p.price)
            final_key = new_plan.key if rank(new_plan) >= rank(cur_plan) else cur_plan.key
        else:
            final_key = new_plan.key
        self._exec("UPDATE users SET premium_until=?, plan=? WHERE id=?", (until, final_key, uid))
        return until

    # kept for old call-sites / admin command
    def add_premium(self, uid: int, days: int) -> int:
        return self.add_subscription(uid, "premium", days)

    def revoke_premium(self, uid: int) -> None:
        self._exec("UPDATE users SET premium_until=0, plan='' WHERE id=?", (uid,))

    # ── credits ────────────────────────────────────────────────────────────
    def credits(self, uid: int) -> int:
        row = self.get_user(uid)
        return int(row["credits"] or 0) if row else 0

    def add_credits(self, uid: int, n: int) -> int:
        self._ensure_user(uid)
        self._exec("UPDATE users SET credits=MAX(0, credits+?) WHERE id=?", (n, uid))
        return self.credits(uid)

    def spend_credit(self, uid: int) -> bool:
        """Atomically consume one credit. Returns False if none left."""
        with self._lock, self._con:
            cur = self._con.execute("UPDATE users SET credits=credits-1 WHERE id=? AND credits>0", (uid,))
            return cur.rowcount > 0

    # ── daily counters ─────────────────────────────────────────────────────
    def daily_used(self, uid: int) -> int:
        row = self.get_user(uid)
        today = today_str()
        if not row or row["daily_date"] != today:
            return 0
        return row["daily_used"]

    def daily_chars_used(self, uid: int) -> int:
        row = self.get_user(uid)
        if not row or row["daily_date"] != today_str():
            return 0
        return int(row["daily_chars"] or 0)

    def record_usage(self, uid: int, count_daily: bool = True, chars: int = 0) -> None:
        """Bump total_files; also bump today's file + character counters unless
        the file was paid for with a credit (credits are not subject to daily
        limits).  Both counters live on the same `daily_date` and reset together."""
        today = today_str()
        row = self.get_user(uid)
        same_day = bool(row and row["daily_date"] == today)
        used = (row["daily_used"] if same_day else 0) + (1 if count_daily else 0)
        used_chars = (int(row["daily_chars"] or 0) if same_day else 0) + (max(0, chars) if count_daily else 0)
        self._exec(
            "UPDATE users SET daily_used=?, daily_chars=?, daily_date=?, total_files=total_files+1 WHERE id=?",
            (used, used_chars, today, uid),
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
            "credits_out": self._one("SELECT COALESCE(SUM(credits),0) c FROM users")["c"],
            "banned": self._one("SELECT COUNT(*) c FROM users WHERE banned=1")["c"],
            "today_users": self._one("SELECT COUNT(*) c FROM users WHERE joined>?", (now - 86400,))["c"],
            "jobs_total": self._one("SELECT COUNT(*) c FROM jobs")["c"],
            "jobs_done": self._one("SELECT COUNT(*) c FROM jobs WHERE status='done'")["c"],
            "jobs_failed": self._one("SELECT COUNT(*) c FROM jobs WHERE status='failed'")["c"],
            "chars": self._one("SELECT COALESCE(SUM(chars),0) c FROM jobs WHERE status='done'")["c"],
            "payments": self._one("SELECT COUNT(*) c FROM payments WHERE status='paid'")["c"],
            "revenue": self._one("SELECT COALESCE(SUM(amount),0) c FROM payments WHERE status='paid' AND COALESCE(currency,'INR')!='XTR'")["c"],
            "stars": self._one("SELECT COALESCE(SUM(amount),0) c FROM payments WHERE status='paid' AND currency='XTR'")["c"],
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
    def add_payment(self, link_id: str, uid: int, amount: int, days: int, plan: str = "premium", units: int = 0, currency: str = "INR") -> None:
        self._exec(
            "INSERT OR REPLACE INTO payments(link_id,user_id,amount,days,plan,units,status,created,currency) VALUES(?,?,?,?,?,?,'created',?,?)",
            (link_id, uid, amount, days, plan, units, int(time.time()), currency),
        )

    def get_payment(self, link_id: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM payments WHERE link_id=?", (link_id,))

    def mark_paid(self, link_id: str, charge_id: str = "") -> None:
        self._exec("UPDATE payments SET status='paid', charge_id=? WHERE link_id=?", (charge_id or "", link_id))

    # ── jobs ───────────────────────────────────────────────────────────────
    def add_job(self, uid: int, file_name: str, lang: str) -> int:
        with self._lock, self._con:
            cur = self._con.execute(
                "INSERT INTO jobs(user_id,file_name,lang,status,created) VALUES(?,?,?,'queued',?)",
                (uid, file_name, lang, int(time.time())),
            )
            return int(cur.lastrowid)

    def finish_job(self, job_id: int, status: str, segments: int = 0, chars: int = 0, seconds: float = 0, out_format: str = "", parts: int = 1) -> None:
        self._exec(
            "UPDATE jobs SET status=?, segments=?, chars=?, seconds=?, out_format=?, parts=? WHERE id=?",
            (status, segments, chars, round(seconds, 1), out_format, parts, job_id),
        )

    def fail_interrupted_jobs(self) -> int:
        """Jobs still 'queued'/'running' from a previous process can never finish."""
        with self._lock, self._con:
            cur = self._con.execute("UPDATE jobs SET status='failed' WHERE status IN ('queued','running')")
            return cur.rowcount or 0


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
            # Render free instances need up to ~60 s to wake from sleep; HF Spaces
            # that were paused (48 h idle) can take a bit longer on the first hit
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
                if r.status == 401:
                    log.warning("worker %s rejected our key — WORKER_SECRET mismatch?", w.url)
                    raise TranslationError("HTTP 401 (WORKER_SECRET mismatch)")
                if r.status != 200:
                    raise TranslationError(f"HTTP {r.status}")
                data = await r.json(content_type=None)
            if not isinstance(data, dict):
                raise TranslationError("bad worker response")
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
        # optional hook(segments, chars) called after parsing, before any request
        # is sent — used for the free-tier character budget check
        self.before_translate = None

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
        for n, soup, nodes, raw_bytes in parsed:
            for node in nodes:
                m = _ws_re.match(str(node))
                lead, _, trail = (m.group(1), m.group(2), m.group(3)) if m else ("", "", "")
                node.replace_with(NavigableString(f"{lead}{translated[i]}{trail}"))
                i += 1
            new_content[n] = EpubTranslator._serialize(soup, raw_bytes)

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
        if self.before_translate:
            self.before_translate(self.segments, self.chars)

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
    # UTF-16 must only be tried when there is a BOM: without one, almost any
    # byte string "decodes" as UTF-16 into CJK garbage (e.g. a cp1252 file with
    # a single accented letter) and the whole book would be destroyed.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


class BaseDocTranslator:
    def __init__(self, translator: Translator):
        self.tr = translator
        self.segments = 0
        self.chars = 0
        self.before_translate = None  # see EpubTranslator

    def _count(self, texts: List[str], what: str) -> None:
        self.segments = len(texts)
        self.chars = sum(len(t) for t in texts)
        if self.segments == 0:
            raise TranslationError(f"{what} contains no translatable text")
        if self.before_translate:
            self.before_translate(self.segments, self.chars)

    async def translate(self, src: Path, dst: Path, progress=None) -> None:  # pragma: no cover
        raise NotImplementedError


class TxtTranslator(BaseDocTranslator):
    """Plain text / Markdown: translate paragraph by paragraph, keep blank lines,
    indentation and line endings exactly as they were."""

    _para_re = re.compile(r"([^\n]+)")

    async def translate(self, src: Path, dst: Path, progress=None) -> None:
        text = _decode_text(await asyncio.to_thread(src.read_bytes))
        # Normalise line endings first, remember the original style and put it
        # back at the end.  (Splitting on "\n" while keeping the "\r" and then
        # joining with "\r\n" used to produce "\r\r\n" on every untouched line.)
        nl = "\r\n" if "\r\n" in text else "\n"
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        idx: List[int] = []
        texts: List[str] = []
        for i, line in enumerate(lines):
            m = _ws_re.match(line)
            body = m.group(2) if m else line
            if body and any(ch.isalpha() for ch in body):
                idx.append(i)
                texts.append(body)
        self._count(texts, "File")
        out = await self.tr.translate_many(texts, progress)
        for i, t in zip(idx, out):
            m = _ws_re.match(lines[i])
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
        P_TAG = f"{W}p"

        def _owner_p(el):
            """Nearest enclosing <w:p> of an element."""
            el = el.getparent()
            while el is not None and el.tag != P_TAG:
                el = el.getparent()
            return el

        parsed = []  # (name, tree, groups)  groups: list of list[Element w:t]
        texts: List[str] = []
        with zipfile.ZipFile(src) as z:
            names = z.namelist()
            for n in names:
                if not self._parts_re.match(n):
                    continue
                tree = ET.fromstring(z.read(n))
                groups: List[List] = []
                for p in tree.iter(P_TAG):
                    # split a paragraph into groups at tabs/breaks so layout survives.
                    # Paragraphs can be nested (text boxes, SmartArt, alt-content):
                    # only take runs whose *nearest* <w:p> is this one, otherwise
                    # the inner runs would be collected twice and written twice.
                    group: List = []
                    for r in p.iter():
                        if r is p:
                            continue
                        if r.tag == f"{W}t":
                            if r.text and _owner_p(r) is p:
                                group.append(r)
                        elif r.tag in (f"{W}tab", f"{W}br", f"{W}cr") and group and _owner_p(r) is p:
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
            if parts and parts[-1].endswith("-") and ltxt.strip()[:1].islower():
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
    credit: bool = field(default=False, compare=False)   # paid with a never-expiring credit
    credit_settled: bool = field(default=False, compare=False)  # credit consumed for real (job done)
    out_ext: str = field(default="", compare=False)      # '' → same as input, else '.pdf' / '.epub' …
    split_kb: int = field(default=0, compare=False)      # 0 → single file, else max KB per part
    strip_extras: bool = field(default=True, compare=False)  # drop crawler intro/summary/TOC pages (EPUB)
    free_chars: int = field(default=0, compare=False)    # free tier: chars still allowed today (0 = unmetered)


class QuotaExceeded(TranslationError):
    """Free-tier daily character budget is smaller than this document."""


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
        # refund the pre-charged credit if the job did not finish successfully
        if job.credit and not job.credit_settled:
            job.credit_settled = True
            db.add_credits(job.user_id, 1)
            log.info("job %d: credit refunded to user %d", job.id, job.user_id)
        try:
            if job.file_path.parent.name.startswith("epub_"):
                shutil.rmtree(job.file_path.parent, ignore_errors=True)
            else:
                job.file_path.unlink(missing_ok=True)
        except Exception:
            pass

    @staticmethod
    def _user_error(e: BaseException) -> str:
        """Friendly failure text; internal details only for known error types."""
        if isinstance(e, QuotaExceeded):
            return str(e)
        if isinstance(e, TranslationError):
            return f"❌ <b>Translation failed</b>\n{html.escape(str(e)[:300])}"
        if isinstance(e, (zipfile.BadZipFile, ValueError)):
            return "❌ <b>Translation failed</b>\nThe file seems to be corrupted or not a valid document."
        if isinstance(e, MemoryError):
            return "❌ <b>Translation failed</b>\nThe file is too large to process right now. Try splitting it or a smaller file."
        return "❌ <b>Translation failed</b>\nUnexpected error — please try again later. If it keeps happening, contact support."

    async def worker_loop(self, app: Client, n: int) -> None:
        log.info("Job worker #%d started", n)
        while True:
            try:
                job: Job = await self.queue.get()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  (should never happen — but never die)
                log.exception("worker #%d queue error: %s", n, e)
                await asyncio.sleep(1)
                continue
            try:
                if job.cancelled or job.id not in self.jobs:
                    self._cleanup(job)
                    continue
                if db.is_banned(job.user_id):
                    db.finish_job(job.id, "cancelled")
                    self._cleanup(job)
                    await safe_edit(job.status_msg, "🚫 <b>Translation cancelled.</b>")
                    continue
                if not job.file_path.exists():
                    db.finish_job(job.id, "failed")
                    self._cleanup(job)
                    await safe_edit(job.status_msg, "❌ File expired before processing. Please send it again.")
                    continue
                self.running[job.id] = job
                db._exec("UPDATE jobs SET status='running' WHERE id=?", (job.id,))
                job.task = asyncio.create_task(self._process(app, job))
                try:
                    await job.task
                except asyncio.CancelledError:
                    if not job.cancelled:
                        db.finish_job(job.id, "failed")
                        self._cleanup(job)
                        raise  # the worker loop itself is being shut down
                    db.finish_job(job.id, "cancelled")
                    await safe_edit(job.status_msg, "🚫 <b>Translation cancelled.</b>")
                except Exception as e:  # noqa: BLE001
                    log.exception("job %d failed", job.id)
                    db.finish_job(job.id, "failed")
                    await safe_edit(job.status_msg, self._user_error(e) + ("\n\n🎟 Your credit has been refunded." if job.credit and not job.credit_settled else ""))
                finally:
                    self._cleanup(job)
            finally:
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
        notes: List[str] = []

        # ── crawler clutter (novel EPUBs): synopsis/"Source"/"Generated by" intro + TOC page ──
        src_path = job.file_path
        if job.strip_extras and job.ext == ".epub":
            cleaned = job.file_path.with_name(job.file_path.stem + ".clean.epub")
            try:
                strip_notes = await asyncio.to_thread(docconv.strip_crawler_extras, job.file_path, cleaned)
                if strip_notes:
                    notes += strip_notes
                    src_path = cleaned
                else:
                    cleaned.unlink(missing_ok=True)
            except Exception as e:  # noqa: BLE001  — never fail the job because of the cleanup pass
                log.warning("job %d: strip_crawler_extras failed: %s", job.id, e)
                cleaned.unlink(missing_ok=True)

        # ── free tier: check the character budget once the document is parsed ──
        if job.free_chars > 0:
            def _quota_check(segments: int, chars: int) -> None:
                if chars > job.free_chars:
                    raise QuotaExceeded(
                        f"⏳ <b>Not enough free quota</b>\n"
                        f"This file has <b>{chars:,}</b> characters but you have <b>{job.free_chars:,}</b> left today "
                        f"(free plan: {fmt_chars(Config.FREE_DAILY_CHARS)}/day).\n\n"
                        f"Send a smaller file, come back tomorrow, or upgrade — /plans"
                    )
            epub.before_translate = _quota_check

        await epub.translate(src_path, out_path, progress)
        elapsed = time.monotonic() - t0

        # ── output format conversion ──
        final_path = out_path
        out_ext = job.out_ext if job.out_ext and not docconv.same_kind(job.out_ext, job.ext) else ""
        if out_ext:
            await safe_edit(job.status_msg, f"🔄 <b>Converting to {docconv.label_of(out_ext)}…</b>\n📄 {html.escape(job.file_name)}")
            final_path = out_path.with_suffix(out_ext)
            font = await ensure_pdf_font(self.session, job.lang) if out_ext == ".pdf" else None
            try:
                notes += await asyncio.to_thread(docconv.convert, out_path, final_path, font, job.lang)
            except docconv.ConvertError as e:
                log.warning("job %d: conversion to %s failed: %s", job.id, out_ext, e)
                notes.append(f"could not convert to {docconv.label_of(out_ext)} ({e}); sent in original format")
                final_path = out_path
                out_ext = ""

        # ── splitting ──
        parts = [final_path]
        if job.split_kb > 0:
            limit = job.split_kb * 1024
            if final_path.stat().st_size > limit:
                await safe_edit(job.status_msg, f"✂️ <b>Splitting into ≤ {docconv.fmt_size(limit)} parts…</b>\n📄 {html.escape(job.file_name)}")
                try:
                    parts = await asyncio.to_thread(docconv.split_file, final_path, limit)
                except docconv.ConvertError as e:
                    log.warning("job %d: split failed: %s", job.id, e)
                    notes.append(f"could not split ({e}); sent as one file")
                    parts = [final_path]

        # ── upload ──
        n = len(parts)
        await safe_edit(job.status_msg, f"📤 <b>Uploading {n} file{'s' if n > 1 else ''}…</b>")
        fmt_out = docconv.label_of(final_path.suffix)
        base_caption = (
            f"✅ <b>Translation complete</b>\n"
            f"📄 {html.escape(job.file_name)}" + (f" → <b>{fmt_out}</b>" if out_ext else "") + "\n"
            f"🌐 {lang_name(job.lang)} · {epub.segments:,} segments · {epub.chars:,} chars\n"
            f"⏱ {int(elapsed // 60)}m {int(elapsed % 60)}s · {int(epub.chars / max(elapsed, 1) / 1000)}k chars/s"
        )
        if notes:
            base_caption += "\nℹ️ " + "; ".join(html.escape(x) for x in notes)
        for i, part in enumerate(parts, 1):
            caption = base_caption if n == 1 else f"📦 <b>Part {i} of {n}</b> · {docconv.fmt_size(part.stat().st_size)}\n" + base_caption
            if len(caption) > 1024:
                caption = caption[:1000] + "…"
            for attempt in range(3):
                try:
                    await app.send_document(job.chat_id, str(part), caption=caption, file_name=part.name)
                    break
                except FloodWait as e:
                    await asyncio.sleep(min(e.value, 120))
                except Exception as e:  # noqa: BLE001
                    if attempt == 2:
                        raise
                    log.warning("job %d: upload part %d failed (%s), retrying", job.id, i, e)
                    await asyncio.sleep(3)
            if n > 1:
                await safe_edit(job.status_msg, f"📤 <b>Uploading…</b> {i}/{n}")
        try:
            await job.status_msg.delete()
        except Exception:
            pass
        db.finish_job(job.id, "done", epub.segments, epub.chars, elapsed, final_path.suffix, n)
        job.credit_settled = True  # keep the credit; don't refund in _cleanup
        db.record_usage(job.user_id, count_daily=not job.credit, chars=epub.chars)
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

    async def create_link(self, uid: int, plan: Plan) -> Tuple[str, str]:
        desc = (
            f"EPUB Translator {plan.title} ({plan.days} days)"
            if plan.is_sub
            else f"EPUB Translator {plan.title} ({plan.credits} credits)"
        )
        data = {
            "amount": plan.price * 100,
            "currency": "INR",
            "accept_partial": False,
            "description": desc,
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {"user_id": str(uid), "plan": plan.key},
        }
        link = await asyncio.to_thread(self.client.payment_link.create, data)
        db.add_payment(link["id"], uid, plan.price, plan.days, plan.key, plan.credits)
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
#  PAYMENTS (Telegram Stars ⭐ / XTR) — raw MTProto, no provider needed
# ═══════════════════════════════════════════════════════════════════════════
#
# Pyrogram 2.0.106 has no high-level invoice API, so we speak MTProto directly:
#   send   → messages.SendMedia(InputMediaInvoice(currency="XTR"))
#   check  → UpdateBotPrecheckoutQuery  → messages.SetBotPrecheckoutResults
#   paid   → MessageActionPaymentSentMe (service message to the bot)
# The invoice payload is a random token stored in `payments` (currency XTR);
# activation happens exactly once, when the "payment sent" update arrives.

STARS_PAYLOAD_PREFIX = "stars:"


class StarsPayments:
    @property
    def enabled(self) -> bool:
        return Config.STARS_ENABLED and "stars" in PLANS

    @staticmethod
    def _payload_id(payload: bytes) -> Optional[str]:
        try:
            s = payload.decode("utf-8")
        except Exception:  # noqa: BLE001
            return None
        return s[len(STARS_PAYLOAD_PREFIX):] if s.startswith(STARS_PAYLOAD_PREFIX) else None

    async def send_invoice(self, client: Client, chat_id: int, uid: int, plan: Plan) -> str:
        """Send a ⭐ invoice message; returns the payment id (stored as link_id)."""
        pay_id = secrets.token_urlsafe(12)
        db.add_payment(pay_id, uid, plan.price, plan.days, plan.key, plan.credits, currency="XTR")
        media = raw.types.InputMediaInvoice(
            title=f"{plan.title} — {plan.days} days"[:32],
            description=(
                f"EPUB Translator Premium for {plan.days} days: unlimited translations, "
                f"priority queue, files up to {plan.max_mb} MB."
            )[:255],
            invoice=raw.types.Invoice(
                currency="XTR",
                prices=[raw.types.LabeledPrice(label=f"{plan.title} ({plan.days} days)", amount=plan.price)],
            ),
            payload=(STARS_PAYLOAD_PREFIX + pay_id).encode(),
            provider="",                         # Stars: no provider token
            provider_data=raw.types.DataJSON(data="{}"),
            start_param=f"stars_{plan.key}",
        )
        await client.invoke(
            raw.functions.messages.SendMedia(
                peer=await client.resolve_peer(chat_id),
                media=media,
                message="",
                random_id=client.rnd_id(),
            )
        )
        return pay_id

    async def on_precheckout(self, client: Client, upd: "raw.types.UpdateBotPrecheckoutQuery") -> None:
        """Telegram asks us (≤10 s) whether the purchase may go through."""
        pay_id = self._payload_id(upd.payload)
        p = db.get_payment(pay_id) if pay_id else None
        ok = bool(
            p and p["user_id"] == upd.user_id and p["status"] != "paid"
            and upd.currency == "XTR" and upd.total_amount == p["amount"]
        )
        err = None if ok else "This invoice is no longer valid. Please open /plans and try again."
        try:
            await client.invoke(raw.functions.messages.SetBotPrecheckoutResults(query_id=upd.query_id, success=ok or None, error=err))
        except Exception as e:  # noqa: BLE001
            log.warning("SetBotPrecheckoutResults failed: %s", e)

    async def on_paid(self, client: Client, uid: int, action: "raw.types.MessageActionPaymentSentMe") -> None:
        """Successful payment → activate the plan (idempotent)."""
        pay_id = self._payload_id(action.payload)
        p = db.get_payment(pay_id) if pay_id else None
        charge_id = getattr(getattr(action, "charge", None), "id", "") or ""
        if p is None:
            log.error("Stars payment with unknown payload from %s (%s ⭐, charge %s)", uid, action.total_amount, charge_id)
            await safe_send(client, uid, "⚠️ Payment received but I could not match it to a plan. Please contact support with this id: "
                            f"<code>{html.escape(charge_id or 'n/a')}</code>")
            return
        if p["status"] == "paid":
            return  # duplicate update — already granted
        lock = _pay_locks.setdefault(pay_id, asyncio.Lock())
        async with lock:
            fresh = db.get_payment(pay_id)
            if fresh is None or fresh["status"] == "paid":
                return
            db.mark_paid(pay_id, charge_id)
            text = _activate_payment(fresh)
        _pay_locks.pop(pay_id, None)
        await safe_send(client, uid, text + "\n\n📎 Send me a file to begin.", reply_markup=main_kb(uid))
        if Config.OWNER_ID:
            plan_name = (PLANS.get(p["plan"]) or PLANS["premium"]).title
            await safe_send(client, Config.OWNER_ID, f"⭐ New Stars payment {p['amount']} ⭐ · {plan_name} from <code>{uid}</code>")


stars = StarsPayments()

# ═══════════════════════════════════════════════════════════════════════════
#  UI HELPERS
# ═══════════════════════════════════════════════════════════════════════════

# ── reply-keyboard (the "normal" buttons under the text box) ──────────────
# Sticker-style labels: a bold emoji + a short word, so the menu looks like a
# proper app bar instead of a wall of text.  Old labels are kept as aliases —
# Telegram caches reply keyboards on the phone, so a user who has not pressed
# /start since the update still taps the *old* text.
BTN_PREMIUM = "👑 Premium"
BTN_LANG = "🌐 Language"
BTN_SETTINGS = "📂 Output"
BTN_STATUS = "📊 Status"
BTN_HELP = "📖 Help"
BTN_CANCEL = "❌ Cancel"
BTN_ADMIN = "🔱 Admin"

# older cached keyboard labels → still recognised
BTN_PREMIUM_OLD = ("⭐ Premium", "💼 Plans")
BTN_LANG_OLD = ("🌐 Language",)
BTN_SETTINGS_OLD = ("⚙️ Output",)
BTN_STATUS_OLD = ("📊 Status",)
BTN_HELP_OLD = ("❓ Help",)
BTN_CANCEL_OLD = ("🚫 Cancel",)
BTN_ADMIN_OLD = ("🛠 Admin",)

ALL_BTNS = (
    BTN_PREMIUM, BTN_LANG, BTN_SETTINGS, BTN_STATUS, BTN_HELP, BTN_CANCEL, BTN_ADMIN,
    *BTN_PREMIUM_OLD, *BTN_LANG_OLD, *BTN_SETTINGS_OLD, *BTN_STATUS_OLD, *BTN_HELP_OLD, *BTN_CANCEL_OLD, *BTN_ADMIN_OLD,
)
USER_COMMANDS = ["start", "menu", "help", "lang", "status", "premium", "pay", "plans", "plan", "buy", "cancel", "settings", "output", "format", "split"]
ADMIN_COMMANDS = [
    "admin", "addworker", "delworker", "addpremium", "addplan", "addcredits", "revoke",
    "ban", "unban", "user", "broadcast", "cancel_input", "seticon", "icons",
]
KNOWN_COMMANDS = USER_COMMANDS + ADMIN_COMMANDS

# Telegram clients sometimes send an emoji with or without the U+FE0F variation
# selector (e.g. "⚙️" vs "⚙") and may add stray whitespace — normalise both sides
# so a tap on a keyboard button is always recognised, on every client.
_BTN_NORM_RE = re.compile(r"[\ufe0e\ufe0f\u200d]|\s+")


def norm_btn(text: Optional[str]) -> str:
    return _BTN_NORM_RE.sub("", text or "").strip().lower()


_ALL_BTNS_NORM = {norm_btn(b) for b in ALL_BTNS}


def is_menu_button(text: Optional[str]) -> bool:
    return norm_btn(text) in _ALL_BTNS_NORM


def btn(*labels: str):
    """Filter matching one of the reply-keyboard labels (normalised)."""
    wanted = {norm_btn(x) for x in labels}
    return filters.create(lambda _, __, m: bool(getattr(m, "text", None)) and norm_btn(m.text) in wanted, name="btn")


# ── button colours & custom-emoji icons ───────────────────────────────────
# Each menu button has a *key*; the colour is fixed here, the optional custom
# emoji icon (the animated/sticker emoji you see in Premium sticker packs) is
# stored in the DB by the admin with /seticon so it can be changed without a
# redeploy.  Telegram renders the icon *before* the button text.
#
#   colour:  "primary" = blue · "success" = green · "danger" = red · "" = white
#
BTN_KEYS: Dict[str, str] = {        # key → current label
    "premium": BTN_PREMIUM,
    "lang": BTN_LANG,
    "output": BTN_SETTINGS,
    "status": BTN_STATUS,
    "help": BTN_HELP,
    "cancel": BTN_CANCEL,
    "admin": BTN_ADMIN,
    # inline action buttons
    "start": "🚀 Start translation",
    "save": "💾 Save as default",
    "pay": "💳 Pay",
    "verify": "✅ I've paid — verify",
    "retry": "🔄 Retry",
    "join": "📢 Join channel",
    "joined": "✅ I've joined",
    "support": "💬 Contact support",
}
BTN_COLOR: Dict[str, str] = {
    "premium": "primary",
    "lang": "success",
    "output": "primary",
    "status": "success",
    "help": "primary",
    "cancel": "danger",
    "admin": "danger",
    "start": "success",
    "save": "primary",
    "pay": "primary",
    "verify": "success",
    "retry": "primary",
    "join": "primary",
    "joined": "success",
    "support": "",
}
_STYLE_OF = {"primary": ButtonStyle.PRIMARY, "success": ButtonStyle.SUCCESS, "danger": ButtonStyle.DANGER}
ICON_SETTING_PREFIX = "icon:"

# Custom-emoji icons only work when the bot owner has Telegram Premium (or the
# bot bought a Fragment username).  When Telegram rejects them we flip this
# flag and resend plain coloured buttons instead of failing the whole message.
_icons_ok = True


def icon_ids() -> Dict[str, str]:
    return {k[len(ICON_SETTING_PREFIX):]: v for k, v in db.all_settings(ICON_SETTING_PREFIX).items() if v}


def _style_kw(key: str, with_icon: bool = True) -> Dict[str, Any]:
    """kwargs for KeyboardButton / InlineKeyboardButton: colour + optional icon."""
    if not HAS_BUTTON_STYLE:
        return {}
    kw: Dict[str, Any] = {}
    color = BTN_COLOR.get(key, "")
    if color:
        kw["style"] = _STYLE_OF[color]
    if with_icon and _icons_ok:
        icon = db.get_setting(ICON_SETTING_PREFIX + key)
        if icon:
            kw["icon_custom_emoji_id"] = icon
    return kw


def kbtn(key: str, text: Optional[str] = None, with_icon: bool = True) -> KeyboardButton:
    return KeyboardButton(text or BTN_KEYS[key], **_style_kw(key, with_icon))


def ibtn(key: str, text: Optional[str] = None, with_icon: bool = True, **kw) -> InlineKeyboardButton:
    """Coloured inline button. `kw` = callback_data= / url= …"""
    return InlineKeyboardButton(text or BTN_KEYS[key], **_style_kw(key, with_icon), **kw)


def _is_icon_error(e: Exception) -> bool:
    msg = str(e).upper()
    return any(x in msg for x in ("PREMIUM_ACCOUNT_REQUIRED", "BUTTON_ICON", "ICON_INVALID", "CUSTOM_EMOJI", "DOCUMENT_INVALID", "BUTTON_STYLE"))


def main_kb(uid: int, with_icon: bool = True) -> ReplyKeyboardMarkup:
    """The persistent menu under the text box — the *only* navigation the bot
    uses.  No inline buttons are attached to Help / Status / Start / payment
    messages any more; everything is reachable from this bar.

        ┌──────────── 👑 Premium  (blue) ────────────┐
        │ 🌐 Language  (green)  │ 📂 Output  (blue)  │
        │ 📊 Status    (green)  │ 📖 Help    (blue)  │
        │ ❌ Cancel    (red)    │ (🔱 Admin  (red))  │
        └───────────────────────┴────────────────────┘
    """
    rows = [
        [kbtn("premium", with_icon=with_icon)],
        [kbtn("lang", with_icon=with_icon), kbtn("output", with_icon=with_icon)],
        [kbtn("status", with_icon=with_icon), kbtn("help", with_icon=with_icon)],
    ]
    last = [kbtn("cancel", with_icon=with_icon)]
    if Config.is_admin(uid):
        last.append(kbtn("admin", with_icon=with_icon))
    rows.append(last)
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        placeholder="📎 Send a file, or pick an option",
    )


# ── inline keyboards ──────────────────────────────────────────────────────
def chunk(items: List[InlineKeyboardButton], n: int) -> List[List[InlineKeyboardButton]]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def close_btn(data: str = "ui:close") -> InlineKeyboardButton:
    return InlineKeyboardButton("✖ Close", callback_data=data)


def lang_kb(current: str) -> InlineKeyboardMarkup:
    """Language picker — a real choice list, so it stays inline.  No Close /
    nav buttons: the message is edited into a confirmation after a tap."""
    btns = [InlineKeyboardButton(("✅ " if c == current else "") + n, callback_data=f"lang:{c}") for c, n in LANGUAGES.items()]
    return InlineKeyboardMarkup(chunk(btns, 2))


def cancel_kb(job_id: int) -> Optional[InlineKeyboardMarkup]:
    """Progress messages carry no inline button — the user cancels with the
    ❌ Cancel menu button (or /cancel).  Kept as a helper so call-sites stay
    unchanged; always returns None."""
    return None


def status_kb(uid: int) -> Optional[InlineKeyboardMarkup]:
    """No inline buttons under /status — ❌ Cancel in the menu handles both a
    waiting file and a queued/running job (see cancel_everything)."""
    return None


def support_btn() -> Optional[InlineKeyboardButton]:
    c = Config.SUPPORT_CONTACT.strip()
    if c.startswith("@") and re.fullmatch(r"@\w{5,32}", c):
        return ibtn("support", url=f"https://t.me/{c[1:]}")
    if c.startswith("https://") or c.startswith("http://"):
        return ibtn("support", url=c)
    return None


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🖥 Workers", callback_data="adm:workers"), InlineKeyboardButton("📈 Stats", callback_data="adm:stats")],
            [InlineKeyboardButton("📋 Queue", callback_data="adm:queue"), InlineKeyboardButton("🧹 Clear stuck", callback_data="adm:clear")],
            [InlineKeyboardButton("📣 Broadcast", callback_data="adm:bcast"), InlineKeyboardButton("🔄 Health check", callback_data="adm:health")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="adm:menu"), close_btn("adm:close")],
        ]
    )


def worker_id(url: str) -> str:
    """Short stable id for callback_data (URLs are too long / may reorder)."""
    return hashlib.sha1(url.encode()).hexdigest()[:10]


def worker_by_id(wid: str) -> Optional[Worker]:
    for w in pool.workers.values():
        if worker_id(w.url) == wid:
            return w
    return None


def workers_kb() -> InlineKeyboardMarkup:
    rows = []
    for i, w in enumerate(pool.workers.values(), 1):
        wid = worker_id(w.url)
        rows.append(
            [
                InlineKeyboardButton(f"{'⏸ Pause' if w.enabled else '▶ Enable'} #{i}", callback_data=f"wrk:toggle:{wid}"),
                ibtn("cancel", f"🗑 Remove #{i}", with_icon=False, callback_data=f"wrk:del:{wid}"),
            ]
        )
    rows.append([InlineKeyboardButton("➕ Add worker", callback_data="wrk:add"), InlineKeyboardButton("🔄 Refresh", callback_data="adm:workers")])
    rows.append([InlineKeyboardButton("« Back", callback_data="adm:menu"), close_btn("adm:close")])
    return InlineKeyboardMarkup(rows)


def progress_bar(pct: int, width: int = 12) -> str:
    filled = int(width * pct / 100)
    return "▰" * filled + "▱" * (width - filled)


def fmt_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y") if ts else "—"


def _strip_icons(markup):
    """Return a copy of a keyboard without custom-emoji icons (colours kept)."""
    if markup is None or not HAS_BUTTON_STYLE:
        return markup
    rows = getattr(markup, "inline_keyboard", None) or getattr(markup, "keyboard", None)
    if not rows:
        return markup
    changed = False
    for row in rows:
        for b in row:
            if getattr(b, "icon_custom_emoji_id", None):
                b.icon_custom_emoji_id = None
                changed = True
    return markup if changed else None


def _icons_rejected(e: Exception) -> bool:
    """Telegram refused the custom-emoji icons → remember it and tell the admin once."""
    global _icons_ok
    if not _icons_ok or not _is_icon_error(e):
        return False
    _icons_ok = False
    log.warning("custom-emoji button icons rejected by Telegram (%s) — falling back to plain coloured buttons. "
                "Icons need Telegram Premium on the bot owner's account.", e)
    return True


async def safe_edit(msg: Optional[Message], text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    """Edit a message; never raises (message deleted, flood-wait, network…)."""
    if msg is None:
        return
    for _ in range(3):
        try:
            await msg.edit_text(text, reply_markup=kb, **_NO_PREVIEW_KW)
            return
        except MessageNotModified:
            return
        except FloodWait as e:
            await asyncio.sleep(min(e.value, 30))
        except Exception as e:  # noqa: BLE001
            if _icons_rejected(e) and _strip_icons(kb) is not None:
                continue  # retry once without icons
            log.debug("edit failed: %s", e)
            return


async def safe_answer(cq: CallbackQuery, text: str = "", alert: bool = False) -> None:
    """Answer a callback query; ignores 'query too old' and similar errors."""
    try:
        await cq.answer(text[:200] if text else None, show_alert=alert)
    except Exception as e:  # noqa: BLE001
        log.debug("answer failed: %s", e)


async def safe_delete(msg: Optional[Message]) -> bool:
    if msg is None:
        return False
    try:
        await msg.delete()
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("delete failed: %s", e)
        return False


async def safe_reply(message: Message, text: str, **kw) -> Optional[Message]:
    """reply_text that survives flood-waits and users who blocked the bot."""
    for k, v in _NO_PREVIEW_KW.items():
        kw.setdefault(k, v)
    for _ in range(3):
        try:
            return await message.reply_text(text, **kw)
        except FloodWait as e:
            await asyncio.sleep(min(e.value, 30))
        except Exception as e:  # noqa: BLE001
            if _icons_rejected(e) and _strip_icons(kw.get("reply_markup")) is not None:
                continue  # retry once without icons
            log.debug("reply failed: %s", e)
            return None
    return None


async def safe_send(client: Client, chat_id: int, text: str, **kw) -> Optional[Message]:
    for k, v in _NO_PREVIEW_KW.items():
        kw.setdefault(k, v)
    for _ in range(3):
        try:
            return await client.send_message(chat_id, text, **kw)
        except FloodWait as e:
            await asyncio.sleep(min(e.value, 30))
        except Exception as e:  # noqa: BLE001
            if _icons_rejected(e) and _strip_icons(kw.get("reply_markup")) is not None:
                continue  # retry once without icons
            log.debug("send failed to %s: %s", chat_id, e)
            return None
    return None


ERR_TEXT = "⚠️ Something went wrong on our side. Please try again in a moment."


def guarded(fn):
    """Wrap a Pyrogram handler so an unexpected exception is logged and the
    user gets a friendly message instead of a silently spinning button.
    Pyrogram's own dispatcher only logs the traceback — the user would see
    nothing at all.  Stop/ContinuePropagation are passed through untouched."""

    @functools.wraps(fn)
    async def wrapper(client: Client, update, *a, **kw):
        try:
            return await fn(client, update, *a, **kw)
        except (StopPropagation, ContinuePropagation):
            raise
        except asyncio.CancelledError:
            raise
        except FloodWait as e:
            log.warning("%s: flood wait %ss", fn.__name__, e.value)
        except Exception as e:  # noqa: BLE001
            log.exception("handler %s crashed: %s", fn.__name__, e)
            try:
                if isinstance(update, CallbackQuery):
                    await safe_answer(update, ERR_TEXT, alert=True)
                elif isinstance(update, Message):
                    await safe_reply(update, ERR_TEXT)
            except Exception:  # noqa: BLE001
                pass

    return wrapper


def fmt_chars(n: int) -> str:
    """1_000_000 → '1M', 250_000 → '250k', 900 → '900'."""
    n = max(0, int(n))
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def free_chars_left(uid: int) -> int:
    return max(0, Config.FREE_DAILY_CHARS - db.daily_chars_used(uid))


def user_line(row: sqlite3.Row) -> str:
    """One-line plan summary shown in /start, /status, /user."""
    uid = row["id"]
    if Config.is_admin(uid):
        return "👑 Admin"
    sub = db.active_sub(uid)
    credits = db.credits(uid)
    if sub:
        line = f"{sub.emoji} {sub.title} till {fmt_dt(row['premium_until'])}"
        if sub.daily_limit:
            line += f" · {max(0, sub.daily_limit - db.daily_used(uid))}/{sub.daily_limit} left today"
    else:
        line = f"🆓 Free · {fmt_chars(free_chars_left(uid))}/{fmt_chars(Config.FREE_DAILY_CHARS)} chars left today"
    if credits:
        line += f"\n🎟 Credits: <b>{credits}</b> (never expire)"
    return line


@dataclass
class Access:
    """What the user is allowed to do with the file they just sent."""
    source: str          # "admin" | "sub" | "credit" | "free" | "blocked"
    max_mb: int
    priority: int
    reason: str = ""     # user-facing text when blocked
    max_chars: int = 0   # free tier only: characters still allowed today (0 = unmetered)


def _free_blocked_reason() -> str:
    return (
        f"⏳ Daily free limit reached ({fmt_chars(Config.FREE_DAILY_CHARS)} characters/day).\n"
        f"🎟 Get {Config.STARTER_CREDITS} credits for just ₹{Config.STARTER_PRICE_INR} (never expire) "
        + (
            f"or go unlimited with 🌟 Premium for {Config.STARS_PREMIUM_PRICE} ⭐ Stars / ₹{Config.PREMIUM_PRICE_INR} — tap <b>{BTN_PREMIUM}</b> below"
            if Config.STARS_ENABLED
            else f"or go unlimited with ⭐ Premium — tap <b>{BTN_PREMIUM}</b> below"
        )
    )


def resolve_access(uid: int) -> Access:
    """Decide which entitlement pays for the next file.

    Order: admin → active subscription with daily quota left → credits → free quota.
    File-size is checked afterwards by the caller against `max_mb`; the free tier
    is metered in *characters* per day, so its `max_chars` is checked again once
    the document has been parsed (see JobQueue._process).
    """
    if Config.is_admin(uid):
        return Access("admin", Config.PREMIUM_MAX_FILE_MB, 0)
    used = db.daily_used(uid)
    sub = db.active_sub(uid)
    if sub and (sub.daily_limit == 0 or used < sub.daily_limit):
        return Access("sub", sub.max_mb, sub.priority)
    if db.credits(uid) > 0:
        return Access("credit", Config.CREDIT_MAX_FILE_MB, 1)
    if sub is None:
        left = free_chars_left(uid)
        if left > 0:
            return Access("free", Config.FREE_MAX_FILE_MB, 2, max_chars=left)
    if sub:
        reason = (
            f"⏳ Daily limit of your {sub.emoji} <b>{sub.title}</b> plan reached ({sub.daily_limit} files).\n"
            f"Come back tomorrow, or buy a 🎟 credit pack / upgrade to ⭐ Premium — tap <b>{BTN_PREMIUM}</b> below"
        )
    else:
        reason = _free_blocked_reason()
    return Access("blocked", 0, 9, reason)


def plans_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [[ibtn("premium", p.button, with_icon=False, callback_data=f"plan:{p.key}")] for p in PLANS.values()]
    return InlineKeyboardMarkup(rows)


def plans_text(row: sqlite3.Row) -> str:
    lines = [
        "👑 <b>Premium Plans & Pricing</b>\n",
        f"Your plan: {user_line(row)}\n",
        f"🆓 <b>Free</b> — {fmt_chars(Config.FREE_DAILY_CHARS)} characters/day · up to {Config.FREE_MAX_FILE_MB} MB\n",
    ]
    for p in PLANS.values():
        if p.is_sub:
            limit = "unlimited" if p.daily_limit == 0 else f"{p.daily_limit} files/day"
            lines.append(f"{p.emoji} <b>{p.title}</b> — {p.price_label} · {p.days} days · {limit} · {p.max_mb} MB")
        else:
            lines.append(f"{p.emoji} <b>{p.title}</b> — {p.price_label} · {p.credits} files · never expires · {p.max_mb} MB")
        if p.tagline:
            lines.append(f"    <i>{p.tagline}</i>")
    lines.append("\n💡 Credits are used only after your daily quota is finished, so they are never wasted.")
    lines.append("👇 Tap a plan below to see details and pay.")
    return "\n".join(lines)


def plan_detail_text(p: Plan) -> str:
    head = f"{p.emoji} <b>{p.title}</b> — {p.price_label}"
    if p.is_sub:
        head += f" / {p.days} days"
    body = "\n".join(f"• {x}" for x in p.features())
    tag = f"\n<i>{p.tagline}</i>" if p.tagline else ""
    if p.is_stars:
        how = "Tap <b>Pay</b> — Telegram opens its own ⭐ Stars checkout. Activation is instant and automatic."
    else:
        how = "Pay via the button, then tap <b>verify</b>. Activation is instant."
    return f"{head}{tag}\n\n{body}\n\n{how}"


# per-admin pending input (e.g. waiting for worker URL / broadcast text)
pending_input: Dict[int, str] = {}
# users whose file is currently being downloaded (slot reserved, job not yet queued)
downloading: set = set()


# ── output options (format + split) ────────────────────────────────────────
@dataclass
class PendingFile:
    """A downloaded file waiting for the user to confirm output options."""
    uid: int
    chat_id: int
    path: Path
    name: str
    ext: str
    lang: str
    status: Message
    out_ext: str = ""          # '' = same as input
    split_kb: int = 0
    strip_extras: bool = True  # drop crawler summary / Source / TOC pages (EPUB)
    awaiting_custom: bool = False
    timer: Optional[asyncio.Task] = None
    deadline: float = 0.0      # monotonic time when the panel auto-starts
    starting: bool = False     # start_job already running (guards double taps / timer race)


pending_files: Dict[int, PendingFile] = {}

_SIZE_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(kb|k|mb|m|gb|g)?\s*$", re.I)


def parse_size_kb(text: str) -> Optional[int]:
    """'500kb' / '25 MB' / '1.5g' / '20' (MB) → KB, or None if unparseable."""
    m = _SIZE_RE.match(text or "")
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "mb").lower()[0]
    mult = {"k": 1, "m": 1024, "g": 1024 * 1024}[unit]
    return int(num * mult)


def fmt_kb(kb: int) -> str:
    return docconv.fmt_size(kb * 1024)


def out_format_label(out_ext: str, in_ext: str = "") -> str:
    if not out_ext or (in_ext and docconv.same_kind(out_ext, in_ext)):
        return f"{docconv.label_of(in_ext)} (same as input)" if in_ext else "same as input"
    return docconv.label_of(out_ext)


def split_label(kb: int) -> str:
    return "no split" if kb <= 0 else f"≤ {fmt_kb(kb)} per file"


def options_kb(prefix: str, out_ext: str, split_kb: int, in_ext: str = "", ask: Optional[bool] = None, strip: bool = True) -> InlineKeyboardMarkup:
    """Shared keyboard for the per-file panel (prefix 'opt') and /settings ('set')."""
    fmts = docconv.available_outputs()
    rows: List[List[InlineKeyboardButton]] = []
    same_sel = not out_ext or bool(in_ext and docconv.same_kind(out_ext, in_ext))
    # ── format ──
    rows.append([InlineKeyboardButton(("✅ " if same_sel else "") + "📄 Same as input", callback_data=f"{prefix}:fmt:same")])
    fbtns = [
        InlineKeyboardButton(
            ("✅ " if (bool(out_ext) and not same_sel and docconv.same_kind(ext, out_ext)) else "") + docconv.label_of(ext),
            callback_data=f"{prefix}:fmt:{ext}",
        )
        for ext in fmts
    ]
    rows += chunk(fbtns, 3)
    # ── split ──
    presets = {mb * 1024 for mb in Config.SPLIT_PRESETS_MB}
    custom_sel = split_kb > 0 and split_kb not in presets
    sbtns = [InlineKeyboardButton(("✅ " if split_kb <= 0 else "") + "✂️ No split", callback_data=f"{prefix}:split:0")]
    for mb in Config.SPLIT_PRESETS_MB:
        kb = mb * 1024
        sbtns.append(InlineKeyboardButton(("✅ " if split_kb == kb else "") + f"{mb} MB", callback_data=f"{prefix}:split:{kb}"))
    sbtns.append(InlineKeyboardButton(("✅ " if custom_sel else "") + ("✏️ Custom" + (f" ({fmt_kb(split_kb)})" if custom_sel else "…")), callback_data=f"{prefix}:custom"))
    rows += chunk(sbtns, 3)
    # ── crawler clutter (EPUB) ──
    if not in_ext or in_ext == ".epub":
        rows.append([InlineKeyboardButton(
            f"🧹 Remove summary / Source / TOC pages: {'✅ ON' if strip else '❌ OFF'}", callback_data=f"{prefix}:strip",
        )])
    # ── actions ──
    if prefix == "opt":
        rows.append([ibtn("start", callback_data="opt:start")])
        rows.append([ibtn("save", callback_data="opt:save"), ibtn("cancel", callback_data="opt:cancel")])
    else:
        rows.append([InlineKeyboardButton(f"💬 Ask for every file: {'✅ ON' if ask else '❌ OFF'}", callback_data="set:ask")])
        rows.append([InlineKeyboardButton("↩️ Reset defaults", callback_data="set:reset")])
    return InlineKeyboardMarkup(rows)


def options_text(pf: PendingFile) -> str:
    try:
        size = pf.path.stat().st_size
    except OSError:
        size = 0
    left = max(0, int(pf.deadline - time.monotonic())) if pf.deadline else Config.OPTIONS_TIMEOUT
    return (
        "⚙️ <b>Output options</b>\n"
        f"📄 {html.escape(pf.name)} · {docconv.fmt_size(size)}\n"
        f"🌐 → {lang_name(pf.lang)}\n\n"
        f"📤 Format: <b>{out_format_label(pf.out_ext, pf.ext)}</b>\n"
        f"✂️ Split: <b>{split_label(pf.split_kb)}</b>\n"
        + (f"🧹 Crawler pages (summary/Source/TOC): <b>{'remove' if pf.strip_extras else 'keep'}</b>\n" if pf.ext == ".epub" else "")
        + "\nPick a format / split size, then tap <b>▶️ Start</b>. "
        f"Starts automatically in ~{left}s."
    )


def _ask_on(row: sqlite3.Row) -> bool:
    v = row["ask_options"]
    return bool(v) if v is not None else True


def _strip_on(row: sqlite3.Row) -> bool:
    try:
        v = row["strip_extras"]
    except (IndexError, KeyError):
        return True
    return bool(v) if v is not None else True


def settings_kb_for(row: sqlite3.Row) -> InlineKeyboardMarkup:
    return options_kb("set", row["out_format"] or "", int(row["split_kb"] or 0), ask=_ask_on(row), strip=_strip_on(row))


def settings_text(row: sqlite3.Row) -> str:
    return (
        "⚙️ <b>Output settings</b> (defaults for every file)\n\n"
        f"📤 Format: <b>{out_format_label(row['out_format'] or '')}</b>\n"
        f"✂️ Split: <b>{split_label(int(row['split_kb'] or 0))}</b>\n"
        f"🧹 Remove crawler pages: <b>{'ON' if _strip_on(row) else 'OFF'}</b>\n"
        f"💬 Ask for every file: <b>{'ON' if _ask_on(row) else 'OFF'}</b>\n\n"
        "• <b>Format</b> — get the translation back as EPUB, PDF, DOCX, TXT or HTML regardless of what you send.\n"
        "• <b>Split</b> — big results are cut into several files no larger than the chosen size "
        "(EPUB by chapters, PDF by pages, DOCX/HTML/TXT by paragraphs).\n"
        "• <b>Remove crawler pages</b> — novel EPUBs from crawler bots start with a summary/synopsis page, "
        "“Source: …” / “Generated by …” lines and a table-of-contents page. ON removes them so the book "
        "starts at chapter 1 (great for listening with TTS).\n"
        "• <b>Ask</b> — OFF = files start immediately with these defaults."
    )

# ═══════════════════════════════════════════════════════════════════════════
#  BOT
# ═══════════════════════════════════════════════════════════════════════════

# One explicit event loop for the whole process.  kurigram schedules every
# internal task (dispatcher, Session.recv_worker, ping_worker, add_handler…)
# on ``client.loop`` and resolves that lazily via asyncio.get_event_loop().
# If the bot were later driven by ``asyncio.run()`` (which spins up a *new*
# loop) those tasks would land on the wrong loop and die with
# "Task was destroyed but it is pending" / "coroutine … was never awaited".
# Creating the loop here and passing it to the Client keeps everything on
# the same loop; ``_run_bot()`` below drives that very loop.  It also avoids
# the deprecated implicit-loop lookup on Python >= 3.12.
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

app = Client(
    Config.SESSION_NAME,
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    workdir=str(Config.DATA_DIR),
    parse_mode=ParseMode.HTML,
    loop=LOOP,
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


_invite_cache: Dict[str, str] = {}


async def force_sub_link(client: Client) -> Optional[str]:
    """Public @username → t.me link; private numeric id → exported invite link (cached)."""
    ch = Config.FORCE_SUB_CHANNEL
    if not ch:
        return None
    if ch.startswith("@"):
        return f"https://t.me/{ch.lstrip('@')}"
    if ch in _invite_cache:
        return _invite_cache[ch]
    try:
        chat = await client.get_chat(_chat_ref(ch))
        link = getattr(chat, "invite_link", None) or (f"https://t.me/{chat.username}" if getattr(chat, "username", None) else None)
        if not link:
            link = await client.export_chat_invite_link(_chat_ref(ch))
        if link:
            _invite_cache[ch] = link
        return link
    except Exception as e:  # noqa: BLE001
        log.warning("cannot build force-sub invite link: %s", e)
        return None


async def guard(client: Client, message: Message) -> Optional[sqlite3.Row]:
    """Common per-message checks. Returns user row or None if blocked."""
    u = message.from_user
    if not u:
        return None
    row = db.upsert_user(u.id, u.first_name or "", u.username)
    if row["banned"]:
        await safe_reply(message, "🚫 You are banned from using this bot.")
        return None
    if not await check_force_sub(client, u.id):
        link = await force_sub_link(client)
        rows = [[ibtn("join", url=link)]] if link else []
        rows.append([ibtn("joined", callback_data="ui:recheck")])
        await safe_reply(message, "📢 Please join our channel first, then tap <b>I've joined</b>.", reply_markup=InlineKeyboardMarkup(rows))
        return None
    return row


async def cq_user(cq: CallbackQuery) -> sqlite3.Row:
    """Upsert + return the user row behind a callback query."""
    return db.upsert_user(cq.from_user.id, cq.from_user.first_name or "", cq.from_user.username)


def start_text(row: sqlite3.Row, first_name: str) -> str:
    return (
        f"👋 <b>Welcome, {html.escape(first_name or 'there')}!</b>\n\n"
        "I translate <b>EPUB · PDF · DOCX · TXT · HTML</b> files into your language while keeping "
        "the original formatting, images and chapters intact.\n\n"
        f"🌐 Target language: <b>{lang_name(row['lang'])}</b>\n"
        f"👑 Plan: {user_line(row)}\n\n"
        "📎 <b>Send me a file to begin</b>, or use the menu buttons below."
    )


# ── /start ─────────────────────────────────────────────────────────────────
@app.on_message(filters.private & filters.command(["start", "menu"]))
@guarded
async def cmd_start(client: Client, message: Message) -> None:
    row = await guard(client, message)
    if not row:
        return
    await safe_reply(message, start_text(row, message.from_user.first_name), reply_markup=main_kb(message.from_user.id))


def help_text() -> str:
    return (
        "📖 <b>How it works</b>\n"
        f"1. Choose your language with <b>{BTN_LANG}</b>\n"
        "2. Send a file: <b>.epub · .pdf · .docx · .txt · .md · .html</b>\n"
        "3. Choose the <b>output format</b> (EPUB · PDF · DOCX · TXT · HTML) and an optional <b>split size</b>\n"
        "4. Watch live progress and receive the file(s)\n\n"
        f"<b>{BTN_SETTINGS}</b> — /settings: default format, split size (e.g. ≤ 20 MB per part), "
        "and whether to ask before every file\n\n"
        "✨ <b>What's preserved</b>\n"
        "• EPUB/HTML: chapters, bold/italic, links, images, TOC, CSS\n"
        "• DOCX: styles, tables, images, headers/footers, footnotes\n"
        "• PDF: page layout, images — text is replaced in place (scanned PDFs not supported)\n"
        "• TXT/MD: line breaks and indentation\n\n"
        f"🆓 Free: {fmt_chars(Config.FREE_DAILY_CHARS)} characters/day (any number of files), up to {Config.FREE_MAX_FILE_MB} MB\n"
        f"🎟 Starter Pack ₹{Config.STARTER_PRICE_INR}: {Config.STARTER_CREDITS} files, never expire, up to {Config.CREDIT_MAX_FILE_MB} MB\n"
        f"🔹 Basic ₹{Config.BASIC_PRICE_INR}: {Config.BASIC_DAILY_LIMIT} files/day for {Config.BASIC_DAYS} days, up to {Config.BASIC_MAX_FILE_MB} MB\n"
        f"⭐ Premium ₹{Config.PREMIUM_PRICE_INR}: unlimited, priority queue, up to {Config.PREMIUM_MAX_FILE_MB} MB\n"
        + (f"🌟 Premium · Stars {Config.STARS_PREMIUM_PRICE} ⭐: same as Premium, {Config.STARS_PREMIUM_DAYS} days, paid with Telegram Stars\n" if Config.STARS_ENABLED else "")
        + f"→ all plans: tap <b>{BTN_PREMIUM}</b> below or /plans\n\n"
        "🧹 <b>Novel EPUBs</b> — crawler pages (synopsis/summary, “Source”, “Generated by”, table-of-contents page) are removed automatically "
        "so audiobook/TTS readers start at chapter 1. Toggle in /settings.\n\n"
        "<b>Commands</b>\n"
        "/start · /help · /lang · /settings · /status · /plans · /cancel\n\n"
        f"💬 Support: {html.escape(Config.SUPPORT_CONTACT)}"
    )


# ── /help ──────────────────────────────────────────────────────────────────
@app.on_message(filters.private & (filters.command("help") | btn(BTN_HELP, *BTN_HELP_OLD)))
@guarded
async def cmd_help(client: Client, message: Message) -> None:
    if not await guard(client, message):
        return
    # no inline buttons here — the persistent menu already has every option
    await safe_reply(message, help_text(), reply_markup=main_kb(message.from_user.id))


# ── language ───────────────────────────────────────────────────────────────
@app.on_message(filters.private & (filters.command("lang") | btn(BTN_LANG, *BTN_LANG_OLD)))
@guarded
async def cmd_lang(client: Client, message: Message) -> None:
    row = await guard(client, message)
    if not row:
        return
    await safe_reply(message, f"🌐 <b>Choose target language</b>\nCurrent: <b>{lang_name(row['lang'])}</b>", reply_markup=lang_kb(row["lang"]))


@app.on_callback_query(filters.regex(r"^lang:(.+)$"))
@guarded
async def cb_lang(client: Client, cq: CallbackQuery) -> None:
    code = cq.matches[0].group(1)
    if code not in LANGUAGES:
        return await safe_answer(cq, "Unknown language", alert=True)
    await cq_user(cq)
    db.set_lang(cq.from_user.id, code)
    # a file waiting on the options panel should follow the new language too
    pf = pending_files.get(cq.from_user.id)
    if pf:
        pf.lang = code
        await safe_edit(pf.status, options_text(pf), options_kb("opt", pf.out_ext, pf.split_kb, pf.ext, strip=pf.strip_extras))
    await safe_answer(cq, f"Language set: {lang_name(code)}")
    await safe_edit(
        cq.message,
        f"✅ Target language: <b>{lang_name(code)}</b>\n\n📎 Now send me a file (EPUB · PDF · DOCX · TXT · HTML).",
    )


# ── status ─────────────────────────────────────────────────────────────────
def status_text(row: sqlite3.Row) -> str:
    uid = row["id"]
    mine = ""
    jid = jobs.by_user.get(uid)
    if jid:
        pos = jobs.position(jid)
        mine = "\n\n📌 <b>Your file</b>: " + ("⚙️ processing now" if jid in jobs.running else f"⏳ queue position #{pos}")
        mine += f"\n↩️ To stop it, tap <b>{BTN_CANCEL}</b> below."
    elif uid in pending_files:
        mine = f"\n\n📌 <b>Your file</b>: waiting for you to pick output options\n↩️ To discard it, tap <b>{BTN_CANCEL}</b> below."
    return (
        "📊 <b>Status</b>\n"
        f"🌐 Language: <b>{lang_name(row['lang'])}</b>\n"
        f"👑 Plan: {user_line(row)}\n"
        f"📚 Files translated: {row['total_files']}\n\n"
        f"🖥 Workers online: {len(pool.available())}/{len(pool.workers)}\n"
        f"⚙️ Processing: {len(jobs.running)} · Queued: {jobs.queued_count()}" + mine
    )


@app.on_message(filters.private & (filters.command("status") | btn(BTN_STATUS, *BTN_STATUS_OLD)))
@guarded
async def cmd_status(client: Client, message: Message) -> None:
    row = await guard(client, message)
    if not row:
        return
    await safe_reply(message, status_text(row), reply_markup=status_kb(row["id"]))


# ── generic UI callbacks (close / open panels from inline buttons) ─────────
@app.on_callback_query(filters.regex(r"^ui:(\w+)$"))
@guarded
async def cb_ui(client: Client, cq: CallbackQuery) -> None:
    action = cq.matches[0].group(1)
    row = await cq_user(cq)
    if action == "close":
        await safe_answer(cq)
        if not await safe_delete(cq.message):
            await safe_edit(cq.message, "✖ Closed.")
        return
    if row["banned"]:
        return await safe_answer(cq, "🚫 You are banned from using this bot.", alert=True)
    if action == "recheck":
        if await check_force_sub(client, cq.from_user.id):
            await safe_answer(cq, "✅ Thanks for joining!")
            await safe_edit(cq.message, start_text(row, cq.from_user.first_name or ""))
            if cq.message:
                await safe_send(client, cq.message.chat.id, "📎 Send me a file to begin.", reply_markup=main_kb(cq.from_user.id))
        else:
            await safe_answer(cq, "❌ You have not joined yet.", alert=True)
        return
    await safe_answer(cq)
    if action == "lang":
        await safe_edit(cq.message, f"🌐 <b>Choose target language</b>\nCurrent: <b>{lang_name(row['lang'])}</b>", lang_kb(row["lang"]))
    elif action == "settings":
        await safe_edit(cq.message, settings_text(row), settings_kb_for(row))
    elif action == "status":
        await safe_edit(cq.message, status_text(row), status_kb(row["id"]))
    elif action == "help":
        await safe_edit(cq.message, help_text())
    elif action == "start":
        await safe_edit(cq.message, start_text(row, cq.from_user.first_name or ""))
    else:
        await safe_answer(cq, "Unknown action.", alert=True)


# ── plans / premium ────────────────────────────────────────────────────────
@app.on_message(filters.private & (filters.command(["premium", "pay", "plans", "plan", "buy"]) | btn(BTN_PREMIUM, *BTN_PREMIUM_OLD)))
@guarded
async def cmd_premium(client: Client, message: Message) -> None:
    row = await guard(client, message)
    if not row:
        return
    await safe_reply(message, plans_text(row), reply_markup=plans_kb(message.from_user.id))


@app.on_callback_query(filters.regex(r"^plans$"))
@guarded
async def cb_plans_menu(client: Client, cq: CallbackQuery) -> None:
    row = await cq_user(cq)
    await safe_answer(cq)
    await safe_edit(cq.message, plans_text(row), plans_kb(cq.from_user.id))


@app.on_callback_query(filters.regex(r"^plan:(\w+)$"))
@guarded
async def cb_plan_detail(client: Client, cq: CallbackQuery) -> None:
    plan = PLANS.get(cq.matches[0].group(1))
    if not plan:
        return await safe_answer(cq, "Unknown plan.", alert=True)
    uid = cq.from_user.id
    await cq_user(cq)
    # no « Back / ✖ Close inline buttons — the user re-opens the list with the
    # 👑 Premium menu button; only *action* buttons (pay / verify / retry) stay inline
    back_hint = f"\n\n↩️ Other plans: tap <b>{BTN_PREMIUM}</b> below."

    # unlimited users don't need a smaller sub — but credit packs/extension are always allowed
    cur = db.active_sub(uid)
    if plan.is_sub and cur and cur.daily_limit == 0 and plan.daily_limit != 0:
        return await safe_answer(cq, f"You already have {cur.title} (unlimited) — no need for {plan.title}.", alert=True)

    # ⭐ Telegram Stars: the invoice is a Telegram-native message, no external link
    if plan.is_stars:
        if not stars.enabled or cq.message is None:
            return await safe_answer(cq, "Stars payments are not available right now.", alert=True)
        await safe_answer(cq, "Sending invoice…")
        try:
            await stars.send_invoice(client, cq.message.chat.id, uid, plan)
        except Exception as e:  # noqa: BLE001
            log.error("stars invoice error: %s", e)
            return await safe_edit(
                cq.message,
                plan_detail_text(plan) + "\n\n⚠️ Could not create the Stars invoice. Please try again in a minute." + back_hint,
                InlineKeyboardMarkup([[ibtn("retry", callback_data=f"plan:{plan.key}")]]),
            )
        return await safe_edit(
            cq.message,
            plan_detail_text(plan) + "\n\n👇 The invoice is below — tap <b>Pay</b> on it. Your plan activates automatically the moment Telegram confirms." + back_hint,
        )

    if not payments.enabled:
        await safe_answer(cq)
        rows: List[List[InlineKeyboardButton]] = []
        sb = support_btn()
        if sb:
            rows.append([sb])
        hint = f"\n\n💬 Payments are handled manually — contact {html.escape(Config.SUPPORT_CONTACT)} to buy."
        if stars.enabled:
            hint += f"\n🌟 Or pay instantly with Telegram Stars: <b>{PLANS['stars'].button}</b>"
            rows.insert(0, [InlineKeyboardButton(PLANS["stars"].button, callback_data="plan:stars")])
        return await safe_edit(cq.message, plan_detail_text(plan) + hint + back_hint, InlineKeyboardMarkup(rows) if rows else None)
    # answer first: creating a Razorpay link can take a few seconds and the
    # callback would otherwise time out (button spins forever)
    await safe_answer(cq, "Creating payment link…")
    try:
        link_id, url = await payments.create_link(uid, plan)
    except Exception as e:  # noqa: BLE001
        log.error("payment link error: %s", e)
        return await safe_edit(
            cq.message,
            plan_detail_text(plan) + "\n\n⚠️ Payment service temporarily unavailable. Please try again in a few minutes." + back_hint,
            InlineKeyboardMarkup([[ibtn("retry", callback_data=f"plan:{plan.key}")]]),
        )
    kb = InlineKeyboardMarkup(
        [
            [ibtn("pay", f"💳 Pay ₹{plan.price}", url=url)],
            [ibtn("verify", callback_data=f"pay:{link_id}")],
        ]
    )
    await safe_edit(cq.message, plan_detail_text(plan) + back_hint, kb)


def _activate_payment(p: sqlite3.Row) -> str:
    """Grant whatever the paid row represents. Returns the user-facing success text."""
    plan = PLANS.get(p["plan"] or "premium")
    uid = p["user_id"]
    if plan is None:  # plan removed from catalogue after purchase → honour as premium days
        until = db.add_subscription(uid, "premium", p["days"] or Config.PREMIUM_DAYS)
        return f"🎉 <b>Premium activated!</b>\nValid till <b>{fmt_dt(until)}</b>."
    if plan.is_sub:
        until = db.add_subscription(uid, sub_plan_for_grant(plan.key), p["days"] or plan.days)
        limit = "Enjoy unlimited translations." if plan.daily_limit == 0 else f"{plan.daily_limit} files every day, up to {plan.max_mb} MB."
        return f"🎉 <b>{plan.emoji} {plan.title} activated!</b>\nValid till <b>{fmt_dt(until)}</b>. {limit}"
    total = db.add_credits(uid, p["units"] or plan.credits)
    return (
        f"🎉 <b>{plan.emoji} {plan.title} activated!</b>\n"
        f"+{p['units'] or plan.credits} credits → you now have <b>{total}</b>.\n"
        "They never expire — send a file whenever you like."
    )


# one verification at a time per payment link (double-tap protection)
_pay_locks: Dict[str, asyncio.Lock] = {}


@app.on_callback_query(filters.regex(r"^pay:(.+)$"))
@guarded
async def cb_pay(client: Client, cq: CallbackQuery) -> None:
    link_id = cq.matches[0].group(1)
    p = db.get_payment(link_id)
    if not p or p["user_id"] != cq.from_user.id:
        return await safe_answer(cq, "Payment not found.", alert=True)
    if p["status"] == "paid":
        return await safe_answer(cq, "Already activated ✅", alert=True)
    if (p["currency"] or "INR") == "XTR":
        return await safe_answer(cq, "Stars payments activate automatically — just pay the invoice.", alert=True)
    if not payments.enabled:
        return await safe_answer(cq, "Payment service is not configured.", alert=True)
    lock = _pay_locks.setdefault(link_id, asyncio.Lock())
    if lock.locked():
        return await safe_answer(cq, "Verifying… please wait.")
    async with lock:
        # a callback query can only be answered ONCE — so verify first, answer after
        paid = await payments.verify(link_id)
        if not paid:
            return await safe_answer(cq, "Payment not received yet. Complete the payment and try again in a minute.", alert=True)
        # re-check: two quick taps must not grant twice
        fresh = db.get_payment(link_id)
        if fresh is None or fresh["status"] == "paid":
            return await safe_answer(cq, "Already activated ✅", alert=True)
        db.mark_paid(link_id)
        text = _activate_payment(p)
    _pay_locks.pop(link_id, None)
    await safe_answer(cq, "Payment verified ✅")
    await safe_edit(cq.message, text + "\n\n📎 Send me a file to begin.")
    if Config.OWNER_ID:
        plan_name = (PLANS.get(p["plan"]) or PLANS["premium"]).title
        await safe_send(
            client,
            Config.OWNER_ID,
            f"💰 New payment ₹{p['amount']} · {plan_name} from <code>{p['user_id']}</code> (@{cq.from_user.username or '-'})",
        )


# ── Telegram Stars: pre-checkout + successful payment (raw MTProto updates) ──
def _peer_user_id(peer) -> int:
    return int(getattr(peer, "user_id", 0) or 0)


@app.on_raw_update(group=-1)
async def on_raw_payment_update(client: Client, update, users, chats) -> None:
    try:
        if isinstance(update, raw.types.UpdateBotPrecheckoutQuery):
            await stars.on_precheckout(client, update)
            return
        if isinstance(update, (raw.types.UpdateNewMessage, raw.types.UpdateNewChannelMessage)):
            msg = update.message
            if isinstance(msg, raw.types.MessageService) and isinstance(msg.action, raw.types.MessageActionPaymentSentMe):
                uid = _peer_user_id(msg.from_id) or _peer_user_id(msg.peer_id)
                if uid:
                    await stars.on_paid(client, uid, msg.action)
    except Exception as e:  # noqa: BLE001  — raw handlers must never take the dispatcher down
        log.exception("payment update handler failed: %s", e)
    # not our business → let the normal parsed handlers run
    raise ContinuePropagation


# ── cancel ─────────────────────────────────────────────────────────────────
async def cancel_everything(uid: int) -> str:
    """Discard a waiting file and/or cancel the queued/running job. Returns user text."""
    pf = pending_files.get(uid)
    if pf:
        _discard_pending(pf)
        shutil.rmtree(pf.path.parent, ignore_errors=True)
        await safe_edit(pf.status, "🚫 <b>Cancelled.</b>")
        return "🚫 File discarded. Send another one whenever you like."
    jid = jobs.by_user.get(uid)
    if jid and jobs.cancel(jid):
        return "🚫 Your translation is being cancelled."
    if uid in downloading:
        return "📥 Your file is still downloading — try again in a few seconds."
    return "ℹ️ You have no active translation right now."


@app.on_message(filters.private & (filters.command("cancel") | btn(BTN_CANCEL, *BTN_CANCEL_OLD)))
@guarded
async def cmd_cancel(client: Client, message: Message) -> None:
    if not message.from_user:
        return
    await safe_reply(message, await cancel_everything(message.from_user.id))


@app.on_callback_query(filters.regex(r"^cancel:(\d+)$"))
@guarded
async def cb_cancel(client: Client, cq: CallbackQuery) -> None:
    jid = int(cq.matches[0].group(1))
    job = jobs.jobs.get(jid)
    if not job:
        await safe_answer(cq, "This job has already finished.", alert=True)
        # remove the stale button so the user does not keep tapping it
        try:
            if cq.message and cq.message.reply_markup:
                await cq.message.edit_reply_markup(None)
        except Exception:  # noqa: BLE001
            pass
        return
    if job.user_id != cq.from_user.id and not Config.is_admin(cq.from_user.id):
        return await safe_answer(cq, "Not your job.", alert=True)
    jobs.cancel(jid)
    await safe_answer(cq, "Cancelling…")
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
@guarded
async def on_document(client: Client, message: Message) -> None:
    row = await guard(client, message)
    if not row:
        return
    uid = message.from_user.id
    # admin is sending a broadcast attachment → leave it to on_admin_input.
    # (Only the *admin* modes — a user waiting to type a custom split size
    # must still be able to send a file; previously the file was silently ignored.)
    if pending_input.get(uid) in ("broadcast", "add_worker"):
        return
    doc = message.document
    if doc is None:
        return
    ext = detect_format(doc.file_name or "", doc.mime_type)
    if not ext:
        return await safe_reply(
            message,
            "⚠️ Unsupported file type.\n\nSupported: <b>" + " · ".join(sorted(SUPPORTED_FORMATS)) + "</b>",
        )
    if ext == ".pdf" and pymupdf is None:
        return await safe_reply(message, "⚠️ PDF support is not installed on this server.")
    name = doc.file_name or f"document{ext}"
    if not name.lower().endswith(ext):
        name += ext

    access = resolve_access(uid)
    if access.source == "blocked":
        return await safe_reply(message, access.reason, reply_markup=main_kb(uid))
    file_size = doc.file_size or 0
    if file_size > access.max_mb * 1024 * 1024:
        hint = ""
        if access.source != "admin" and access.max_mb < Config.PREMIUM_MAX_FILE_MB:
            hint = f"\n⭐ Premium allows files up to {Config.PREMIUM_MAX_FILE_MB} MB — tap <b>{BTN_PREMIUM}</b> below."
        return await safe_reply(
            message,
            f"⚠️ File too large ({file_size / 1048576:.1f} MB). Limit for your plan: <b>{access.max_mb} MB</b>." + hint,
            reply_markup=main_kb(uid),
        )
    if file_size > Config.TG_MAX_FILE_MB * 1024 * 1024:
        return await safe_reply(message, f"⚠️ Telegram bots can only download files up to {Config.TG_MAX_FILE_MB} MB.")
    if jobs.user_has_job(uid) or uid in downloading or uid in pending_files:
        return await safe_reply(
            message,
            f"⚠️ You already have a file in progress. Tap <b>{BTN_CANCEL}</b> first if you want to send another one.",
            reply_markup=main_kb(uid),
        )
    if not pool.available() and not Config.DIRECT_FALLBACK and Config.DIRECT_CONCURRENCY <= 0:
        return await safe_reply(message, "⚠️ Translation service is offline right now. Please try again later.")

    # reserve the user's slot *before* the (slow) download so two files sent
    # back-to-back cannot both slip past the "one job per user" check
    downloading.add(uid)
    status: Optional[Message] = None
    tmp_dir: Optional[Path] = None
    try:
        status = await safe_reply(message, "📥 <b>Downloading…</b>")
        if status is None:  # user blocked the bot / chat unavailable
            return
        tmp_dir = Path(tempfile.mkdtemp(prefix="epub_", dir=Config.DATA_DIR))
        safe_name = re.sub(r"[^\w.\- ]", "_", name).strip(" ._") or f"document{ext}"
        if not safe_name.lower().endswith(ext):
            safe_name += ext
        safe_name = safe_name[-120:]  # keep the path short on every filesystem
        try:
            path = await message.download(file_name=str(tmp_dir / safe_name))
        except Exception as e:  # noqa: BLE001
            log.warning("download failed for %s: %s", uid, e)
            path = None
        if not path or not _sniff_ok(Path(path), ext):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            await safe_edit(status, f"❌ Download failed or this is not a valid {SUPPORTED_FORMATS[ext][1]} file.")
            return
        # user cancelled (/cancel) or got banned while the file was downloading
        fresh = db.get_user(uid)
        if fresh is None or fresh["banned"]:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            await safe_edit(status, "🚫 <b>Cancelled.</b>")
            return

        pf = PendingFile(
            uid=uid, chat_id=message.chat.id, path=Path(path), name=name, ext=ext, lang=row["lang"], status=status,
            out_ext=row["out_format"] or "", split_kb=int(row["split_kb"] or 0), strip_extras=_strip_on(row),
        )
        if not _ask_on(row):
            await start_job(pf)                     # defaults, no questions asked
            return
        pending_files[uid] = pf
        pf.deadline = time.monotonic() + Config.OPTIONS_TIMEOUT
        pf.timer = asyncio.create_task(_options_timeout(uid))
        await safe_edit(status, options_text(pf), options_kb("opt", pf.out_ext, pf.split_kb, pf.ext, strip=pf.strip_extras))
    except Exception as e:  # noqa: BLE001
        # never leave the user's slot reserved forever if anything above blew up
        # (previously an unexpected error here meant "You already have a file in
        # progress" until the bot was restarted)
        log.exception("on_document failed for %s: %s", uid, e)
        pf = pending_files.pop(uid, None)
        if pf and pf.timer and not pf.timer.done():
            pf.timer.cancel()
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        await safe_edit(status, "❌ Something went wrong while receiving the file. Please try again.")
        return
    finally:
        downloading.discard(uid)


async def _options_timeout(uid: int) -> None:
    """Auto-start with the current selection when the user does not answer.
    While the user is typing a custom size the deadline is pushed back instead
    of leaving the file (and the user's slot) waiting forever."""
    try:
        while True:
            pf = pending_files.get(uid)
            if pf is None:
                return
            wait = pf.deadline - time.monotonic()
            if wait > 0:
                await asyncio.sleep(min(wait, 5))
                continue
            if pf.awaiting_custom:
                # give them one more window, then start anyway
                pf.awaiting_custom = False
                pf.deadline = time.monotonic() + Config.OPTIONS_TIMEOUT
                await safe_edit(pf.status, options_text(pf), options_kb("opt", pf.out_ext, pf.split_kb, pf.ext, strip=pf.strip_extras))
                continue
            break
    except asyncio.CancelledError:
        return
    pf = pending_files.get(uid)
    if pf and not pf.starting:
        await start_job(pf)


def _discard_pending(pf: PendingFile) -> None:
    pending_files.pop(pf.uid, None)
    if pf.timer and not pf.timer.done() and pf.timer is not asyncio.current_task():
        pf.timer.cancel()


async def start_job(pf: PendingFile) -> None:
    """Charge the user's entitlement and put the pending file into the queue."""
    if pf.starting:
        return  # ▶️ tapped twice / timer fired at the same moment
    pf.starting = True
    _discard_pending(pf)
    uid = pf.uid
    tmp_dir = pf.path.parent
    try:
        await _start_job_inner(pf, uid, tmp_dir)
    except Exception as e:  # noqa: BLE001
        log.exception("start_job failed for %s: %s", uid, e)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        await safe_edit(pf.status, ERR_TEXT)


async def _start_job_inner(pf: PendingFile, uid: int, tmp_dir: Path) -> None:
    if not pf.path.exists():
        await safe_edit(pf.status, "❌ File expired. Please send it again.")
        return
    if jobs.user_has_job(uid):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        await safe_edit(pf.status, "⚠️ You already have a file in progress. Use /cancel to stop it first.")
        return
    if db.is_banned(uid):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        await safe_edit(pf.status, "🚫 <b>Cancelled.</b>")
        return
    # Re-resolve now: quota may have changed while the file was downloading / waiting.
    access = resolve_access(uid)
    if access.source == "blocked":
        shutil.rmtree(tmp_dir, ignore_errors=True)
        await safe_edit(pf.status, access.reason)
        return
    if pf.path.stat().st_size > access.max_mb * 1024 * 1024:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        await safe_edit(pf.status, f"⚠️ File too large for your plan (limit <b>{access.max_mb} MB</b>).")
        return
    # Credits are spent up-front (atomically) so two parallel uploads can't
    # both ride on the same last credit; refunded if the job fails/cancels.
    paid_with_credit = False
    if access.source == "credit":
        if not db.spend_credit(uid):
            shutil.rmtree(tmp_dir, ignore_errors=True)
            await safe_edit(pf.status, resolve_access(uid).reason or "⏳ No credits left.")
            return
        paid_with_credit = True
    premium = access.priority == 0
    job_id = db.add_job(uid, pf.name, pf.lang)
    job = Job(
        priority=access.priority,
        created=time.time(),
        id=job_id,
        user_id=uid,
        chat_id=pf.chat_id,
        file_path=pf.path,
        file_name=pf.name,
        lang=pf.lang,
        status_msg=pf.status,
        ext=pf.ext,
        credit=paid_with_credit,
        out_ext=pf.out_ext if pf.out_ext and not docconv.same_kind(pf.out_ext, pf.ext) else "",
        split_kb=max(0, int(pf.split_kb or 0)),
        strip_extras=bool(pf.strip_extras),
        free_chars=access.max_chars if access.source == "free" else 0,
    )
    await jobs.submit(job)
    pos = jobs.position(job_id)
    opts = f"\n📤 {out_format_label(job.out_ext, pf.ext)} · ✂️ {split_label(job.split_kb)}"
    if pf.ext == ".epub":
        opts += f" · 🧹 {'clean' if job.strip_extras else 'keep extras'}"
    await safe_edit(
        pf.status,
        f"✅ <b>Queued</b> · position #{pos}\n📄 {html.escape(pf.name)}\n🌐 → {lang_name(pf.lang)}" + opts
        + (f"\n🎟 1 credit used · {db.credits(uid)} left" if paid_with_credit else "")
        + (f"\n🆓 Free quota left today: {fmt_chars(job.free_chars)} chars" if job.free_chars else "")
        + ("" if premium else f"\n\n👑 Premium users skip the queue — tap <b>{BTN_PREMIUM}</b> below")
        + f"\n↩️ Changed your mind? Tap <b>{BTN_CANCEL}</b> below.",
        cancel_kb(job_id),
    )


def clear_set_custom(uid: int) -> None:
    """Forget a pending 'type your default split size' prompt (only that mode)."""
    if pending_input.get(uid) == "set_custom":
        pending_input.pop(uid, None)


def _validate_split(kb: Optional[int]) -> Optional[str]:
    """None → valid; else a user-facing error."""
    if kb is None:
        return "⚠️ Please send a size like <code>500kb</code> or <code>25mb</code> (or <code>0</code> for no split)."
    if kb and kb < Config.SPLIT_MIN_KB:
        return f"⚠️ Minimum split size is {Config.SPLIT_MIN_KB} KB."
    if kb > Config.TG_MAX_FILE_MB * 1024:
        return f"⚠️ Telegram files can't exceed {Config.TG_MAX_FILE_MB} MB."
    return None


@app.on_callback_query(filters.regex(r"^opt:(\w+)(?::(.+))?$"))
@guarded
async def cb_options(client: Client, cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    pf = pending_files.get(uid)
    action, arg = cq.matches[0].group(1), cq.matches[0].group(2) or ""
    if not pf or (cq.message and pf.status and pf.status.id != cq.message.id):
        await safe_answer(cq, "This file is no longer waiting — send it again.", alert=True)
        # stale panel: drop its buttons so it cannot be tapped again
        try:
            if cq.message and cq.message.reply_markup and not pf:
                await cq.message.edit_reply_markup(None)
        except Exception:  # noqa: BLE001
            pass
        return
    if pf.starting:
        return await safe_answer(cq, "Already starting…")
    # every interaction gives the user a fresh window before auto-start
    pf.deadline = max(pf.deadline, time.monotonic() + min(Config.OPTIONS_TIMEOUT, 45))
    if action == "fmt":
        if arg == "same":
            pf.out_ext = ""
        elif arg in docconv.available_outputs():
            pf.out_ext = arg
        else:
            return await safe_answer(cq, "That format is not available on this server.", alert=True)
        pf.awaiting_custom = False
        await safe_answer(cq, f"Format: {out_format_label(pf.out_ext, pf.ext)}")
    elif action == "split":
        try:
            kb = max(0, int(arg or 0))
        except ValueError:
            return await safe_answer(cq, "Bad value.", alert=True)
        pf.split_kb = kb
        pf.awaiting_custom = False
        await safe_answer(cq, f"Split: {split_label(pf.split_kb)}")
    elif action == "custom":
        pf.awaiting_custom = True
        pf.deadline = time.monotonic() + Config.OPTIONS_TIMEOUT
        await safe_answer(cq, "Type the size in the chat")
        await safe_edit(
            cq.message,
            options_text(pf) + "\n\n✏️ <b>Send the maximum size per file</b> as a message, e.g. <code>500kb</code>, "
            f"<code>25mb</code>, <code>1.5gb</code> (min {Config.SPLIT_MIN_KB} KB, max {Config.TG_MAX_FILE_MB} MB). Send <code>0</code> for no split.",
            options_kb("opt", pf.out_ext, pf.split_kb, pf.ext, strip=pf.strip_extras),
        )
        return
    elif action == "strip":
        pf.strip_extras = not pf.strip_extras
        pf.awaiting_custom = False
        await safe_answer(cq, "Crawler pages will be removed" if pf.strip_extras else "Crawler pages will be kept")
    elif action == "save":
        db.set_out_format(uid, pf.out_ext)
        db.set_split_kb(uid, pf.split_kb)
        db.set_strip_extras(uid, pf.strip_extras)
        await safe_answer(cq, "Saved as your default ✔")
    elif action == "start":
        await safe_answer(cq, "Starting…")
        await start_job(pf)
        return
    elif action == "cancel":
        _discard_pending(pf)
        shutil.rmtree(pf.path.parent, ignore_errors=True)
        await safe_answer(cq, "Cancelled")
        await safe_edit(cq.message, "🚫 <b>Cancelled.</b> Send another file whenever you like.")
        return
    else:
        return await safe_answer(cq, "Unknown action.", alert=True)
    await safe_edit(cq.message, options_text(pf), options_kb("opt", pf.out_ext, pf.split_kb, pf.ext, strip=pf.strip_extras))


async def handle_custom_size(message: Message, kb: Optional[int]) -> bool:
    """Text sent while a file waits for a custom split size. Returns True when consumed."""
    uid = message.from_user.id
    pf = pending_files.get(uid)
    if pf and pf.awaiting_custom:
        err = _validate_split(kb)
        if err:
            pf.deadline = time.monotonic() + Config.OPTIONS_TIMEOUT
            await safe_reply(message, err)
            return True
        pf.split_kb = kb or 0
        pf.awaiting_custom = False
        pf.deadline = time.monotonic() + Config.OPTIONS_TIMEOUT
        await safe_edit(pf.status, options_text(pf), options_kb("opt", pf.out_ext, pf.split_kb, pf.ext, strip=pf.strip_extras))
        await safe_reply(
            message,
            f"✂️ Split set: <b>{split_label(pf.split_kb)}</b>",
            reply_markup=InlineKeyboardMarkup([[ibtn("start", callback_data="opt:start")]]),
        )
        return True
    if pending_input.get(uid) == "set_custom":
        err = _validate_split(kb)
        if err:
            await safe_reply(message, err + " Or tap ✖ Close on the settings panel to stop.")
            return True
        pending_input.pop(uid, None)
        db.set_split_kb(uid, kb or 0)
        row = db.get_user(uid)
        await safe_reply(message, settings_text(row), reply_markup=settings_kb_for(row))
        return True
    return False


# ── /settings ──────────────────────────────────────────────────────────────
@app.on_message(filters.private & (filters.command(["settings", "output", "format", "split"]) | btn(BTN_SETTINGS, *BTN_SETTINGS_OLD)))
@guarded
async def cmd_settings(client: Client, message: Message) -> None:
    row = await guard(client, message)
    if not row:
        return
    clear_set_custom(message.from_user.id)
    await safe_reply(message, settings_text(row), reply_markup=settings_kb_for(row))


@app.on_callback_query(filters.regex(r"^set:(\w+)(?::(.+))?$"))
@guarded
async def cb_settings(client: Client, cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    await cq_user(cq)
    action, arg = cq.matches[0].group(1), cq.matches[0].group(2) or ""
    if action == "fmt":
        if arg == "same":
            db.set_out_format(uid, "")
        elif arg in docconv.available_outputs():
            db.set_out_format(uid, arg)
        else:
            return await safe_answer(cq, "That format is not available on this server.", alert=True)
        clear_set_custom(uid)
        await safe_answer(cq, "Default format saved")
    elif action == "split":
        try:
            db.set_split_kb(uid, max(0, int(arg or 0)))
        except ValueError:
            return await safe_answer(cq, "Bad value.", alert=True)
        clear_set_custom(uid)
        await safe_answer(cq, "Default split saved")
    elif action == "custom":
        pending_input[uid] = "set_custom"
        await safe_answer(cq, "Type the size in the chat")
        if cq.message:
            await safe_send(
                client,
                cq.message.chat.id,
                f"✏️ Send the default maximum size per file, e.g. <code>500kb</code>, <code>25mb</code> "
                f"(min {Config.SPLIT_MIN_KB} KB, max {Config.TG_MAX_FILE_MB} MB). Send <code>0</code> for no split.",
            )
        return
    elif action == "ask":
        row = db.get_user(uid)
        new = not _ask_on(row)
        db.set_ask_options(uid, new)
        await safe_answer(cq, "Will ask before every file" if new else "Files start immediately with these defaults")
    elif action == "strip":
        row = db.get_user(uid)
        new = not _strip_on(row)
        db.set_strip_extras(uid, new)
        pf = pending_files.get(uid)
        if pf:
            pf.strip_extras = new
        await safe_answer(cq, "Crawler summary/TOC pages will be removed" if new else "Crawler pages will be kept")
    elif action == "reset":
        db.set_out_format(uid, "")
        db.set_split_kb(uid, 0)
        db.set_ask_options(uid, True)
        db.set_strip_extras(uid, True)
        clear_set_custom(uid)
        await safe_answer(cq, "Defaults restored")
    elif action == "close":
        clear_set_custom(uid)
        await safe_answer(cq)
        if not await safe_delete(cq.message):
            await safe_edit(cq.message, "✖ Closed.")
        return
    else:
        return await safe_answer(cq, "Unknown action.", alert=True)
    row = db.get_user(uid)
    await safe_edit(cq.message, settings_text(row), settings_kb_for(row))


# ═══════════════════════════════════════════════════════════════════════════
#  ADMIN
# ═══════════════════════════════════════════════════════════════════════════

admin_filter = filters.private & filters.create(lambda _, __, m: bool(m.from_user and Config.is_admin(m.from_user.id)))


def admin_text() -> str:
    s = db.stats()
    return (
        "🔱 <b>Admin panel</b>\n\n"
        f"👥 Users: {s['users']} (⭐ {s['premium']} · 🚫 {s['banned']})\n"
        f"🖥 Workers: {len(pool.available())}/{len(pool.workers)} online\n"
        f"⚙️ Processing: {len(jobs.running)} · Queued: {jobs.queued_count()}\n\n"
        "<b>Commands</b>\n"
        "<code>/addworker URL</code> · <code>/delworker URL</code>\n"
        "<code>/addpremium USER_ID [days]</code> · <code>/addplan USER_ID PLAN [days]</code>\n"
        "<code>/addcredits USER_ID N</code> · <code>/revoke USER_ID</code>\n"
        f"Plans: {' · '.join(f'<code>{k}</code>' for k in PLANS)}\n"
        "<code>/ban USER_ID</code> · <code>/unban USER_ID</code> · <code>/user USER_ID</code>\n"
        "<code>/broadcast TEXT</code> (or reply to a message)\n"
        "<code>/seticon KEY</code> + custom emoji → sticker-style button icons · <code>/icons</code>"
    )


@app.on_message(admin_filter & (filters.command("admin") | btn(BTN_ADMIN, *BTN_ADMIN_OLD)))
@guarded
async def cmd_admin(client: Client, message: Message) -> None:
    await safe_reply(message, admin_text(), reply_markup=admin_kb())


# non-admins tapping a cached "🔱 Admin" / "🛠 Admin" button must get *some* answer
@app.on_message(filters.private & ~admin_filter & (filters.command("admin") | btn(BTN_ADMIN, *BTN_ADMIN_OLD)))
@guarded
async def cmd_admin_denied(client: Client, message: Message) -> None:
    if not message.from_user:
        return
    await safe_reply(message, "🔒 This section is for admins only.", reply_markup=main_kb(message.from_user.id))


def _back_kb(target: str = "adm:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=target), close_btn("adm:close")]])


# health check may take up to 75 s — never run two at once from button spam
_health_lock = asyncio.Lock()


@app.on_callback_query(filters.regex(r"^adm:(\w+)$"))
@guarded
async def cb_admin(client: Client, cq: CallbackQuery) -> None:
    if not Config.is_admin(cq.from_user.id):
        return await safe_answer(cq, "Admins only.", alert=True)
    action = cq.matches[0].group(1)
    if action == "menu":
        await safe_answer(cq)
        await safe_edit(cq.message, admin_text(), admin_kb())
    elif action == "close":
        pending_input.pop(cq.from_user.id, None)
        await safe_answer(cq)
        if not await safe_delete(cq.message):
            await safe_edit(cq.message, "✖ Closed.")
    elif action == "workers":
        await safe_answer(cq)
        await safe_edit(cq.message, "🖥 <b>Workers</b>\n\n" + pool.summary(), workers_kb())
    elif action == "health":
        # answer *before* pinging: a ping may take up to 75 s and Telegram only
        # accepts a callback answer for ~15 s (otherwise the button spins forever)
        if _health_lock.locked():
            return await safe_answer(cq, "A health check is already running…")
        await safe_answer(cq, "Pinging workers…")
        await safe_edit(cq.message, "🖥 <b>Workers</b>\n\n⏳ Pinging every worker (up to ~75 s)…", _back_kb())
        async with _health_lock:
            if jobs.session:
                await pool.health_check(jobs.session)
        await safe_edit(cq.message, "🖥 <b>Workers</b> (fresh check)\n\n" + pool.summary(), workers_kb())
    elif action == "stats":
        await safe_answer(cq)
        s = db.stats()
        await safe_edit(
            cq.message,
            "📈 <b>Statistics</b>\n\n"
            f"👥 Users: {s['users']} · new 24h: {s['today_users']}\n"
            f"⭐ Premium: {s['premium']} · 🚫 Banned: {s['banned']}\n\n"
            f"📚 Jobs: {s['jobs_total']} · ✅ {s['jobs_done']} · ❌ {s['jobs_failed']}\n"
            f"🔤 Characters translated: {s['chars']:,}\n\n"
            f"💰 Payments: {s['payments']} · Revenue: ₹{s['revenue']:,} + {s['stars']:,} ⭐\n"
            f"🎟 Unused credits (all users): {s['credits_out']}",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="adm:stats")], [InlineKeyboardButton("« Back", callback_data="adm:menu"), close_btn("adm:close")]]),
        )
    elif action == "queue":
        await safe_answer(cq)
        lines = []
        ordered = sorted(jobs.jobs.values(), key=lambda j: (j.id not in jobs.running, j.priority, j.created))
        rows: List[List[InlineKeyboardButton]] = []
        for j in ordered[:40]:
            state = "⚙️" if j.id in jobs.running else "⏳"
            lines.append(f"{state} #{j.id} · <code>{j.user_id}</code> · {html.escape(j.file_name[:30])} → {j.lang}")
        for j in ordered[:6]:
            rows.append([ibtn("cancel", f"🚫 Cancel #{j.id}", with_icon=False, callback_data=f"cancel:{j.id}")])
        if len(ordered) > 40:
            lines.append(f"… and {len(ordered) - 40} more")
        rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="adm:queue")])
        rows.append([InlineKeyboardButton("« Back", callback_data="adm:menu"), close_btn("adm:close")])
        await safe_edit(cq.message, "📋 <b>Queue</b>\n\n" + ("\n".join(lines) or "Empty."), InlineKeyboardMarkup(rows))
    elif action == "clear":
        n = jobs.clear_stuck()
        await safe_answer(cq, f"Cancelled {n} running job(s).", alert=True)
        await safe_edit(cq.message, admin_text(), admin_kb())
    elif action == "bcast":
        pending_input[cq.from_user.id] = "broadcast"
        await safe_answer(cq)
        await safe_edit(
            cq.message,
            "📣 Send the broadcast message now (text / photo / document). It will be copied to every user.\n"
            "Tap <b>Abort</b> or send /cancel_input to stop.",
            InlineKeyboardMarkup([[InlineKeyboardButton("✖ Abort", callback_data="adm:abort")]]),
        )
    elif action == "abort":
        pending_input.pop(cq.from_user.id, None)
        await safe_answer(cq, "Aborted")
        await safe_edit(cq.message, admin_text(), admin_kb())
    else:
        await safe_answer(cq, "Unknown action.", alert=True)


@app.on_callback_query(filters.regex(r"^wrk:(\w+)(?::(\w+))?$"))
@guarded
async def cb_workers(client: Client, cq: CallbackQuery) -> None:
    if not Config.is_admin(cq.from_user.id):
        return await safe_answer(cq, "Admins only.", alert=True)
    action, wid = cq.matches[0].group(1), cq.matches[0].group(2)
    if action == "add":
        pending_input[cq.from_user.id] = "add_worker"
        await safe_answer(cq)
        await safe_edit(
            cq.message,
            "➕ Send the worker URL(s), e.g.\n<code>https://user-space.hf.space</code>\n<code>https://user.pythonanywhere.com</code>\n"
            "<code>https://proj.vercel.app</code>\n<code>https://xyz.onrender.com</code>\n\nTap <b>Abort</b> or send /cancel_input to stop.",
            InlineKeyboardMarkup([[InlineKeyboardButton("✖ Abort", callback_data="wrk:abort")]]),
        )
        return
    if action == "abort":
        pending_input.pop(cq.from_user.id, None)
        await safe_answer(cq, "Aborted")
        await safe_edit(cq.message, "🖥 <b>Workers</b>\n\n" + pool.summary(), workers_kb())
        return
    w = worker_by_id(wid or "")
    if w is None:
        await safe_answer(cq, "Worker not found (list changed) — refreshed.", alert=True)
        await safe_edit(cq.message, "🖥 <b>Workers</b>\n\n" + pool.summary(), workers_kb())
        return
    if action == "toggle":
        pool.toggle(w.url)
        await safe_answer(cq, "Worker enabled" if pool.workers.get(w.url, w).enabled else "Worker paused")
    elif action == "del":
        pool.remove(w.url)
        await safe_answer(cq, "Worker removed")
    else:
        return await safe_answer(cq, "Unknown action.", alert=True)
    await safe_edit(cq.message, "🖥 <b>Workers</b>\n\n" + pool.summary(), workers_kb())


def _arg_int(message: Message, i: int, default: Optional[int] = None) -> Optional[int]:
    """message.command[i] as int, or default (None → missing/invalid)."""
    try:
        return int(message.command[i])
    except (IndexError, ValueError, AttributeError):
        return default


@app.on_message(admin_filter & filters.command("addworker"))
@guarded
async def cmd_addworker(client: Client, message: Message) -> None:
    pending_input.pop(message.from_user.id, None)
    if len(message.command) < 2:
        return await safe_reply(message, "Usage: <code>/addworker https://user-space.hf.space [more URLs…]</code>")
    urls = message.command[1:]
    added = [u for u in urls if pool.add(u)]
    bad = [u for u in urls if not pool.is_valid(pool.normalize(u))]
    if jobs.session and added:
        await pool.health_check(jobs.session)
    note = f"\n⚠️ Ignored invalid: {html.escape(', '.join(bad))}" if bad else ""
    await safe_reply(message, f"✅ Added {len(added)} worker(s).{note}\n\n" + pool.summary(), reply_markup=workers_kb())


@app.on_message(admin_filter & filters.command("delworker"))
@guarded
async def cmd_delworker(client: Client, message: Message) -> None:
    if len(message.command) < 2:
        return await safe_reply(message, "Usage: <code>/delworker URL</code>")
    ok = pool.remove(pool.normalize(message.command[1]))
    await safe_reply(message, "✅ Removed." if ok else "⚠️ Not found.", reply_markup=workers_kb())


@app.on_message(admin_filter & filters.command("addpremium"))
@guarded
async def cmd_addpremium(client: Client, message: Message) -> None:
    uid = _arg_int(message, 1)
    days = _arg_int(message, 2, Config.PREMIUM_DAYS)
    if uid is None or days is None or days <= 0:
        return await safe_reply(message, "Usage: <code>/addpremium USER_ID [days]</code>")
    until = db.add_premium(uid, days)
    await safe_reply(message, f"⭐ Premium for <code>{uid}</code> till {fmt_dt(until)}.")
    await safe_send(client, uid, f"🎉 <b>Premium activated!</b> Valid till <b>{fmt_dt(until)}</b>.")


@app.on_message(admin_filter & filters.command("addplan"))
@guarded
async def cmd_addplan(client: Client, message: Message) -> None:
    """/addplan USER_ID PLAN_KEY [days|credits] — grant any catalogue plan manually."""
    uid = _arg_int(message, 1)
    plan = PLANS.get(message.command[2].lower()) if len(message.command) > 2 else None
    amount = _arg_int(message, 3, 0)
    if uid is None or plan is None or amount is None or amount < 0:
        return await safe_reply(
            message,
            "Usage: <code>/addplan USER_ID PLAN [days|credits]</code>\n"
            f"Plans: {' · '.join(f'<code>{k}</code>' for k in PLANS)}",
        )
    if plan.is_sub:
        until = db.add_subscription(uid, sub_plan_for_grant(plan.key), amount or plan.days)
        await safe_reply(message, f"{plan.emoji} {plan.title} for <code>{uid}</code> till {fmt_dt(until)}.")
        note = f"🎉 <b>{plan.emoji} {plan.title} activated!</b> Valid till <b>{fmt_dt(until)}</b>."
    else:
        n = amount or plan.credits
        total = db.add_credits(uid, n)
        await safe_reply(message, f"{plan.emoji} +{n} credits for <code>{uid}</code> → {total} total.")
        note = f"🎉 <b>{plan.emoji} {plan.title} activated!</b> +{n} credits → you now have <b>{total}</b>. They never expire."
    await safe_send(client, uid, note)


@app.on_message(admin_filter & filters.command("addcredits"))
@guarded
async def cmd_addcredits(client: Client, message: Message) -> None:
    """/addcredits USER_ID N — N may be negative to deduct."""
    uid, n = _arg_int(message, 1), _arg_int(message, 2)
    if uid is None or n is None:
        return await safe_reply(message, "Usage: <code>/addcredits USER_ID N</code>")
    total = db.add_credits(uid, n)
    await safe_reply(message, f"🎟 Credits for <code>{uid}</code>: {n:+d} → <b>{total}</b>.")
    if n > 0:
        await safe_send(client, uid, f"🎟 You received <b>{n}</b> file credits → total <b>{total}</b>. They never expire.")


@app.on_message(admin_filter & filters.command("revoke"))
@guarded
async def cmd_revoke(client: Client, message: Message) -> None:
    uid = _arg_int(message, 1)
    if uid is None:
        return await safe_reply(message, "Usage: <code>/revoke USER_ID</code>")
    db.revoke_premium(uid)
    await safe_reply(message, f"Subscription revoked for <code>{uid}</code> (credits untouched — use /addcredits to adjust).")


@app.on_message(admin_filter & filters.command(["ban", "unban"]))
@guarded
async def cmd_ban(client: Client, message: Message) -> None:
    uid = _arg_int(message, 1)
    if uid is None:
        return await safe_reply(message, f"Usage: <code>/{message.command[0]} USER_ID</code>")
    if uid == Config.OWNER_ID or uid in Config.ADMIN_IDS:
        return await safe_reply(message, "Cannot ban an admin.")
    ban = message.command[0].lower() == "ban"
    if db.get_user(uid) is None:
        db.upsert_user(uid, "", None)
    db.set_banned(uid, ban)
    if ban:
        # stop everything the user has in flight and free their slot
        if uid in jobs.by_user:
            jobs.cancel(jobs.by_user[uid])
        pf = pending_files.get(uid)
        if pf:
            _discard_pending(pf)
            shutil.rmtree(pf.path.parent, ignore_errors=True)
            await safe_edit(pf.status, "🚫 <b>Cancelled.</b>")
    await safe_reply(message, f"{'🚫 Banned' if ban else '✅ Unbanned'} <code>{uid}</code>.")


@app.on_message(admin_filter & filters.command("user"))
@guarded
async def cmd_user(client: Client, message: Message) -> None:
    uid = _arg_int(message, 1)
    if uid is None:
        return await safe_reply(message, "Usage: <code>/user USER_ID</code>")
    row = db.get_user(uid)
    if not row:
        return await safe_reply(message, "User not found.")
    state = ""
    if uid in jobs.by_user:
        state = f"\n⚙️ Active job #{jobs.by_user[uid]}"
    elif uid in pending_files:
        state = "\n⏳ File waiting for options"
    await safe_reply(
        message,
        f"👤 <b>{html.escape(row['name'] or '-')}</b> @{row['username'] or '-'} · <code>{uid}</code>\n"
        f"🌐 {lang_name(row['lang'])} · 📚 {row['total_files']} files · today {db.daily_used(uid)} files / {db.daily_chars_used(uid):,} chars\n"
        f"💼 {user_line(row)}\n"
        f"🚫 Banned: {'yes' if row['banned'] else 'no'} · joined {fmt_dt(row['joined'])}" + state,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Ban" if not row["banned"] else "✅ Unban", callback_data=f"usr:{'unban' if row['banned'] else 'ban'}:{uid}")]]),
    )


@app.on_callback_query(filters.regex(r"^usr:(ban|unban):(\d+)$"))
@guarded
async def cb_user(client: Client, cq: CallbackQuery) -> None:
    if not Config.is_admin(cq.from_user.id):
        return await safe_answer(cq, "Admins only.", alert=True)
    action, uid = cq.matches[0].group(1), int(cq.matches[0].group(2))
    if uid == Config.OWNER_ID or uid in Config.ADMIN_IDS:
        return await safe_answer(cq, "Cannot ban an admin.", alert=True)
    ban = action == "ban"
    if db.get_user(uid) is None:
        db.upsert_user(uid, "", None)
    db.set_banned(uid, ban)
    if ban:
        if uid in jobs.by_user:
            jobs.cancel(jobs.by_user[uid])
        pf = pending_files.get(uid)
        if pf:
            _discard_pending(pf)
            shutil.rmtree(pf.path.parent, ignore_errors=True)
            await safe_edit(pf.status, "🚫 <b>Cancelled.</b>")
    await safe_answer(cq, "Banned" if ban else "Unbanned")
    try:
        await cq.message.edit_reply_markup(
            InlineKeyboardMarkup([[InlineKeyboardButton("✅ Unban" if ban else "🚫 Ban", callback_data=f"usr:{'unban' if ban else 'ban'}:{uid}")]])
        )
    except Exception:  # noqa: BLE001
        pass


_broadcast_running = False


async def do_broadcast(client: Client, src: Message, status: Message) -> None:
    global _broadcast_running
    if _broadcast_running:
        await safe_edit(status, "⚠️ Another broadcast is still running — try again when it finishes.")
        return
    _broadcast_running = True
    try:
        ids = db.all_user_ids()
        ok = fail = 0
        for i, uid in enumerate(ids, 1):
            try:
                await src.copy(uid)
                ok += 1
            except FloodWait as e:
                await asyncio.sleep(min(e.value, 300))
                try:
                    await src.copy(uid)
                    ok += 1
                except Exception:  # noqa: BLE001
                    fail += 1
            except Exception:  # noqa: BLE001
                fail += 1
            if i % 25 == 0:
                await safe_edit(status, f"📣 Broadcasting… {i}/{len(ids)}")
            await asyncio.sleep(0.05)
        await safe_edit(status, f"📣 <b>Broadcast done</b>\n✅ {ok} · ❌ {fail}")
    except Exception as e:  # noqa: BLE001
        log.exception("broadcast crashed: %s", e)
        await safe_edit(status, "❌ Broadcast stopped due to an error — see the log.")
    finally:
        _broadcast_running = False


@app.on_message(admin_filter & filters.command("broadcast"))
@guarded
async def cmd_broadcast(client: Client, message: Message) -> None:
    pending_input.pop(message.from_user.id, None)
    src = message.reply_to_message
    if not src:
        text = (message.text or message.caption or "").split(None, 1)
        if len(text) < 2:
            return await safe_reply(message, "Reply to a message with /broadcast, or <code>/broadcast TEXT</code>.")
        try:
            src = await message.reply_text(text[1])
        except Exception:  # invalid HTML in the text → send it verbatim
            src = await safe_reply(message, text[1], parse_mode=ParseMode.DISABLED)
        if src is None:
            return
    status = await safe_reply(message, "📣 Broadcasting…")
    if status:
        asyncio.create_task(do_broadcast(client, src, status))


@app.on_message(admin_filter & filters.command("cancel_input"))
@guarded
async def cmd_cancel_input(client: Client, message: Message) -> None:
    had = pending_input.pop(message.from_user.id, None)
    await safe_reply(message, "Input cancelled." if had else "Nothing to cancel.")


# ── button icons (custom emoji) ────────────────────────────────────────────
def _custom_emoji_ids(message: Message) -> List[str]:
    """All custom-emoji ids found in a message (text entities or a custom-emoji sticker)."""
    ids: List[str] = []
    for ent in (message.entities or []) + (message.caption_entities or []):
        cid = getattr(ent, "custom_emoji_id", None)
        if cid:
            ids.append(str(cid))
    st = getattr(message, "sticker", None)
    if st is not None and getattr(st, "custom_emoji_id", None):
        ids.append(str(st.custom_emoji_id))
    return ids


def icons_text() -> str:
    cur = icon_ids()
    lines = [
        "🎨 <b>Button icons</b> (custom emoji shown before the button text)\n",
        "Status: " + ("✅ active" if _icons_ok else "⚠️ rejected by Telegram — the bot owner needs <b>Telegram Premium</b> for custom-emoji icons; colours still work") + "\n",
    ]
    for key, label in BTN_KEYS.items():
        lines.append(f"<code>{key}</code> → {html.escape(label)}" + (f" · icon <code>{cur[key]}</code>" if key in cur else " · <i>none</i>"))
    lines.append(
        "\n<b>How to set</b>\n"
        "1. Open any Premium emoji pack (e.g. the 👑 / 🔥 ones from your sticker panel)\n"
        "2. Send <code>/seticon premium</code> and in the <b>same message</b> add the emoji — e.g. <code>/seticon premium 👑</code> picked from the custom pack\n"
        "   (or reply to a message that contains the custom emoji)\n"
        "3. <code>/seticon premium off</code> removes it · <code>/icons reset</code> removes all\n"
        "Then tap /start to refresh the keyboard."
    )
    if not HAS_BUTTON_STYLE:
        lines.append("\n⚠️ Running on legacy pyrogram — install <code>kurigram</code> (see requirements-bot.txt) for colours & icons.")
    return "\n".join(lines)


@app.on_message(admin_filter & filters.command("icons"))
@guarded
async def cmd_icons(client: Client, message: Message) -> None:
    global _icons_ok
    arg = (message.command[1] if len(message.command) > 1 else "").lower()
    if arg == "reset":
        for k in icon_ids():
            db.del_setting(ICON_SETTING_PREFIX + k)
        _icons_ok = True
        return await safe_reply(message, "♻️ All button icons removed.", reply_markup=main_kb(message.from_user.id))
    if arg == "retry":
        _icons_ok = True
    await safe_reply(message, icons_text(), reply_markup=main_kb(message.from_user.id))


@app.on_message(admin_filter & filters.command("seticon"))
@guarded
async def cmd_seticon(client: Client, message: Message) -> None:
    global _icons_ok
    key = (message.command[1] if len(message.command) > 1 else "").lower()
    if key not in BTN_KEYS:
        return await safe_reply(
            message,
            "Usage: <code>/seticon KEY 👑</code> — KEY is one of: " + " · ".join(f"<code>{k}</code>" for k in BTN_KEYS)
            + "\nThe emoji must be a <b>custom (Premium) emoji</b>, picked from an emoji pack — a normal emoji has no id.",
        )
    rest = [x.lower() for x in message.command[2:]]
    if rest and rest[0] in ("off", "none", "remove", "clear"):
        db.del_setting(ICON_SETTING_PREFIX + key)
        return await safe_reply(message, f"🗑 Icon removed for <code>{key}</code>.", reply_markup=main_kb(message.from_user.id))
    ids = _custom_emoji_ids(message)
    if not ids and message.reply_to_message:
        ids = _custom_emoji_ids(message.reply_to_message)
    if not ids:
        return await safe_reply(
            message,
            "⚠️ No <b>custom emoji</b> found in that message.\n"
            "Open the emoji panel → pick one from a <b>custom/Premium pack</b> (the sticker-style ones) and send it together with the command, "
            "or reply to a message containing it.",
        )
    db.set_setting(ICON_SETTING_PREFIX + key, ids[0])
    _icons_ok = True  # new id → give Telegram another chance
    await safe_reply(
        message,
        f"✅ Icon set for <code>{key}</code> → id <code>{ids[0]}</code>\nIf the keyboard below still shows no icon, Telegram rejected it (owner needs Telegram Premium).",
        reply_markup=main_kb(message.from_user.id),
    )


# admin pending-input consumer (must be registered after commands; group=1)
@app.on_message(admin_filter & ~filters.command(KNOWN_COMMANDS), group=1)
@guarded
async def on_admin_input(client: Client, message: Message) -> None:
    mode = pending_input.get(message.from_user.id)
    if mode not in ("add_worker", "broadcast"):
        return
    text = message.text or ""
    if is_menu_button(text):
        # the admin tapped a menu button instead of answering → forget the prompt
        pending_input.pop(message.from_user.id, None)
        return
    if text.startswith("/"):
        # any other command was already handled in group 0; it must not be
        # swallowed here as a worker URL / broadcast text
        pending_input.pop(message.from_user.id, None)
        return
    pending_input.pop(message.from_user.id, None)
    if mode == "add_worker":
        if not text:
            await safe_reply(message, "⚠️ Please send the worker URL as text.")
        else:
            urls = text.split()
            added = [u for u in urls if pool.add(u)]
            bad = [u for u in urls if not pool.is_valid(pool.normalize(u))]
            if jobs.session and added:
                await pool.health_check(jobs.session)
            note = f"\n⚠️ Ignored invalid: {html.escape(', '.join(bad))}" if bad else ""
            await safe_reply(message, f"✅ Added {len(added)} worker(s).{note}\n\n" + pool.summary(), reply_markup=workers_kb())
    elif mode == "broadcast":
        status = await safe_reply(message, "📣 Broadcasting…")
        if status:
            asyncio.create_task(do_broadcast(client, message, status))
    message.stop_propagation()


# ── fallback for random text ───────────────────────────────────────────────
@app.on_message(filters.private & filters.text & ~filters.command(KNOWN_COMMANDS), group=2)
@guarded
async def on_text(client: Client, message: Message) -> None:
    if not message.from_user or not message.text:
        return
    text = message.text.strip()
    if is_menu_button(text):
        return  # handled in group 0 (or admin-denied)
    uid = message.from_user.id
    if text.startswith("/"):
        # unknown command — tell the user instead of staying silent
        if db.is_banned(uid):
            return
        return await safe_reply(
            message,
            "🤔 Unknown command. Try /help or use the buttons below.",
            reply_markup=main_kb(uid),
        )
    # a custom split size for a waiting file or for /settings?
    if await handle_custom_size(message, parse_size_kb(text)):
        return
    if Config.is_admin(uid) and uid in pending_input:
        return
    row = await guard(client, message)
    if not row:
        return
    await safe_reply(
        message,
        "📎 Send me a file to translate (<b>EPUB · PDF · DOCX · TXT · HTML</b>), or use the menu below.",
        reply_markup=main_kb(uid),
    )


# ── anything else (stickers, photos, voice…) ──────────────────────────────
@app.on_message(filters.private & ~filters.text & ~filters.document & ~filters.service, group=2)
@guarded
async def on_other(client: Client, message: Message) -> None:
    if not message.from_user:
        return
    uid = message.from_user.id
    if Config.is_admin(uid) and pending_input.get(uid) == "broadcast":
        return  # photo broadcast handled in group 1
    if db.is_banned(uid):
        return
    # Service / system messages Pyrogram cannot classify (e.g. the "payment
    # sent" notice after a ⭐ Stars purchase) carry no media at all — never
    # answer those with "please send a file".
    if message.media is None and not message.text and not message.caption:
        return
    await safe_reply(
        message,
        "📎 Please send the book as a <b>file/document</b> (EPUB · PDF · DOCX · TXT · HTML) — not as a photo or text.",
        reply_markup=main_kb(uid),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  BACKGROUND TASKS & ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════


async def keep_alive_loop() -> None:
    """Ping workers to keep sleepy free tiers (Render, HF Spaces) awake and refresh health."""
    await asyncio.sleep(5)
    while True:
        try:
            if jobs.session:
                await pool.health_check(jobs.session)
                log.info("health: %d/%d workers online", len(pool.available()), len(pool.workers))
        except Exception as e:  # noqa: BLE001
            log.warning("keep-alive error: %s", e)
        await asyncio.sleep(Config.WORKER_PING_INTERVAL)


def cleanup_on_boot() -> None:
    """Remove temp dirs and mark jobs that were interrupted by the previous
    process as failed (their files are gone; users must resend)."""
    n_dirs = 0
    try:
        for d in Config.DATA_DIR.glob("epub_*"):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                n_dirs += 1
    except Exception as e:  # noqa: BLE001
        log.warning("temp cleanup failed: %s", e)
    n_jobs = db.fail_interrupted_jobs()
    if n_dirs or n_jobs:
        log.info("boot cleanup: removed %d temp dir(s), marked %d interrupted job(s) failed", n_dirs, n_jobs)


async def register_commands() -> None:
    """Populate the '/' command menu in Telegram (users + richer list for admins)."""
    user_cmds = [
        BotCommand("start", "🏠 Main menu"),
        BotCommand("lang", "🌐 Choose target language"),
        BotCommand("settings", "📂 Output format & split size"),
        BotCommand("status", "📊 Your plan, queue & workers"),
        BotCommand("plans", "👑 Premium plans & pricing"),
        BotCommand("cancel", "❌ Cancel current file"),
        BotCommand("help", "📖 How it works"),
    ]
    admin_cmds = user_cmds + [
        BotCommand("admin", "Admin panel"),
        BotCommand("addworker", "Add worker URL(s)"),
        BotCommand("delworker", "Remove worker URL"),
        BotCommand("addplan", "Grant plan: USER_ID PLAN [n]"),
        BotCommand("addcredits", "Give credits: USER_ID N"),
        BotCommand("user", "Show user: USER_ID"),
        BotCommand("ban", "Ban USER_ID"),
        BotCommand("unban", "Unban USER_ID"),
        BotCommand("broadcast", "Broadcast a message"),
        BotCommand("seticon", "Button icon: KEY + custom emoji"),
        BotCommand("icons", "Show / reset button icons"),
    ]
    try:
        await app.set_bot_commands(user_cmds)
    except Exception as e:  # noqa: BLE001
        log.warning("set_bot_commands failed: %s", e)
    for aid in {Config.OWNER_ID, *Config.ADMIN_IDS} - {0}:
        try:
            await app.set_bot_commands(admin_cmds, scope=BotCommandScopeChat(aid))
        except Exception as e:  # noqa: BLE001
            log.debug("admin commands for %s failed: %s", aid, e)


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Never let an unhandled exception in a background task kill the process silently."""
    exc = context.get("exception")
    if isinstance(exc, asyncio.CancelledError):
        return
    log.error("unhandled asyncio error: %s", context.get("message"), exc_info=exc)


async def main() -> None:
    asyncio.get_running_loop().set_exception_handler(_loop_exception_handler)
    cleanup_on_boot()
    await app.start()
    me = await app.get_me()
    await register_commands()
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
        await safe_send(app, Config.OWNER_ID, f"🟢 Bot restarted · {len(pool.available())}/{len(pool.workers)} workers online")
    try:
        await idle()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # tell users whose file was waiting/queued that they need to resend
        for pf in list(pending_files.values()):
            _discard_pending(pf)
            shutil.rmtree(pf.path.parent, ignore_errors=True)
            await safe_edit(pf.status, "🔄 Bot is restarting — please send the file again in a minute.")
        for job in list(jobs.jobs.values()):
            await safe_edit(job.status_msg, "🔄 Bot is restarting — please send the file again in a minute.")
        try:
            if jobs.session:
                await jobs.session.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await app.stop()
        except Exception:  # noqa: BLE001
            pass
        log.info("Bot stopped")


def _run_bot() -> None:
    """Drive ``main()`` on the client's own event loop.

    kurigram >= 2.2 made ``Client.run()`` keyword-only (``run(*, use_qr=…)``), so
    the old ``app.run(main())`` raises *TypeError: Run.run() takes 1 positional
    argument but 2 were given*.  ``asyncio.run(main())`` is not a drop-in
    replacement either because it creates a fresh loop while the client's
    internal tasks are bound to ``app.loop`` (see the ``LOOP`` comment above).
    """
    loop = app.loop
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        # mirror asyncio.run()'s teardown so nothing is left pending on exit
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        except Exception:  # noqa: BLE001
            pass
        finally:
            asyncio.set_event_loop(None)
            loop.close()


if __name__ == "__main__":
    _run_bot()
