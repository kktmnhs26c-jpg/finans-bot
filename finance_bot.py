import os
import re
import csv
import io
import sqlite3
import logging
import hashlib
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_PATH = os.environ.get("DB_PATH", "finance.db")
PORT = int(os.environ.get("PORT", "10000"))

WEBHOOK_BASE_URL = (
    os.environ.get("WEBHOOK_BASE_URL", "").strip()
    or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
)

TIMEZONE = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Europe/Moscow"))

_ALLOWED_RAW = os.environ.get("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = {
    int(x.strip()) for x in _ALLOWED_RAW.split(",") if x.strip().isdigit()
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("finance_bot")

EXPENSE_CATEGORIES = {
    "☕ Кофе": ["кофе", "капучино", "латте", "американо", "эспрессо", "кофейня", "coffee"],
    "🍔 Еда": ["еда", "продукты", "супермаркет", "обед", "ужин", "завтрак", "бургер", "шаурма", "пицца", "ресторан", "кафе", "доставка", "вода", "хлеб", "молоко", "мясо", "фрукты"],
    "🎮 Игры и развлечения": ["игра", "игры", "steam", "playstation", "xbox", "донат", "кино", "боулинг", "развлеч", "клуб", "бильярд"],
    "✈️ Путешествия": ["путешеств", "отель", "гостиниц", "билет", "самолет", "самолёт", "поезд", "авиабилет", "booking", "airbnb", "тур", "виза"],
    "🚕 Транспорт": ["такси", "uber", "яндекс такси", "бензин", "топливо", "парков", "автобус", "метро", "проезд", "машин", "мойка", "шиномонтаж", "масло", "заправ"],
    "🛍 Покупки": ["одежд", "обув", "покупк", "телефон", "айфон", "iphone", "чехол", "ремешок", "техника", "ozon", "wildberries", "wb", "маркетплейс"],
    "📱 Подписки и связь": ["подписк", "youtube", "spotify", "apple music", "icloud", "telegram premium", "netflix", "связь", "интернет", "сим", "тариф"],
    "🏠 Дом": ["аренд", "квартир", "дом", "коммун", "электрич", "мебель", "ремонт", "быт"],
    "💊 Здоровье": ["аптек", "лекар", "врач", "анализ", "стомат", "витамин", "массаж", "клиник", "мед"],
    "📚 Образование": ["курс", "учеб", "книг", "образован", "универ", "колледж", "школ", "репетитор"],
    "🎁 Подарки": ["подар", "цветы", "букет"],
    "💼 Бизнес": ["бизнес", "реклама", "закуп", "поставщик", "товар", "касса", "магазин бизнес", "зарплата сотруд", "аренда магазина"],
    "💸 Прочее": [],
}

INCOME_CATEGORIES = {
    "💼 Зарплата": ["зарплат", "аванс", "зп", "оклад"],
    "🏪 Бизнес": ["бизнес", "магазин", "выручк", "продаж", "прибыл", "касса"],
    "↩️ Возврат": ["возврат", "вернули", "компенсац"],
    "🎁 Подарок": ["подарили", "подарок", "дали деньги"],
    "💰 Доход": ["доход", "получил", "получила", "пришло", "заработал", "заработала", "выплат", "перевели"],
    "💵 Прочее": [],
}

INCOME_WORDS = (
    "доход", "получил", "получила", "пришло", "заработал", "заработала",
    "зарплата", "зарплату", "аванс", "выручка", "прибыль", "вернули",
    "возврат", "подарили", "начислили", "выплатили",
)
EXPENSE_WORDS = (
    "расход", "потратил", "потратила", "купил", "купила", "заплатил",
    "заплатила", "списали", "оплатил", "оплатила",
)

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
                """, (user_id, kind, amount_kopecks, category, note, created_at))
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("""
                INSERT INTO transactions
                (user_id, kind, amount_kopecks, category, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, kind, amount_kopecks, category, note, created_at.isoformat(timespec="seconds")))

