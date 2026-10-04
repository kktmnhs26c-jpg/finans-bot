import os
import re
import csv
import io
import sqlite3
import logging
import threading
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------- Settings --------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DB_PATH = os.environ.get("DB_PATH", "finance.db")
PORT = int(os.environ.get("PORT", "10000"))

# Optional: comma-separated Telegram user IDs.
# If empty, anyone who finds the bot can use it.
_ALLOWED_RAW = os.environ.get("ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = {
    int(x.strip()) for x in _ALLOWED_RAW.split(",") if x.strip().isdigit()
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("finance_bot")

# -------------------- Categories --------------------

EXPENSE_CATEGORIES = {
    "☕ Кофе": [
        "кофе", "капучино", "латте", "американо", "эспрессо",
        "кофейня", "старбакс", "coffee",
    ],
    "🍔 Еда": [
        "еда", "продукты", "магазин", "супермаркет", "обед", "ужин",
        "завтрак", "бургер", "шаурма", "пицца", "ресторан", "кафе",
        "доставка", "вода", "хлеб", "молоко", "мясо", "фрукты",
    ],
    "🎮 Игры и развлечения": [
        "игра", "игры", "steam", "ps", "playstation", "xbox", "донат",
        "кино", "боулинг", "развлеч", "клуб", "бильярд",
    ],
    "✈️ Путешествия": [
        "путешеств", "отель", "гостиниц", "билет", "самолет", "самолёт",
        "поезд", "авиабилет", "booking", "airbnb", "тур", "виза",
    ],
    "🚕 Транспорт": [
        "такси", "uber", "яндекс такси", "бензин", "топливо", "газ",
        "парков", "автобус", "метро", "проезд", "машин", "мойка",
        "шиномонтаж", "масло", "заправ",
    ],
    "🛍 Покупки": [
        "одежд", "обув", "покупк", "телефон", "айфон", "iphone",
        "чехол", "ремешок", "техника", "ozon", "wildberries", "wb",
        "маркетплейс",
    ],
    "📱 Подписки и связь": [
        "подписк", "youtube", "spotify", "apple music", "icloud",
        "telegram premium", "netflix", "связь", "интернет", "сим",
        "тариф",
    ],
    "🏠 Дом": [
        "аренд", "квартир", "дом", "коммун", "электрич", "газ счет",
        "мебель", "ремонт", "быт",
    ],
    "💊 Здоровье": [
        "аптек", "лекар", "врач", "анализ", "стомат", "витамин",
        "массаж", "клиник", "мед",
    ],
    "📚 Образование": [
        "курс", "учеб", "книг", "образован", "универ", "колледж",
        "школ", "репетитор",
    ],
    "🎁 Подарки": [
        "подар", "цветы", "букет",
    ],
    "💼 Бизнес": [
        "бизнес", "реклама", "закуп", "поставщик", "товар", "касса",
        "магазин бизнес", "зарплата сотруд", "аренда магазина",
    ],
    "💸 Прочее": [],
}

INCOME_CATEGORIES = {
    "💼 Зарплата": [
        "зарплат", "аванс", "зп", "оклад",
    ],
    "🏪 Бизнес": [
        "бизнес", "магазин", "выручк", "продаж", "прибыл", "касса",
    ],
    "↩️ Возврат": [
        "возврат", "вернули", "компенсац",
    ],
    "🎁 Подарок": [
        "подарили", "подарок", "дали деньги",
    ],
    "💰 Доход": [
        "доход", "получил", "получила", "пришло", "заработал",
        "заработала", "выплат", "перевели",
    ],
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

# -------------------- Database --------------------

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
                amount_kopecks INTEGER NOT NULL,
                category TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_tx_user_date ON transactions(user_id, created_at)"
        )

# -------------------- Helpers --------------------

def allowed(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS

async def check_access(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    if allowed(uid):
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            "⛔ У вас нет доступа к этому боту."
        )
    return False

def normalize_amount(raw: str) -> int | None:
    s = raw.strip().lower().replace("₽", "").replace("р.", "").replace("руб.", "")
    s = s.replace("рублей", "").replace("рубля", "").replace("руб", "")
    s = s.replace("\u00a0", "").replace(" ", "")
    s = s.replace(",", ".")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return int((value * 100).quantize(Decimal("1")))

def extract_amount(text: str) -> int | None:
    # Handles: 350, 1 250, 1250.50, 1250,50
    candidates = re.findall(r"(?<!\w)(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,](\d{1,2}))?", text)
    if not candidates:
        return None

    best = None
    for whole, frac in candidates:
        raw = whole + (("." + frac) if frac else "")
        value = normalize_amount(raw)
        if value:
            best = value
            break
    return best

def clean_note(text: str, amount_kopecks: int) -> str:
    amount = Decimal(amount_kopecks) / 100
    variants = {
        f"{amount:.2f}".replace(".", ","),
        f"{amount:.2f}",
        str(int(amount)) if amount == int(amount) else "",
    }
    note = text
    # Remove the first visible numeric amount, not every number in the note.
    note = re.sub(
        r"(?<!\w)(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d{1,2})?\s*(?:₽|р\.?|руб(?:лей|ля)?\.?)?",
        "",
        note,
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
    note = re.sub(r"\s+", " ", note).strip(" -—,:;.")
    return note or "Без описания"

def detect_kind(text: str) -> str:
    t = text.lower().strip()

    if t.startswith("+"):
        return "income"
    if t.startswith("-"):
        return "expense"

    income_score = sum(1 for w in INCOME_WORDS if w in t)
    expense_score = sum(1 for w in EXPENSE_WORDS if w in t)

    # A clearly income-like keyword takes priority.
    if income_score > expense_score:
        return "income"
    return "expense"

def detect_category(text: str, kind: str) -> str:
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

def money(kopecks: int) -> str:
    value = Decimal(kopecks) / 100
    if value == value.to_integral():
        return f"{int(value):,} ₽".replace(",", " ")
    return f"{value:,.2f} ₽".replace(",", "X").replace(".", ",").replace("X", " ")

def period_bounds(mode: str):
    now = datetime.now()
    if mode == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif mode == "week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif mode == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = datetime(2000, 1, 1)
    return start, now

def get_summary(user_id: int, mode: str):
    start, end = period_bounds(mode)
    with db() as con:
        rows = con.execute(
            """
            SELECT kind, category, SUM(amount_kopecks) AS total
            FROM transactions
            WHERE user_id=? AND created_at>=? AND created_at<=?
            GROUP BY kind, category
            ORDER BY total DESC
            """,
            (user_id, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")),
        ).fetchall()

    income = sum(r["total"] for r in rows if r["kind"] == "income")
    expense = sum(r["total"] for r in rows if r["kind"] == "expense")
    return rows, income, expense

def render_summary(user_id: int, mode: str) -> str:
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
            lines.append(f"• {r['category']}: {money(r['total'])}")

    income_rows = [r for r in rows if r["kind"] == "income"]
    if income_rows:
        lines += ["", "🔺 <b>Доходы по категориям:</b>"]
        for r in income_rows:
            lines.append(f"• {r['category']}: {money(r['total'])}")

    return "\n".join(lines)

# -------------------- Bot commands --------------------

HELP_TEXT = """
<b>💰 Финансовый бот</b>

Просто пиши обычным сообщением:

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

Я сам определю:
• доход или расход
• сумму
• категорию
• дату и время

<b>Команды:</b>
/today — сегодня
/week — неделя
/month — месяц
/all — всё время
/last — последние записи
/undo — удалить последнюю запись
/export — выгрузить CSV
/categories — категории
/id — показать ваш Telegram ID
/help — помощь
""".strip()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(f"Ваш Telegram ID: <code>{uid}</code>", parse_mode="HTML")

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    await update.message.reply_text(render_summary(update.effective_user.id, "today"), parse_mode="HTML")

async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    await update.message.reply_text(render_summary(update.effective_user.id, "week"), parse_mode="HTML")

async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    await update.message.reply_text(render_summary(update.effective_user.id, "month"), parse_mode="HTML")

async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    await update.message.reply_text(render_summary(update.effective_user.id, "all"), parse_mode="HTML")

async def categories_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    exp = "\n".join(f"• {x}" for x in EXPENSE_CATEGORIES)
    inc = "\n".join(f"• {x}" for x in INCOME_CATEGORIES)
    text = f"<b>Расходы</b>\n{exp}\n\n<b>Доходы</b>\n{inc}"
    await update.message.reply_text(text, parse_mode="HTML")

async def last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    uid = update.effective_user.id
    with db() as con:
        rows = con.execute(
            """
            SELECT * FROM transactions
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT 10
            """,
            (uid,),
        ).fetchall()
    if not rows:
        await update.message.reply_text("Пока записей нет.")
        return

    out = ["🧾 <b>Последние 10 записей</b>", ""]
    for r in rows:
        sign = "🟢 +" if r["kind"] == "income" else "🔴 −"
        dt = datetime.fromisoformat(r["created_at"]).strftime("%d.%m %H:%M")
        out.append(f"{sign}{money(r['amount_kopecks'])} — {r['category']}")
        out.append(f"   {r['note']} · {dt}")
    await update.message.reply_text("\n".join(out), parse_mode="HTML")

async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    uid = update.effective_user.id
    with db() as con:
        row = con.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (uid,),
        ).fetchone()
        if not row:
            await update.message.reply_text("Удалять нечего.")
            return
        con.execute("DELETE FROM transactions WHERE id=?", (row["id"],))
    sign = "+" if row["kind"] == "income" else "−"
    await update.message.reply_text(
        f"↩️ Удалено: {sign}{money(row['amount_kopecks'])} — {row['category']} ({row['note']})"
    )

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update): return
    uid = update.effective_user.id
    with db() as con:
        rows = con.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY created_at ASC",
            (uid,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("Пока нечего выгружать.")
        return

    s = io.StringIO()
    writer = csv.writer(s, delimiter=";")
    writer.writerow(["Дата", "Тип", "Сумма", "Категория", "Описание"])
    for r in rows:
        writer.writerow([
            r["created_at"],
            "Доход" if r["kind"] == "income" else "Расход",
            str(Decimal(r["amount_kopecks"]) / 100).replace(".", ","),
            r["category"],
            r["note"],
        ])

    data = s.getvalue().encode("utf-8-sig")
    bio = io.BytesIO(data)
    bio.name = f"finance_export_{datetime.now():%Y-%m-%d}.csv"
    await update.message.reply_document(
        document=InputFile(bio, filename=bio.name),
        caption="📤 Ваша выгрузка финансов."
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    text = (update.message.text or "").strip()
    uid = update.effective_user.id

    amount = extract_amount(text)
    if not amount:
        await update.message.reply_text(
            "Не увидел сумму. Напишите, например:\n"
            "<code>кофе 350</code>\n"
            "<code>+50000 зарплата</code>",
            parse_mode="HTML",
        )
        return

    kind = detect_kind(text)
    category = detect_category(text, kind)
    note = clean_note(text, amount)
    created_at = datetime.now().isoformat(timespec="seconds")

    with db() as con:
        con.execute(
            """
            INSERT INTO transactions
            (user_id, kind, amount_kopecks, category, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (uid, kind, amount, category, note, created_at),
        )

    _, income_today, expense_today = get_summary(uid, "today")
    _, income_month, expense_month = get_summary(uid, "month")

    if kind == "income":
        title = f"✅ Доход записан: <b>+{money(amount)}</b>"
    else:
        title = f"✅ Расход записан: <b>−{money(amount)}</b>"

    reply = (
        f"{title}\n"
        f"{category}\n"
        f"📝 {note}\n\n"
        f"Сегодня: +{money(income_today)} / −{money(expense_today)}\n"
        f"Месяц: +{money(income_month)} / −{money(expense_month)}\n"
        f"Баланс месяца: <b>{money(income_month - expense_month)}</b>"
    )
    await update.message.reply_text(reply, parse_mode="HTML")

# -------------------- Render health server --------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Finance bot is running")
    def log_message(self, format, *args):
        return

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

# -------------------- Main --------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    init_db()

    # Lets Render see an open HTTP port while Telegram works via polling.
    threading.Thread(target=run_health_server, daemon=True).start()

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

    log.info("Finance bot started")
    app.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()
