import os
import re
import csv
import io
import sqlite3
import logging
import hashlib
import hmac
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from html import escape

import psycopg
from psycopg.rows import dict_row
from telegram import (
    Update,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    WebAppInfo,
    MenuButtonWebApp,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, Response, RedirectResponse
import uvicorn

# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_PATH = os.environ.get("DB_PATH", "finance.db")
PORT = int(os.environ.get("PORT", "10000"))
BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"

WEBHOOK_BASE_URL = (
    os.environ.get("WEBHOOK_BASE_URL", "").strip()
    or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
)

TIMEZONE = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Europe/Moscow"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("finance_bot")

# Не логируем полные HTTP-запросы Telegram API: в URL может быть BOT_TOKEN.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ============================================================
# КАТЕГОРИИ
# ============================================================

EXPENSE_CATEGORIES = {
    "☕ Кофе": [
        "кофе", "капучино", "латте", "американо", "эспрессо",
        "кофейня", "coffee"
    ],
    "🍔 Еда": [
        "еда", "продукты", "супермаркет", "обед", "ужин", "завтрак",
        "бургер", "шаурма", "пицца", "ресторан", "кафе", "доставка",
        "вода", "хлеб", "молоко", "мясо", "фрукты"
    ],
    "🎮 Игры и развлечения": [
        "игра", "игры", "steam", "playstation", "xbox", "донат",
        "кино", "боулинг", "развлеч", "клуб", "бильярд"
    ],
    "✈️ Путешествия": [
        "путешеств", "отель", "гостиниц", "билет", "самолет",
        "самолёт", "поезд", "авиабилет", "booking", "airbnb",
        "тур", "виза"
    ],
    "🚕 Транспорт": [
        "такси", "uber", "яндекс такси", "бензин", "топливо",
        "парков", "автобус", "метро", "проезд", "машин", "мойка",
        "шиномонтаж", "масло", "заправ"
    ],
    "🛍 Покупки": [
        "одежд", "обув", "покупк", "телефон", "айфон", "iphone",
        "чехол", "ремешок", "техника", "ozon", "wildberries",
        "wb", "маркетплейс"
    ],
    "📱 Подписки и связь": [
        "подписк", "youtube", "spotify", "apple music", "icloud",
        "telegram premium", "netflix", "связь", "интернет",
        "сим", "тариф"
    ],
    "🏠 Дом": [
        "аренд", "квартир", "дом", "коммун", "электрич",
        "мебель", "ремонт", "быт"
    ],
    "💊 Здоровье": [
        "аптек", "лекар", "врач", "анализ", "стомат", "витамин",
        "массаж", "клиник", "мед"
    ],
    "📚 Образование": [
        "курс", "учеб", "книг", "образован", "универ",
        "колледж", "школ", "репетитор"
    ],
    "🎁 Подарки": ["подар", "цветы", "букет"],
    "💼 Бизнес": [
        "бизнес", "реклама", "закуп", "поставщик", "товар",
        "касса", "магазин бизнес", "зарплата сотруд", "аренда магазина"
    ],
    "💸 Прочее": [],
}

INCOME_CATEGORIES = {
    "💼 Зарплата": ["зарплат", "аванс", "зп", "оклад"],
    "🏪 Бизнес": ["бизнес", "магазин", "выручк", "продаж", "прибыл", "касса"],
    "↩️ Возврат": ["возврат", "вернули", "компенсац"],
    "🎁 Подарок": ["подарили", "подарок", "дали деньги"],
    "💰 Доход": [
        "доход", "получил", "получила", "пришло", "заработал",
        "заработала", "выплат", "перевели"
    ],
    "💵 Прочее": [],
}

INCOME_WORDS = (
    "доход", "получил", "получила", "пришло", "заработал",
    "заработала", "зарплата", "зарплату", "аванс", "выручка",
    "прибыль", "вернули", "возврат", "подарили", "начислили",
    "выплатили", "перевели",
)

EXPENSE_WORDS = (
    "расход", "потратил", "потратила", "купил", "купила",
    "заплатил", "заплатила", "списали", "оплатил", "оплатила",
)


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

def using_postgres():
    return bool(DATABASE_URL)


def init_db():
    if using_postgres():
        with psycopg.connect(DATABASE_URL) as con:
            with con.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS transactions (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
                        amount_kopecks BIGINT NOT NULL,
                        category TEXT NOT NULL,
                        note TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_tx_user_date
                    ON transactions(user_id, created_at)
                """)
        log.info("Database: PostgreSQL")
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
                    amount_kopecks INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_tx_user_date
                ON transactions(user_id, created_at)
            """)
        log.warning("DATABASE_URL not set: using SQLite")


def insert_transaction(user_id, kind, amount_kopecks, category, note, created_at):
    if using_postgres():
        with psycopg.connect(DATABASE_URL) as con:
            with con.cursor() as cur:
                cur.execute("""
                    INSERT INTO transactions
                    (user_id, kind, amount_kopecks, category, note, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    user_id, kind, amount_kopecks,
                    category, note, created_at
                ))
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("""
                INSERT INTO transactions
                (user_id, kind, amount_kopecks, category, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id, kind, amount_kopecks, category, note,
                created_at.isoformat(timespec="seconds"),
            ))


def fetch_summary(user_id, start, end):
    if using_postgres():
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT kind, category, SUM(amount_kopecks) AS total
                    FROM transactions
                    WHERE user_id=%s
                      AND created_at >= %s
                      AND created_at <= %s
                    GROUP BY kind, category
                    ORDER BY total DESC
                """, (user_id, start, end))
                return cur.fetchall()

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("""
            SELECT kind, category, SUM(amount_kopecks) AS total
            FROM transactions
            WHERE user_id=?
              AND created_at >= ?
              AND created_at <= ?
            GROUP BY kind, category
            ORDER BY total DESC
        """, (
            user_id,
            start.isoformat(timespec="seconds"),
            end.isoformat(timespec="seconds"),
        )).fetchall()
    finally:
        con.close()


def fetch_last(user_id, limit=10):
    if using_postgres():
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT *
                    FROM transactions
                    WHERE user_id=%s
                    ORDER BY id DESC
                    LIMIT %s
                """, (user_id, limit))
                return cur.fetchall()

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("""
            SELECT *
            FROM transactions
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
    finally:
        con.close()


def fetch_all(user_id):
    if using_postgres():
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT *
                    FROM transactions
                    WHERE user_id=%s
                    ORDER BY created_at ASC
                """, (user_id,))
                return cur.fetchall()

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("""
            SELECT *
            FROM transactions
            WHERE user_id=?
            ORDER BY created_at ASC
        """, (user_id,)).fetchall()
    finally:
        con.close()