def fetch_summary(user_id, start, end):
    if using_postgres():
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT kind, category, SUM(amount_kopecks) AS total
                    FROM transactions
                    WHERE user_id=%s AND created_at >= %s AND created_at <= %s
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
            WHERE user_id=? AND created_at >= ? AND created_at <= ?
            GROUP BY kind, category
            ORDER BY total DESC
        """, (user_id, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))).fetchall()
    finally:
        con.close()

def fetch_last(user_id, limit=10):
    if using_postgres():
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT * FROM transactions
                    WHERE user_id=%s
                    ORDER BY id DESC
                    LIMIT %s
                """, (user_id, limit))
                return cur.fetchall()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("""
            SELECT * FROM transactions
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
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
                cur.execute("DELETE FROM transactions WHERE id=%s", (row_id,))
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("DELETE FROM transactions WHERE id=?", (row_id,))
    return row

def fetch_all(user_id):
    if using_postgres():
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT * FROM transactions
                    WHERE user_id=%s
                    ORDER BY created_at ASC
                """, (user_id,))
                return cur.fetchall()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("""
            SELECT * FROM transactions
            WHERE user_id=?
            ORDER BY created_at ASC
        """, (user_id,)).fetchall()
    finally:
        con.close()

def allowed(user_id):
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS

async def check_access(update):
    uid = update.effective_user.id if update.effective_user else 0
    if allowed(uid):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("⛔ У вас нет доступа к этому боту.")
    return False

def normalize_amount(raw):
    s = raw.strip().lower().replace("₽", "").replace("р.", "").replace("руб.", "")
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
    candidates = re.findall(r"(?<!\w)(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,](\d{1,2}))?", text)
    if not candidates:
        return None
    for whole, frac in candidates:
        raw = whole + (("." + frac) if frac else "")
        value = normalize_amount(raw)
        if value:
            return value
    return None