def delete_last(user_id):
    rows = fetch_last(user_id, 1)
    if not rows:
        return None

    row = rows[0]
    row_id = row["id"]

    if using_postgres():
        with psycopg.connect(DATABASE_URL) as con:
            with con.cursor() as cur:
                cur.execute(
                    "DELETE FROM transactions WHERE id=%s AND user_id=%s",
                    (row_id, user_id),
                )
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "DELETE FROM transactions WHERE id=? AND user_id=?",
                (row_id, user_id),
            )

    return row



# ============================================================
# WEB-СЕССИИ ДЛЯ MINI APP / PWA
# ============================================================

def init_web_sessions():
    if using_postgres():
        with psycopg.connect(DATABASE_URL) as con:
            with con.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS web_sessions (
                        token_hash TEXT PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_web_sessions_user
                    ON web_sessions(user_id)
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS web_login_codes (
                        code_hash TEXT PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        expires_at TIMESTAMPTZ NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS browser_login_requests (
                        request_hash TEXT PRIMARY KEY,
                        user_id BIGINT,
                        expires_at TIMESTAMPTZ NOT NULL
                    )
                """)
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_web_sessions_user
                ON web_sessions(user_id)
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS web_login_codes (
                    code_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at TEXT NOT NULL
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS browser_login_requests (
                    request_hash TEXT PRIMARY KEY,
                    user_id INTEGER,
                    expires_at TEXT NOT NULL
                )
            """)


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_web_session(user_id):
    token = secrets.token_urlsafe(32)
    token_hash = _token_hash(token)
    created_at = now_local()

    if using_postgres():
        with psycopg.connect(DATABASE_URL) as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO web_sessions(token_hash, user_id, created_at)
                    VALUES (%s, %s, %s)
                    """,
                    (token_hash, user_id, created_at),
                )
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                """
                INSERT INTO web_sessions(token_hash, user_id, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    token_hash,
                    user_id,
                    created_at.isoformat(timespec="seconds"),
                ),
            )

    return token


def session_user_id(token):
    if not token:
        return None

    token_hash = _token_hash(token)

    if using_postgres():
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM web_sessions WHERE token_hash=%s",
                    (token_hash,),
                )
                row = cur.fetchone()
                return int(row["user_id"]) if row else None

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT user_id FROM web_sessions WHERE token_hash=?",
            (token_hash,),
        ).fetchone()
        return int(row["user_id"]) if row else None
    finally:
        con.close()


def create_login_code(user_id):
    code = f"{secrets.randbelow(1000000):06d}"
    code_hash = _token_hash(code)
    expires_at = now_local() + timedelta(minutes=60)

    if using_postgres():
        with psycopg.connect(DATABASE_URL) as con:
            with con.cursor() as cur:
                cur.execute(
                    "DELETE FROM web_login_codes WHERE user_id=%s OR expires_at < %s",
                    (user_id, now_local()),
                )
                cur.execute(
                    """
                    INSERT INTO web_login_codes(code_hash, user_id, expires_at)
                    VALUES (%s, %s, %s)
                    """,
                    (code_hash, user_id, expires_at),
                )
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "DELETE FROM web_login_codes WHERE user_id=?",
                (user_id,),
            )
            con.execute(
                """
                INSERT INTO web_login_codes(code_hash, user_id, expires_at)
                VALUES (?, ?, ?)
                """,
                (
                    code_hash,
                    user_id,
                    expires_at.isoformat(timespec="seconds"),
                ),
            )

    return code


def redeem_login_code(code):
    code = re.sub(r"\D", "", str(code or ""))
    if len(code) != 6:
        return None

    code_hash = _token_hash(code)

    if using_postgres():
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, expires_at
                    FROM web_login_codes
                    WHERE code_hash=%s
                    """,
                    (code_hash,),
                )
                row = cur.fetchone()
                if not row:
                    return None

                if row["expires_at"] < now_local():
                    cur.execute(
                        "DELETE FROM web_login_codes WHERE code_hash=%s",
                        (code_hash,),
                    )
                    return None

                cur.execute(
                    "DELETE FROM web_login_codes WHERE code_hash=%s",
                    (code_hash,),
                )
                return int(row["user_id"])

    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT user_id, expires_at
            FROM web_login_codes
            WHERE code_hash=?
            """,
            (code_hash,),
        ).fetchone()

        if not row:
            return None

        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=TIMEZONE)

        if expires_at < now_local():
            con.execute(
                "DELETE FROM web_login_codes WHERE code_hash=?",
                (code_hash,),
            )
            return None

        con.execute(
            "DELETE FROM web_login_codes WHERE code_hash=?",
            (code_hash,),
        )
        return int(row["user_id"])


def create_browser_handoff(user_id):
    ticket = secrets.token_urlsafe(24)
    ticket_hash = _token_hash(ticket)
    expires_at = now_local() + timedelta(minutes=2)

    if using_postgres():
        with psycopg.connect(DATABASE_URL) as con:
            with con.cursor() as cur:
                cur.execute(
                    "DELETE FROM web_login_codes WHERE user_id=%s OR expires_at < %s",
                    (user_id, now_local()),
                )
                cur.execute(
                    """
                    INSERT INTO web_login_codes(code_hash, user_id, expires_at)
                    VALUES (%s, %s, %s)
                    """,
                    (ticket_hash, user_id, expires_at),
                )
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "DELETE FROM web_login_codes WHERE user_id=?",
                (user_id,),
            )
            con.execute(
                """
                INSERT INTO web_login_codes(code_hash, user_id, expires_at)
                VALUES (?, ?, ?)
                """,
                (
                    ticket_hash,
                    user_id,
                    expires_at.isoformat(timespec="seconds"),
                ),
            )

    return ticket


def redeem_browser_handoff(ticket):
    ticket = str(ticket or "").strip()
    if len(ticket) < 20:
        return None

    ticket_hash = _token_hash(ticket)

    if using_postgres():
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, expires_at
                    FROM web_login_codes
                    WHERE code_hash=%s
                    """,
                    (ticket_hash,),
                )
                row = cur.fetchone()
                if not row:
                    return None

                cur.execute(
                    "DELETE FROM web_login_codes WHERE code_hash=%s",
                    (ticket_hash,),
                )

                if row["expires_at"] < now_local():
                    return None

                return int(row["user_id"])

    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT user_id, expires_at
            FROM web_login_codes
            WHERE code_hash=?
            """,
            (ticket_hash,),
        ).fetchone()

        if not row:
            return None

        con.execute(
            "DELETE FROM web_login_codes WHERE code_hash=?",
            (ticket_hash,),
        )

        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=TIMEZONE)

        if expires_at < now_local():
            return None

        return int(row["user_id"])


def create_browser_login_request():
    request_id = secrets.token_urlsafe(24)
    request_hash = _token_hash(request_id)
    expires_at = now_local() + timedelta(minutes=10)

    if using_postgres():
        with psycopg.connect(DATABASE_URL) as con:
            with con.cursor() as cur:
                cur.execute(
                    "DELETE FROM browser_login_requests WHERE expires_at < %s",
                    (now_local(),),
                )
                cur.execute(
                    """
                    INSERT INTO browser_login_requests
                    (request_hash, user_id, expires_at)
                    VALUES (%s, NULL, %s)
                    """,
                    (request_hash, expires_at),
                )
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                """
                INSERT INTO browser_login_requests
                (request_hash, user_id, expires_at)
                VALUES (?, NULL, ?)
                """,
                (
                    request_hash,
                    expires_at.isoformat(timespec="seconds"),
                ),
            )

    return request_id


def claim_browser_login_request(request_id, user_id):
    request_id = str(request_id or "").strip()
    if len(request_id) < 20 or len(request_id) > 80:
        return False

    request_hash = _token_hash(request_id)
    expires_at = now_local() + timedelta(minutes=60)

    if using_postgres():
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, expires_at
                    FROM browser_login_requests
                    WHERE request_hash=%s
                    FOR UPDATE
                    """,
                    (request_hash,),
                )
                row = cur.fetchone()

                if not row:
                    cur.execute(
                        """
                        INSERT INTO browser_login_requests
                        (request_hash, user_id, expires_at)
                        VALUES (%s, %s, %s)
                        """,
                        (request_hash, user_id, expires_at),
                    )
                    return True

                existing_user = row["user_id"]
                existing_expiry = row["expires_at"]

                if existing_user is not None and int(existing_user) != int(user_id) and existing_expiry >= now_local():
                    return False

                cur.execute(
                    """
                    UPDATE browser_login_requests
                    SET user_id=%s, expires_at=%s
                    WHERE request_hash=%s
                    """,
                    (user_id, expires_at, request_hash),
                )
                return True

    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT user_id, expires_at
            FROM browser_login_requests
            WHERE request_hash=?
            """,
            (request_hash,),
        ).fetchone()

        if not row:
            con.execute(
                """
                INSERT INTO browser_login_requests
                (request_hash, user_id, expires_at)
                VALUES (?, ?, ?)
                """,
                (
                    request_hash,
                    user_id,
                    expires_at.isoformat(timespec="seconds"),
                ),
            )
            return True

        existing_expiry = datetime.fromisoformat(row["expires_at"])
        if existing_expiry.tzinfo is None:
            existing_expiry = existing_expiry.replace(tzinfo=TIMEZONE)

        existing_user = row["user_id"]
        if (
            existing_user is not None
            and int(existing_user) != int(user_id)
            and existing_expiry >= now_local()
        ):
            return False

        con.execute(
            """
            UPDATE browser_login_requests
            SET user_id=?, expires_at=?
            WHERE request_hash=?
            """,
            (
                user_id,
                expires_at.isoformat(timespec="seconds"),
                request_hash,
            ),
        )
        return True


def consume_browser_login_request(request_id):
    request_hash = _token_hash(str(request_id or ""))

    if using_postgres():
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, expires_at
                    FROM browser_login_requests
                    WHERE request_hash=%s
                    """,
                    (request_hash,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                if row["expires_at"] < now_local():
                    cur.execute(
                        "DELETE FROM browser_login_requests WHERE request_hash=%s",
                        (request_hash,),
                    )
                    return None
                if row["user_id"] is None:
                    return 0

                user_id = int(row["user_id"])
                cur.execute(
                    "DELETE FROM browser_login_requests WHERE request_hash=%s",
                    (request_hash,),
                )
                return user_id

    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT user_id, expires_at
            FROM browser_login_requests
            WHERE request_hash=?
            """,
            (request_hash,),
        ).fetchone()

        if not row:
            return None

        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=TIMEZONE)

        if expires_at < now_local():
            con.execute(
                "DELETE FROM browser_login_requests WHERE request_hash=?",
                (request_hash,),
            )
            return None

        if row["user_id"] is None:
            return 0

        user_id = int(row["user_id"])
        con.execute(
            "DELETE FROM browser_login_requests WHERE request_hash=?",
            (request_hash,),
        )
        return user_id