def clean_note(text, amount_kopecks):
    note = re.sub(
        r"(?<!\w)(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d{1,2})?\s*(?:₽|р\.?|руб(?:лей|ля)?\.?)?",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    note = re.sub(
        r"\b(доход|расход|потратил|потратила|купил|купила|заплатил|заплатила|"
        r"оплатил|оплатила|получил|получила|пришло|заработал|заработала)\b",
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
    return best_category or ("💵 Прочее" if kind == "income" else "💸 Прочее")

def money(kopecks):
    value = Decimal(int(kopecks)) / 100
    if value == value.to_integral():
        return f"{int(value):,} ₽".replace(",", " ")
    return f"{value:,.2f} ₽".replace(",", "X").replace(".", ",").replace("X", " ")

def now_local():
    return datetime.now(TIMEZONE)

def period_bounds(mode):
    now = now_local()
    if mode == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif mode == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif mode == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = datetime(2000, 1, 1, tzinfo=TIMEZONE)
    return start, now

def get_summary(user_id, mode):
    start, end = period_bounds(mode)
    rows = fetch_summary(user_id, start, end)
    income = sum(int(r["total"]) for r in rows if r["kind"] == "income")
    expense = sum(int(r["total"]) for r in rows if r["kind"] == "expense")
    return rows, income, expense

def render_summary(user_id, mode):
    rows, income, expense = get_summary(user_id, mode)
    titles = {"today": "Сегодня", "week": "Эта неделя", "month": "Этот месяц", "all": "За всё время"}
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
            lines.append(f"• {r['category']}: {money(r['total'])}")
    income_rows = [r for r in rows if r["kind"] == "income"]
    if income_rows:
        lines += ["", "🔺 <b>Доходы по категориям:</b>"]
        for r in income_rows:
            lines.append(f"• {r['category']}: {money(r['total'])}")
    return "\n".join(lines)

def row_datetime(row):
    value = row["created_at"]
    if isinstance(value, datetime):
        return value.astimezone(TIMEZONE)
    return datetime.fromisoformat(value).replace(tzinfo=TIMEZONE)

HELP_TEXT = """
<b>💰 Финансовый бот</b>

Пиши обычным сообщением:
<code>кофе 350</code>
<code>продукты 1850</code>
<code>такси 420</code>
<code>игры 1500</code>
<code>путешествие 25000</code>
<code>зарплата 85000</code>
<code>получил 12000 за работу</code>

Можно использовать знак:
<code>-500 бензин</code> — расход
<code>+50000 зарплата</code> — доход

<b>Команды:</b>
/today — сегодня
/week — неделя
/month — месяц
/all — всё время
/last — последние записи
/undo — удалить последнюю запись
/export — выгрузить CSV
/categories — категории
/id — Telegram ID
/help — помощь
""".strip()

async def start(update, context):
    if not await check_access(update):
        return
    storage = "PostgreSQL ☁️" if using_postgres() else "SQLite ⚠️"
    await update.message.reply_text(HELP_TEXT + f"\n\nХранилище: <b>{storage}</b>", parse_mode="HTML")

async def help_cmd(update, context):
    await start(update, context)

async def id_cmd(update, context):
    uid = update.effective_user.id
    await update.message.reply_text(f"Ваш Telegram ID: <code>{uid}</code>", parse_mode="HTML")

async def today_cmd(update, context):
    if not await check_access(update): return
    await update.message.reply_text(render_summary(update.effective_user.id, "today"), parse_mode="HTML")

async def week_cmd(update, context):
    if not await check_access(update): return
    await update.message.reply_text(render_summary(update.effective_user.id, "week"), parse_mode="HTML")

async def month_cmd(update, context):
    if not await check_access(update): return
    await update.message.reply_text(render_summary(update.effective_user.id, "month"), parse_mode="HTML")

async def all_cmd(update, context):
    if not await check_access(update): return
    await update.message.reply_text(render_summary(update.effective_user.id, "all"), parse_mode="HTML")

async def categories_cmd(update, context):
    if not await check_access(update): return
    exp = "\n".join(f"• {x}" for x in EXPENSE_CATEGORIES)
    inc = "\n".join(f"• {x}" for x in INCOME_CATEGORIES)
    await update.message.reply_text(f"<b>Расходы</b>\n{exp}\n\n<b>Доходы</b>\n{inc}", parse_mode="HTML")

async def last_cmd(update, context):
    if not await check_access(update): return
    rows = fetch_last(update.effective_user.id, 10)
    if not rows:
        await update.message.reply_text("Пока записей нет.")
        return
    out = ["🧾 <b>Последние 10 записей</b>", ""]
    for r in rows:
        sign = "🟢 +" if r["kind"] == "income" else "🔴 −"
        dt = row_datetime(r).strftime("%d.%m %H:%M")
        out.append(f"{sign}{money(r['amount_kopecks'])} — {r['category']}")
        out.append(f"   {r['note']} · {dt}")
    await update.message.reply_text("\n".join(out), parse_mode="HTML")

async def undo_cmd(update, context):
    if not await check_access(update): return
    row = delete_last(update.effective_user.id)
    if not row:
        await update.message.reply_text("Удалять нечего.")
        return
    sign = "+" if row["kind"] == "income" else "−"
    await update.message.reply_text(f"↩️ Удалено: {sign}{money(row['amount_kopecks'])} — {row['category']} ({row['note']})")

async def export_cmd(update, context):
    if not await check_access(update): return
    rows = fetch_all(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Пока нечего выгружать.")
        return
    s = io.StringIO()
    writer = csv.writer(s, delimiter=";")
    writer.writerow(["Дата", "Тип", "Сумма", "Категория", "Описание"])
    for r in rows:
        dt = row_datetime(r).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([
            dt,
            "Доход" if r["kind"] == "income" else "Расход",
            str(Decimal(int(r["amount_kopecks"])) / 100).replace(".", ","),
            r["category"],
            r["note"],
        ])
    bio = io.BytesIO(s.getvalue().encode("utf-8-sig"))
    bio.name = f"finance_export_{now_local():%Y-%m-%d}.csv"
    await update.message.reply_document(document=InputFile(bio, filename=bio.name), caption="📤 Ваша выгрузка финансов.")

async def handle_text(update, context):
    if not await check_access(update):
        return
    text = (update.message.text or "").strip()
    uid = update.effective_user.id
    amount = extract_amount(text)
    if not amount:
        await update.message.reply_text(
            "Не увидел сумму. Напишите, например:\n<code>кофе 350</code>\n<code>+50000 зарплата</code>",
            parse_mode="HTML",
        )
        return

    kind = detect_kind(text)
    category = detect_category(text, kind)
    note = clean_note(text, amount)
    created_at = now_local()

    insert_transaction(uid, kind, amount, category, note, created_at)

    _, income_today, expense_today = get_summary(uid, "today")
    _, income_month, expense_month = get_summary(uid, "month")

    title = f"✅ Доход записан: <b>+{money(amount)}</b>" if kind == "income" else f"✅ Расход записан: <b>−{money(amount)}</b>"

    await update.message.reply_text(
        f"{title}\n"
        f"{category}\n"
        f"📝 {note}\n\n"
        f"Сегодня: +{money(income_today)} / −{money(expense_today)}\n"
        f"Месяц: +{money(income_month)} / −{money(expense_month)}\n"
        f"Баланс месяца: <b>{money(income_month - expense_month)}</b>",
        parse_mode="HTML",
    )

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if WEBHOOK_BASE_URL:
        webhook_url = WEBHOOK_BASE_URL.rstrip("/") + "/telegram"
        webhook_secret = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()
        log.info("Starting webhook: %s", webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=webhook_url,
            secret_token=webhook_secret,
            drop_pending_updates=False,
        )
    else:
        log.info("No webhook URL detected; starting polling")
        app.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()