def validate_telegram_init_data(init_data):
    if not init_data or not BOT_TOKEN:
        return None

    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = data.pop("hash", "")
        if not received_hash:
            return None

        check_string = "\n".join(
            f"{key}={data[key]}" for key in sorted(data.keys())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        auth_date = int(data.get("auth_date", "0") or 0)
        if auth_date:
            age = int(now_local().timestamp()) - auth_date
            if age < -300 or age > 86400:
                return None

        user_raw = data.get("user")
        if not user_raw:
            return None

        user = json.loads(user_raw)
        user_id = int(user["id"])
        return {
            "id": user_id,
            "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", ""),
            "username": user.get("username", ""),
        }
    except Exception:
        return None


def delete_transaction_by_id(user_id, row_id):
    if using_postgres():
        with psycopg.connect(DATABASE_URL) as con:
            with con.cursor() as cur:
                cur.execute(
                    "DELETE FROM transactions WHERE id=%s AND user_id=%s",
                    (row_id, user_id),
                )
                return cur.rowcount > 0

    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "DELETE FROM transactions WHERE id=? AND user_id=?",
            (row_id, user_id),
        )
        return cur.rowcount > 0


def webapp_url():
    if not WEBHOOK_BASE_URL:
        return ""
    return WEBHOOK_BASE_URL.rstrip("/") + "/app"


# ============================================================
# ОБРАБОТКА СУММ И ТЕКСТА
# ============================================================

def normalize_amount(raw):
    s = raw.strip().lower()
    s = s.replace("₽", "").replace("р.", "").replace("руб.", "")
    s = s.replace("рублей", "").replace("рубля", "").replace("руб", "")
    s = s.replace("\u00a0", "").replace(" ", "").replace(",", ".")

    try:
        value = Decimal(s)
    except InvalidOperation:
        return None

    if value <= 0:
        return None

    return int((value * 100).quantize(Decimal("1")))


def extract_amount(text):
    candidates = re.findall(
        r"(?<!\w)(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,](\d{1,2}))?",
        text,
    )
    if not candidates:
        return None

    for whole, frac in candidates:
        raw = whole + (("." + frac) if frac else "")
        value = normalize_amount(raw)
        if value:
            return value

    return None


def clean_note(text):
    note = re.sub(
        r"(?<!\w)(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)"
        r"(?:[.,]\d{1,2})?\s*(?:₽|р\.?|руб(?:лей|ля)?\.?)?",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )

    note = re.sub(
        r"\b(доход|расход|потратил|потратила|купил|купила|"
        r"заплатил|заплатила|оплатил|оплатила|получил|получила|"
        r"пришло|заработал|заработала)\b",
        "",
        note,
        flags=re.IGNORECASE,
    )

    note = re.sub(r"\s+", " ", note).strip(" -—,:;.+")
    return note or "Без описания"


def detect_kind(text):
    t = text.lower().strip()

    if t.startswith("+"):
        return "income"

    if t.startswith("-"):
        return "expense"

    income_score = sum(1 for w in INCOME_WORDS if w in t)
    expense_score = sum(1 for w in EXPENSE_WORDS if w in t)

    return "income" if income_score > expense_score else "expense"


def detect_category(text, kind):
    t = text.lower()
    categories = INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES

    best_category = None
    best_score = 0

    for category, words in categories.items():
        score = sum(1 for word in words if word in t)
        if score > best_score:
            best_score = score
            best_category = category

    if best_category:
        return best_category

    return "💵 Прочее" if kind == "income" else "💸 Прочее"


def money(kopecks):
    value = Decimal(int(kopecks)) / 100

    if value == value.to_integral():
        return f"{int(value):,} ₽".replace(",", " ")

    return (
        f"{value:,.2f} ₽"
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", " ")
    )


def now_local():
    return datetime.now(TIMEZONE)


def period_bounds(mode):
    now = now_local()

    if mode == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    elif mode == "week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    elif mode == "month":
        start = now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )

    else:
        start = datetime(2000, 1, 1, tzinfo=TIMEZONE)

    return start, now


def get_summary(user_id, mode):
    start, end = period_bounds(mode)
    rows = fetch_summary(user_id, start, end)

    income = sum(
        int(r["total"]) for r in rows if r["kind"] == "income"
    )
    expense = sum(
        int(r["total"]) for r in rows if r["kind"] == "expense"
    )

    return rows, income, expense


def render_summary(user_id, mode):
    rows, income, expense = get_summary(user_id, mode)

    titles = {
        "today": "Сегодня",
        "week": "Эта неделя",
        "month": "Этот месяц",
        "all": "За всё время",
    }

    lines = [
        f"📊 <b>{titles.get(mode, 'Отчёт')}</b>",
        "",
        f"🟢 Доходы: <b>{money(income)}</b>",
        f"🔴 Расходы: <b>{money(expense)}</b>",
        f"💰 Баланс: <b>{money(income - expense)}</b>",
    ]

    expense_rows = [r for r in rows if r["kind"] == "expense"]
    if expense_rows:
        lines += ["", "🔻 <b>Расходы по категориям:</b>"]
        for r in expense_rows:
            lines.append(
                f"• {escape(str(r['category']))}: {money(r['total'])}"
            )

    income_rows = [r for r in rows if r["kind"] == "income"]
    if income_rows:
        lines += ["", "🔺 <b>Доходы по категориям:</b>"]
        for r in income_rows:
            lines.append(
                f"• {escape(str(r['category']))}: {money(r['total'])}"
            )

    return "\n".join(lines)


def row_datetime(row):
    value = row["created_at"]

    if isinstance(value, datetime):
        return value.astimezone(TIMEZONE)

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TIMEZONE)

    return dt.astimezone(TIMEZONE)


# ============================================================
# ДИЗАЙН МЕНЮ
# ============================================================

def main_menu_keyboard():
    rows = []

    if webapp_url():
        rows.append([
            InlineKeyboardButton(
                "📱 Открыть FINANS",
                web_app=WebAppInfo(url=webapp_url()),
            )
        ])

    rows += [
        [
            InlineKeyboardButton("💰 Мой баланс", callback_data="balance"),
        ],
        [
            InlineKeyboardButton("➕ Добавить доход", callback_data="add_income"),
            InlineKeyboardButton("➖ Добавить расход", callback_data="add_expense"),
        ],
        [
            InlineKeyboardButton("📊 Статистика", callback_data="stats"),
            InlineKeyboardButton("🧾 История", callback_data="history"),
        ],
        [
            InlineKeyboardButton("🏷 Категории", callback_data="categories"),
            InlineKeyboardButton("📤 Экспорт", callback_data="export"),
        ],
        [
            InlineKeyboardButton("📲 Установить на iPhone", callback_data="install"),
        ],
        [
            InlineKeyboardButton("↩️ Отменить запись", callback_data="undo_ask"),
            InlineKeyboardButton("❓ Помощь", callback_data="help"),
        ],
    ]

    return InlineKeyboardMarkup(rows)

def stats_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Сегодня", callback_data="stat_today"),
            InlineKeyboardButton("Неделя", callback_data="stat_week"),
        ],
        [
            InlineKeyboardButton("Месяц", callback_data="stat_month"),
            InlineKeyboardButton("Всё время", callback_data="stat_all"),
        ],
        [
            InlineKeyboardButton("⬅️ Главное меню", callback_data="menu"),
        ],
    ])


def cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_input")]
    ])


def undo_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data="undo_yes"),
            InlineKeyboardButton("❌ Нет", callback_data="menu"),
        ]
    ])


def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="menu")]
    ])


HELP_TEXT = """
<b>💎 FINANS — личный финансовый помощник</b>

Записывай расходы и доходы обычным сообщением:

<code>кофе 350</code>
<code>продукты 1850</code>
<code>такси 420</code>
<code>зарплата 85000</code>

Можно указывать знак:

<code>-500 бензин</code> — расход
<code>+50000 зарплата</code> — доход

Также можно нажать кнопки
<b>«Добавить доход»</b> или <b>«Добавить расход»</b>.

🔐 У каждого пользователя полностью отдельный учёт.
""".strip()


def welcome_text(user_id, first_name=""):
    _, month_income, month_expense = get_summary(user_id, "month")
    _, all_income, all_expense = get_summary(user_id, "all")

    name = escape(first_name) if first_name else "Пользователь"

    return (
        f"👋 <b>{name}, добро пожаловать!</b>\n\n"
        f"<b>💎 FINANS</b>\n"
        f"Твой личный помощник по учёту денег.\n\n"
        f"📅 <b>Этот месяц</b>\n"
        f"🟢 Доходы: {money(month_income)}\n"
        f"🔴 Расходы: {money(month_expense)}\n"
        f"💰 Баланс: <b>{money(month_income - month_expense)}</b>\n\n"
        f"🏦 Общий баланс: <b>{money(all_income - all_expense)}</b>\n\n"
        f"Выбери действие 👇"
    )


def history_text(user_id):
    rows = fetch_last(user_id, 10)

    if not rows:
        return "🧾 <b>История</b>\n\nПока записей нет."

    out = ["🧾 <b>Последние 10 операций</b>", ""]

    for r in rows:
        sign = "🟢 +" if r["kind"] == "income" else "🔴 −"
        dt = row_datetime(r).strftime("%d.%m %H:%M")

        out.append(
            f"{sign}{money(r['amount_kopecks'])} — "
            f"{escape(str(r['category']))}"
        )
        out.append(
            f"   {escape(str(r['note']))} · {dt}"
        )

    return "\n".join(out)


def categories_text():
    expenses = "\n".join(
        f"• {escape(x)}" for x in EXPENSE_CATEGORIES
    )
    incomes = "\n".join(
        f"• {escape(x)}" for x in INCOME_CATEGORIES
    )

    return (
        f"🏷 <b>Категории</b>\n\n"
        f"<b>Расходы</b>\n{expenses}\n\n"
        f"<b>Доходы</b>\n{incomes}"
    )


# ============================================================
# ОТПРАВКА МЕНЮ / ЭКСПОРТ
# ============================================================

async def send_main_menu(update, context, with_logo=False):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    text = welcome_text(user.id, user.first_name or "")

    # На /start пробуем автоматически использовать фото профиля самого бота
    # как логотип. Если у бота нет фото или Telegram не разрешит получить его,
    # просто покажется обычное красивое меню.
    if with_logo:
        try:
            bot_me = await context.bot.get_me()
            photos = await context.bot.get_user_profile_photos(
                bot_me.id,
                limit=1,
            )

            if photos.total_count > 0 and photos.photos:
                file_id = photos.photos[0][-1].file_id
                await message.reply_photo(
                    photo=file_id,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=main_menu_keyboard(),
                )
                return
        except Exception as exc:
            log.info("Could not use bot profile photo: %s", exc)

    await message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


async def send_export(message, user_id):
    rows = fetch_all(user_id)

    if not rows:
        await message.reply_text(
            "📤 Пока нечего выгружать.",
            reply_markup=back_keyboard(),
        )
        return

    s = io.StringIO()
    writer = csv.writer(s, delimiter=";")
    writer.writerow(["Дата", "Тип", "Сумма", "Категория", "Описание"])

    for r in rows:
        dt = row_datetime(r).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([
            dt,
            "Доход" if r["kind"] == "income" else "Расход",
            str(
                Decimal(int(r["amount_kopecks"])) / 100
            ).replace(".", ","),
            r["category"],
            r["note"],
        ])

    bio = io.BytesIO(s.getvalue().encode("utf-8-sig"))
    bio.name = f"finance_export_{now_local():%Y-%m-%d}.csv"

    await message.reply_document(
        document=InputFile(bio, filename=bio.name),
        caption="📤 Ваша выгрузка финансов.",
    )


# ============================================================
# КОМАНДЫ
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_kind", None)

    if context.args and context.args[0].startswith("login_"):
        request_id = context.args[0][6:]
        if claim_browser_login_request(request_id, update.effective_user.id):
            await update.effective_message.reply_text(
                "✅ <b>Вход в FINANS подтверждён.</b>\n\n"
                "Вернитесь в Safari — приложение откроется автоматически.",
                parse_mode="HTML",
            )
            return

        await update.effective_message.reply_text(
            "⚠️ Не удалось подтвердить вход. Вернитесь в Safari и нажмите "
            "«Войти через Telegram» ещё раз."
        )
        return

    await send_main_menu(update, context, with_logo=True)


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_kind", None)
    await send_main_menu(update, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        HELP_TEXT,
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        f"Ваш Telegram ID: <code>{uid}</code>",
        parse_mode="HTML",
    )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        render_summary(update.effective_user.id, "today"),
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        render_summary(update.effective_user.id, "week"),
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        render_summary(update.effective_user.id, "month"),
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        render_summary(update.effective_user.id, "all"),
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


async def categories_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        categories_text(),
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


async def last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        history_text(update.effective_user.id),
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = fetch_last(update.effective_user.id, 1)

    if not row:
        await update.effective_message.reply_text(
            "↩️ Удалять нечего.",
            reply_markup=back_keyboard(),
        )
        return

    r = row[0]
    sign = "+" if r["kind"] == "income" else "−"

    await update.effective_message.reply_text(
        f"Удалить последнюю запись?\n\n"
        f"<b>{sign}{money(r['amount_kopecks'])}</b>\n"
        f"{escape(str(r['category']))}\n"
        f"📝 {escape(str(r['note']))}",
        parse_mode="HTML",
        reply_markup=undo_keyboard(),
    )


async def install_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if not webapp_url():
        await update.effective_message.reply_text(
            "Веб-приложение пока недоступно: не задан публичный адрес сервиса."
        )
        return

    token = create_web_session(uid)
    login_url = (
        WEBHOOK_BASE_URL.rstrip("/")
        + "/login?token="
        + token
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Открыть FINANS", url=login_url)]
    ])

    await update.effective_message.reply_text(
        "📲 <b>FINANS на экране iPhone</b>\n\n"
        "Нажмите «Открыть FINANS» — вход выполнится автоматически.\n\n"
        "Затем в Safari нажмите «Поделиться» → "
        "«На экран Домой». После этого приложение будет открываться "
        "без кода и без повторного входа.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_export(
        update.effective_message,
        update.effective_user.id,
    )


# ============================================================
# КНОПКИ
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    uid = update.effective_user.id
    data = query.data

    if data == "menu":
        context.user_data.pop("pending_kind", None)
        await query.message.reply_text(
            welcome_text(uid, update.effective_user.first_name or ""),
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "balance":
        _, month_income, month_expense = get_summary(uid, "month")
        _, all_income, all_expense = get_summary(uid, "all")

        await query.message.reply_text(
            "💰 <b>Мой баланс</b>\n\n"
            f"📅 За месяц: <b>{money(month_income - month_expense)}</b>\n"
            f"🟢 Доходы: {money(month_income)}\n"
            f"🔴 Расходы: {money(month_expense)}\n\n"
            f"🏦 За всё время: "
            f"<b>{money(all_income - all_expense)}</b>",
            parse_mode="HTML",
            reply_markup=back_keyboard(),
        )
        return

    if data == "add_income":
        context.user_data["pending_kind"] = "income"

        await query.message.reply_text(
            "➕ <b>Добавление дохода</b>\n\n"
            "Напишите сумму и описание одним сообщением.\n"
            "Например:\n"
            "<code>50000 зарплата</code>\n"
            "<code>12000 за работу</code>",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    if data == "add_expense":
        context.user_data["pending_kind"] = "expense"

        await query.message.reply_text(
            "➖ <b>Добавление расхода</b>\n\n"
            "Напишите сумму и описание одним сообщением.\n"
            "Например:\n"
            "<code>350 кофе</code>\n"
            "<code>2500 продукты</code>",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return

    if data == "cancel_input":
        context.user_data.pop("pending_kind", None)

        await query.message.reply_text(
            "✅ Ввод отменён.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "stats":
        await query.message.reply_text(
            "📊 <b>Статистика</b>\n\nВыберите период:",
            parse_mode="HTML",
            reply_markup=stats_keyboard(),
        )
        return

    if data.startswith("stat_"):
        mode = data.replace("stat_", "", 1)

        if mode not in {"today", "week", "month", "all"}:
            return

        await query.message.reply_text(
            render_summary(uid, mode),
            parse_mode="HTML",
            reply_markup=stats_keyboard(),
        )
        return

    if data == "history":
        await query.message.reply_text(
            history_text(uid),
            parse_mode="HTML",
            reply_markup=back_keyboard(),
        )
        return

    if data == "categories":
        await query.message.reply_text(
            categories_text(),
            parse_mode="HTML",
            reply_markup=back_keyboard(),
        )
        return

    if data == "export":
        await send_export(query.message, uid)
        return

    if data == "undo_ask":
        rows = fetch_last(uid, 1)

        if not rows:
            await query.message.reply_text(
                "↩️ Удалять нечего.",
                reply_markup=back_keyboard(),
            )
            return

        r = rows[0]
        sign = "+" if r["kind"] == "income" else "−"

        await query.message.reply_text(
            "⚠️ <b>Удалить последнюю запись?</b>\n\n"
            f"<b>{sign}{money(r['amount_kopecks'])}</b>\n"
            f"{escape(str(r['category']))}\n"
            f"📝 {escape(str(r['note']))}",
            parse_mode="HTML",
            reply_markup=undo_keyboard(),
        )
        return

    if data == "undo_yes":
        row = delete_last(uid)

        if not row:
            await query.message.reply_text(
                "Удалять нечего.",
                reply_markup=main_menu_keyboard(),
            )
            return

        sign = "+" if row["kind"] == "income" else "−"

        await query.message.reply_text(
            f"✅ Запись удалена:\n"
            f"<b>{sign}{money(row['amount_kopecks'])}</b> — "
            f"{escape(str(row['category']))}",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "install":
        if not webapp_url():
            await query.message.reply_text(
                "Веб-приложение пока недоступно.",
                reply_markup=back_keyboard(),
            )
            return

        token = create_web_session(uid)
        login_url = (
            WEBHOOK_BASE_URL.rstrip("/")
            + "/login?token="
            + token
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Открыть FINANS", url=login_url)],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data="menu")],
        ])

        await query.message.reply_text(
            "📲 <b>Установка FINANS на iPhone</b>\n\n"
            "Нажмите «Открыть FINANS» — вход выполнится автоматически.\n"
            "Потом в Safari: «Поделиться» → «На экран Домой».\n\n"
            "Повторно вводить код не нужно.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    if data == "help":
        await query.message.reply_text(
            HELP_TEXT,
            parse_mode="HTML",
            reply_markup=back_keyboard(),
        )
        return


# ============================================================
# ОБЫЧНЫЕ СООБЩЕНИЯ
# ============================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    text = (update.effective_message.text or "").strip()
    uid = update.effective_user.id

    amount = extract_amount(text)

    if not amount:
        await update.effective_message.reply_text(
            "Не увидел сумму.\n\n"
            "Напишите, например:\n"
            "<code>кофе 350</code>\n"
            "<code>+50000 зарплата</code>",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
        return

    pending_kind = context.user_data.pop("pending_kind", None)

    if pending_kind in {"income", "expense"}:
        kind = pending_kind
    else:
        kind = detect_kind(text)

    category = detect_category(text, kind)
    note = clean_note(text)
    created_at = now_local()

    insert_transaction(
        uid,
        kind,
        amount,
        category,
        note,
        created_at,
    )

    _, income_today, expense_today = get_summary(uid, "today")
    _, income_month, expense_month = get_summary(uid, "month")

    if kind == "income":
        title = f"✅ Доход записан: <b>+{money(amount)}</b>"
    else:
        title = f"✅ Расход записан: <b>−{money(amount)}</b>"

    await update.effective_message.reply_text(
        f"{title}\n"
        f"{escape(category)}\n"
        f"📝 {escape(note)}\n\n"
        f"📅 Сегодня: "
        f"+{money(income_today)} / −{money(expense_today)}\n"
        f"📊 Месяц: "
        f"+{money(income_month)} / −{money(expense_month)}\n"
        f"💰 Баланс месяца: "
        f"<b>{money(income_month - expense_month)}</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


# ============================================================
# СЛУЖЕБНОЕ
# ============================================================

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Открыть главное меню"),
        BotCommand("menu", "Главное меню"),
        BotCommand("today", "Отчёт за сегодня"),
        BotCommand("week", "Отчёт за неделю"),
        BotCommand("month", "Отчёт за месяц"),
        BotCommand("all", "Отчёт за всё время"),
        BotCommand("last", "Последние операции"),
        BotCommand("undo", "Удалить последнюю запись"),
        BotCommand("export", "Выгрузить CSV"),
        BotCommand("categories", "Категории"),
        BotCommand("install", "Установить FINANS на iPhone"),
        BotCommand("help", "Помощь"),
    ])

    if webapp_url():
        try:
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="FINANS",
                    web_app=WebAppInfo(url=webapp_url()),
                )
            )
        except Exception as exc:
            log.warning("Could not set Mini App menu button: %s", exc)


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    log.exception(
        "Unhandled exception while processing update",
        exc_info=context.error,
    )



# ============================================================
# FASTAPI: TELEGRAM WEBHOOK + MINI APP / PWA
# ============================================================

telegram_app = None


def build_telegram_application():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("month", month_cmd))
    app.add_handler(CommandHandler("all", all_cmd))
    app.add_handler(CommandHandler("last", last_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("categories", categories_cmd))
    app.add_handler(CommandHandler("install", install_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )
    app.add_error_handler(error_handler)
    return app


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    init_db()
    init_web_sessions()

    telegram_app = build_telegram_application()
    await telegram_app.initialize()
    await post_init(telegram_app)
    await telegram_app.start()

    if WEBHOOK_BASE_URL:
        webhook_url = WEBHOOK_BASE_URL.rstrip("/") + "/telegram"
        webhook_secret = hashlib.sha256(
            BOT_TOKEN.encode("utf-8")
        ).hexdigest()

        await telegram_app.bot.set_webhook(
            url=webhook_url,
            secret_token=webhook_secret,
            drop_pending_updates=False,
        )
        log.info("Telegram webhook active: %s", webhook_url)
    else:
        await telegram_app.bot.delete_webhook(drop_pending_updates=False)
        await telegram_app.updater.start_polling(drop_pending_updates=False)
        log.info("No public URL; Telegram polling active")

    try:
        yield
    finally:
        if telegram_app:
            if telegram_app.updater and telegram_app.updater.running:
                await telegram_app.updater.stop()
            if telegram_app.running:
                await telegram_app.stop()
            await telegram_app.shutdown()


web = FastAPI(
    title="FINANS",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


async def request_user_id(request: Request):
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        uid = session_user_id(token)
        if uid:
            return uid

    cookie_token = request.cookies.get("finans_session", "")
    if cookie_token:
        uid = session_user_id(cookie_token)
        if uid:
            return uid

    init_data = request.headers.get("x-telegram-init-data", "")
    user = validate_telegram_init_data(init_data)
    if user:
        return int(user["id"])

    raise HTTPException(status_code=401, detail="Unauthorized")


def api_row(row):
    return {
        "id": int(row["id"]),
        "kind": str(row["kind"]),
        "amount": float(Decimal(int(row["amount_kopecks"])) / 100),
        "amount_text": money(row["amount_kopecks"]),
        "category": str(row["category"]),
        "note": str(row["note"]),
        "created_at": row_datetime(row).isoformat(),
    }


@web.get("/")
async def root():
    return RedirectResponse(url="/app", status_code=307)


@web.get("/app")
async def mini_app():
    return FileResponse(WEBAPP_DIR / "index.html")


@web.get("/login")
async def login_by_token(token: str):
    uid = session_user_id(token)
    if not uid:
        raise HTTPException(status_code=401, detail="Invalid login link")

    response = RedirectResponse(url="/app", status_code=302)
    response.set_cookie(
        key="finans_session",
        value=token,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@web.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(
        WEBAPP_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@web.get("/sw.js")
async def service_worker():
    return FileResponse(
        WEBAPP_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@web.get("/icon.svg")
async def icon():
    return FileResponse(WEBAPP_DIR / "icon.svg", media_type="image/svg+xml")


@web.post("/telegram")
async def telegram_webhook(request: Request):
    if not telegram_app:
        raise HTTPException(status_code=503, detail="Bot is starting")

    expected = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()
    received = request.headers.get("x-telegram-bot-api-secret-token", "")

    if WEBHOOK_BASE_URL and not hmac.compare_digest(expected, received):
        raise HTTPException(status_code=403, detail="Invalid secret")

    payload = await request.json()
    update = Update.de_json(payload, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


@web.post("/api/session")
async def api_session(request: Request):
    init_data = request.headers.get("x-telegram-init-data", "")
    user = validate_telegram_init_data(init_data)

    if not user:
        raise HTTPException(status_code=401, detail="Telegram authorization failed")

    token = create_web_session(int(user["id"]))
    return {"token": token, "user": user}


@web.post("/api/handoff")
async def api_create_handoff(request: Request):
    uid = await request_user_id(request)
    ticket = create_browser_handoff(uid)
    return {"handoff": ticket}


@web.post("/api/handoff/consume")
async def api_consume_handoff(request: Request):
    data = await request.json()
    uid = redeem_browser_handoff(data.get("handoff"))

    if not uid:
        raise HTTPException(
            status_code=401,
            detail="Ссылка входа устарела. Откройте FINANS из Telegram ещё раз.",
        )

    token = create_web_session(uid)
    response = Response(
        content=json.dumps({"ok": True}),
        media_type="application/json",
    )
    response.set_cookie(
        key="finans_session",
        value=token,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@web.post("/api/browser-login/start")
async def api_browser_login_start():
    request_id = create_browser_login_request()
    return {
        "request_id": request_id,
        "telegram_url": (
            "https://t.me/finance76tracker89bot?start=login_"
            + request_id
        ),
    }


@web.get("/api/browser-login/status")
async def api_browser_login_status(request_id: str):
    uid = consume_browser_login_request(request_id)

    if uid is None:
        raise HTTPException(status_code=410, detail="Login request expired")

    if uid == 0:
        return {"authorized": False}

    token = create_web_session(uid)
    response = Response(
        content=json.dumps({"authorized": True}),
        media_type="application/json",
    )
    response.set_cookie(
        key="finans_session",
        value=token,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@web.post("/api/code-login")
async def api_code_login(request: Request):
    data = await request.json()
    uid = redeem_login_code(data.get("code"))

    if not uid:
        raise HTTPException(
            status_code=401,
            detail="Код неверный или уже истёк",
        )

    token = create_web_session(uid)
    return {"token": token}


@web.get("/api/dashboard")
async def api_dashboard(request: Request):
    uid = await request_user_id(request)

    _, today_income, today_expense = get_summary(uid, "today")
    _, month_income, month_expense = get_summary(uid, "month")
    _, all_income, all_expense = get_summary(uid, "all")

    rows = fetch_last(uid, 12)

    return {
        "today": {
            "income": today_income / 100,
            "expense": today_expense / 100,
            "balance": (today_income - today_expense) / 100,
        },
        "month": {
            "income": month_income / 100,
            "expense": month_expense / 100,
            "balance": (month_income - month_expense) / 100,
        },
        "all": {
            "income": all_income / 100,
            "expense": all_expense / 100,
            "balance": (all_income - all_expense) / 100,
        },
        "transactions": [api_row(r) for r in rows],
    }


@web.get("/api/history")
async def api_history(request: Request, limit: int = 50):
    uid = await request_user_id(request)
    limit = max(1, min(int(limit), 100))
    rows = fetch_last(uid, limit)
    return {"transactions": [api_row(r) for r in rows]}


@web.post("/api/transaction")
async def api_add_transaction(request: Request):
    uid = await request_user_id(request)
    data = await request.json()

    kind = str(data.get("kind", "")).strip().lower()
    if kind not in {"income", "expense"}:
        raise HTTPException(status_code=400, detail="Invalid transaction kind")

    amount = normalize_amount(str(data.get("amount", "")))
    if not amount:
        raise HTTPException(status_code=400, detail="Invalid amount")

    note = str(data.get("note") or "").strip() or "Без описания"
    category = str(data.get("category") or "").strip()
    if not category:
        category = detect_category(note, kind)

    insert_transaction(
        uid,
        kind,
        amount,
        category,
        note,
        now_local(),
    )

    return {"ok": True}


@web.delete("/api/transaction/{row_id}")
async def api_delete_transaction(row_id: int, request: Request):
    uid = await request_user_id(request)
    deleted = delete_transaction_by_id(uid, row_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Transaction not found")

    return {"ok": True}


@web.get("/api/export")
async def api_export(request: Request):
    uid = await request_user_id(request)
    rows = fetch_all(uid)

    s = io.StringIO()
    writer = csv.writer(s, delimiter=";")
    writer.writerow(["Дата", "Тип", "Сумма", "Категория", "Описание"])

    for r in rows:
        writer.writerow([
            row_datetime(r).strftime("%Y-%m-%d %H:%M:%S"),
            "Доход" if r["kind"] == "income" else "Расход",
            str(Decimal(int(r["amount_kopecks"])) / 100).replace(".", ","),
            r["category"],
            r["note"],
        ])

    body = s.getvalue().encode("utf-8-sig")
    filename = f"finans_{now_local():%Y-%m-%d}.csv"

    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


def main():
    uvicorn.run(
        web,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
