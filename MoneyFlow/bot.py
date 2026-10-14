# -*- coding: utf-8 -*-
"""
MoneyFlow — shaxsiy moliya boshqaruv Telegram boti.

Texnologiyalar: pyTelegramBotAPI (long polling), SQLAlchemy 2.x, PostgreSQL,
python-dotenv, matplotlib.  Barcha kod shu bitta faylda.
"""
import functools
import html
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import matplotlib

matplotlib.use("Agg")
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

import telebot  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import (  # noqa: E402
    BigInteger, text as sql_text, Boolean, DateTime, Integer, Numeric, String,
    create_engine, delete, func, select, update,
)
from sqlalchemy.exc import OperationalError, SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker  # noqa: E402
from telebot import types  # noqa: E402
from telebot.apihelper import ApiTelegramException  # noqa: E402

# ----------------------------------------------------------------------------
# SOZLAMALAR VA LOG
# ----------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("moneyflow")


def require_env(name: str, example: str) -> str:
    value = os.getenv(name, "").strip().strip('"').strip("'")
    if not value:
        sys.exit(
            f"XATO: '{name}' topilmadi. .env faylga (yoki Railway Variables bo‘limiga) "
            f"quyidagicha yozing:\n{name}={example}"
        )
    return value


def normalize_db_url(url: str) -> str:
    """postgres:// va postgresql:// manzillarini SQLAlchemy (psycopg2) formatiga o‘tkazadi."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    if not url.startswith("postgresql+psycopg2://"):
        sys.exit("XATO: DATABASE_URL 'postgresql://' yoki 'postgres://' bilan boshlanishi kerak.")
    return url


BOT_TOKEN = require_env("BOT_TOKEN", "telegram_bot_token")
DATABASE_URL = normalize_db_url(
    require_env("DATABASE_URL", "postgresql://user:password@host:5432/database")
)

TZ = timezone(timedelta(hours=5))  # O‘zbekiston vaqti (UTC+5, yozgi vaqt yo‘q)
UTC = timezone.utc
PAGE_SIZE = 5
MAX_NOTE = 200
MAX_NAME = 64

CATEGORIES = ["Oziq-ovqat", "Transport", "Kiyim", "Uy", "Aloqa", "Boshqa"]
CAT_EMOJI = {
    "Oziq-ovqat": "🍽", "Transport": "🚌", "Kiyim": "👕",
    "Uy": "🏠", "Aloqa": "📱", "Boshqa": "📦",
}
MONTHS = [
    "Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
    "Iyul", "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr",
]

# ----------------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------------
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,      # uzilgan ulanishni so‘rovdan oldin tekshiradi
    pool_size=5,
    max_overflow=5,
    pool_recycle=300,        # eski ulanishlarni 5 daqiqada yangilaydi
    connect_args={
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    },
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    type: Mapped[str] = mapped_column(String(10))  # income | expense
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    note: Mapped[str | None] = mapped_column(String(MAX_NOTE), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Debt(Base):
    __tablename__ = "debts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    person: Mapped[str] = mapped_column(String(MAX_NAME))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    direction: Mapped[str] = mapped_column(String(5))  # gave (berdim) | took (oldim)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Goal(Base):
    __tablename__ = "goals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(MAX_NAME))
    target: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    saved: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ----------------------------------------------------------------------------
# YORDAMCHI FUNKSIYALAR
# ----------------------------------------------------------------------------
esc = html.escape
AMOUNT_RE = re.compile(r"^\d{1,12}(\.\d{1,2})?$")


def money(v) -> str:
    """125000 -> '125 000 so‘m'."""
    v = Decimal(v)
    s = f"{int(v):,}" if v == v.to_integral_value() else f"{v:,.2f}"
    return s.replace(",", " ") + " so‘m"


def parse_amount(text: str):
    """Musbat, to‘g‘ri formatdagi summani Decimal qilib qaytaradi, aks holda None."""
    t = text.strip().replace(" ", "").replace("\u00a0", "").replace("_", "").replace(",", ".")
    if not AMOUNT_RE.match(t):
        return None
    v = Decimal(t)
    return v if v > 0 else None


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def add_month(dt: datetime) -> datetime:
    return (dt.replace(day=1) + timedelta(days=32)).replace(day=1)


def valid_period(key: str) -> bool:
    if key in ("today", "month", "year", "all"):
        return True
    if re.fullmatch(r"\d{4}", key):
        return 2000 <= int(key) <= 2100
    if re.fullmatch(r"\d{4}-\d{2}", key):
        return 2000 <= int(key[:4]) <= 2100 and 1 <= int(key[5:]) <= 12
    return False


def period_bounds(key: str):
    """Davr uchun (boshlanish, tugash) UTC vaqtlarini qaytaradi; 'all' uchun None."""
    if key == "all":
        return None
    now = datetime.now(TZ)
    if key == "today":
        s = now.replace(hour=0, minute=0, second=0, microsecond=0)
        e = s + timedelta(days=1)
    elif key == "month":
        s = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        e = add_month(s)
    elif key == "year":
        s = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        e = s.replace(year=s.year + 1)
    elif re.fullmatch(r"\d{4}", key):
        y = int(key)
        s, e = datetime(y, 1, 1, tzinfo=TZ), datetime(y + 1, 1, 1, tzinfo=TZ)
    else:
        y, mth = int(key[:4]), int(key[5:7])
        s = datetime(y, mth, 1, tzinfo=TZ)
        e = add_month(s)
    return s.astimezone(UTC), e.astimezone(UTC)


def period_label(key: str) -> str:
    now = datetime.now(TZ)
    if key == "today":
        return f"Bugun ({now:%d.%m.%Y})"
    if key == "month":
        return f"Shu oy ({MONTHS[now.month - 1]} {now.year})"
    if key == "year":
        return f"Shu yil ({now.year})"
    if key == "all":
        return "Umumiy"
    if len(key) == 4:
        return f"{key}-yil"
    return f"{MONTHS[int(key[5:7]) - 1]} {key[:4]}"


def sums(s, uid: int, bounds=None):
    """(daromad, xarajat) yig‘indilari."""
    q = select(Transaction.type, func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.user_id == uid
    )
    if bounds:
        q = q.where(Transaction.created_at >= bounds[0], Transaction.created_at < bounds[1])
    q = q.group_by(Transaction.type)
    r = {t: Decimal(v) for t, v in s.execute(q).all()}
    return r.get("income", Decimal(0)), r.get("expense", Decimal(0))


def category_totals(s, uid: int, bounds=None):
    q = select(Transaction.category, func.sum(Transaction.amount)).where(
        Transaction.user_id == uid, Transaction.type == "expense"
    )
    if bounds:
        q = q.where(Transaction.created_at >= bounds[0], Transaction.created_at < bounds[1])
    q = q.group_by(Transaction.category).order_by(func.sum(Transaction.amount).desc())
    return [(c or "Boshqa", Decimal(v)) for c, v in s.execute(q).all()]


def to_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------
# BOT, HOLATLAR, KLAVIATURALAR
# ----------------------------------------------------------------------------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)

STATE: dict[int, dict] = {}    # foydalanuvchi_id -> {"action": ..., "data": {...}}
FILTERS: dict[int, dict] = {}  # foydalanuvchi_id -> tarix filtri

B_INC, B_EXP, B_DEBT, B_GOAL = "💰 Daromad", "💸 Xarajat", "🤝 Qarzlar", "🎯 Jamg‘arma"
B_STAT, B_HIST, B_BAL, B_SET = "📊 Statistika", "📜 Tarix", "💳 Balans", "⚙️ Sozlamalar"
CANCEL_ROW = [("❌ Bekor qilish", "cancel")]


def main_menu():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    m.add(
        types.KeyboardButton(B_INC), types.KeyboardButton(B_EXP),
        types.KeyboardButton(B_DEBT), types.KeyboardButton(B_GOAL),
        types.KeyboardButton(B_STAT), types.KeyboardButton(B_HIST),
        types.KeyboardButton(B_BAL), types.KeyboardButton(B_SET),
    )
    return m


def kb(*rows):
    m = types.InlineKeyboardMarkup()
    for row in rows:
        m.row(*[types.InlineKeyboardButton(t, callback_data=d) for t, d in row])
    return m


def send(chat_id, text, markup=None):
    return bot.send_message(chat_id, text, reply_markup=markup)


def edit(call, text, markup=None):
    try:
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id, reply_markup=markup
        )
    except ApiTelegramException as e:
        if "message is not modified" in str(e):
            return
        log.warning("edit_message_text xatosi: %s", e)
        send(call.message.chat.id, text, markup)


def chat_of(obj) -> int:
    return obj.chat.id if isinstance(obj, types.Message) else obj.message.chat.id


def safe(fn):
    """Handler ichidagi xatolarni logga yozadi va foydalanuvchiga tushunarli xabar beradi."""
    @functools.wraps(fn)
    def wrapper(obj, *args, **kwargs):
        try:
            try:
                return fn(obj, *args, **kwargs)
            except OperationalError:
                # Ulanish vaqtincha uzilgan bo‘lishi mumkin: ulanishlarni yangilab, 1 marta qayta urinamiz.
                log.warning("Database ulanishi uzildi, qayta urinilmoqda", exc_info=True)
                engine.dispose()
                return fn(obj, *args, **kwargs)
        except Exception as e:  # noqa: BLE001
            if isinstance(e, ApiTelegramException) and "message is not modified" in str(e):
                return None
            code = type(e).__name__
            if isinstance(e, SQLAlchemyError):
                log.exception("Database xatosi")
                text = (
                    "⚠️ Ma’lumotlar bazasida xatolik yuz berdi. Iltimos, birozdan so‘ng qayta urinib ko‘ring."
                    f"\n<code>Kod: {code}</code>"
                )
            else:
                log.exception("Kutilmagan xato")
                text = f"⚠️ Xatolik yuz berdi. Iltimos, qayta urinib ko‘ring.\n<code>Kod: {code}</code>"
            try:
                STATE.pop(obj.from_user.id, None)
                send(chat_of(obj), text)
            except Exception:  # noqa: BLE001
                log.exception("Xato haqida xabar yuborib bo‘lmadi")
            return None
    return wrapper


def set_state(uid: int, action: str, **data):
    STATE[uid] = {"action": action, "data": data}


def cancel_markup():
    return kb(CANCEL_ROW)


def default_filter(type_="all"):
    return {"type": type_, "period": "all", "category": None, "min": None, "sort": "date"}


def get_filter(uid: int) -> dict:
    return FILTERS.setdefault(uid, default_filter())


# ----------------------------------------------------------------------------
# DAROMAD / XARAJAT SAQLASH VA HISOBOT
# ----------------------------------------------------------------------------
def tx_line(t: Transaction) -> str:
    icon = "💰" if t.type == "income" else "💸"
    cat = f" • {CAT_EMOJI.get(t.category, '📦')} {esc(t.category)}" if t.category else ""
    note = f"\n      📝 {esc(t.note)}" if t.note else ""
    return f"<b>#{t.id}</b> {icon} {money(t.amount)}{cat}\n      🕒 {fmt_dt(t.created_at)}{note}"


def finish_tx(chat: int, uid: int, ttype: str, category, amount: Decimal, note):
    with SessionLocal() as s:
        tx = Transaction(
            user_id=uid, type=ttype, category=category,
            amount=amount, note=note, created_at=utcnow(),
        )
        s.add(tx)
        s.commit()
        t_inc, t_exp = sums(s, uid, period_bounds("today"))
        a_inc, a_exp = sums(s, uid)
    title = "Daromad" if ttype == "income" else "Xarajat"
    icon = "💰" if ttype == "income" else "💸"
    lines = [f"✅ <b>{title} saqlandi</b>", "", f"{icon} Yangi yozuv: <b>{title}</b>"]
    if category:
        lines.append(f"🏷 Kategoriya: {CAT_EMOJI.get(category, '📦')} {esc(category)}")
    lines.append(f"💵 Summa: <b>{money(amount)}</b>")
    if note:
        lines.append(f"📝 Izoh: {esc(note)}")
    lines += [
        f"🕒 Sana va vaqt: {fmt_dt(tx.created_at)}",
        "",
        "📅 <b>Bugun</b>",
        f"💰 Daromad: {money(t_inc)}",
        f"💸 Xarajat: {money(t_exp)}",
        "",
        f"💳 <b>Joriy balans: {money(a_inc - a_exp)}</b>",
    ]
    send(chat, "\n".join(lines), main_menu())


def note_prompt(chat: int, label: str):
    send(
        chat,
        f"📝 Izoh kiriting (ixtiyoriy, {MAX_NOTE} belgigacha).",
        kb([(label, "skip")], CANCEL_ROW),
    )


def finish_note(uid: int, chat: int, note):
    """Izoh bosqichini yakunlaydi: yangi yozuvni saqlaydi yoki izohni yangilaydi."""
    st = STATE.pop(uid, None)
    if not st:
        return
    d, act = st["data"], st["action"]
    if act == "inc_note":
        finish_tx(chat, uid, "income", None, d["amount"], note)
    elif act == "exp_note":
        finish_tx(chat, uid, "expense", d["category"], d["amount"], note)
    elif act == "edit_note":
        show_after_edit(chat, uid, d["id"], {"note": note})


def show_after_edit(chat: int, uid: int, tx_id: int, values: dict):
    with SessionLocal() as s:
        res = s.execute(
            update(Transaction)
            .where(Transaction.id == tx_id, Transaction.user_id == uid)
            .values(**values)
        )
        s.commit()
        updated = res.rowcount
        t = s.scalar(select(Transaction).where(Transaction.id == tx_id, Transaction.user_id == uid))
    if not updated or t is None:
        send(chat, "❌ Yozuv topilmadi.")
        return
    send(chat, "✅ <b>Yozuv yangilandi</b>\n\n" + tx_line(t), kb([("📋 Tarixga qaytish", "h:p:0")]))


# ----------------------------------------------------------------------------
# MENYU HANDLERLARI
# ----------------------------------------------------------------------------
@bot.message_handler(commands=["start", "help"])
@safe
def cmd_start(m):
    if m.chat.type != "private":
        return
    STATE.pop(m.from_user.id, None)
    name = esc(m.from_user.first_name or "do‘st")
    send(
        m.chat.id,
        f"👋 Assalomu alaykum, <b>{name}</b>!\n\n"
        "💼 <b>MoneyFlow</b> — shaxsiy moliyangizni boshqarish boti.\n\n"
        "Daromad va xarajatlaringizni yozib boring, qarzlar va jamg‘armalarni nazorat qiling, "
        "statistika va grafiklarni ko‘ring.\n\n"
        "Quyidagi menyudan tanlang 👇",
        main_menu(),
    )


@bot.message_handler(commands=["cancel"])
@safe
def cmd_cancel(m):
    if m.chat.type != "private":
        return
    STATE.pop(m.from_user.id, None)
    send(m.chat.id, "❌ Amal bekor qilindi.", main_menu())


def menu_income(m):
    send(m.chat.id, "💰 <b>Daromad</b> bo‘limi", kb(
        [("➕ Daromad qo‘shish", "inc:add")],
        [("📋 Daromadlar tarixi", "inc:hist")],
    ))


def menu_expense(m):
    send(m.chat.id, "💸 <b>Xarajat</b> bo‘limi", kb(
        [("➕ Xarajat qo‘shish", "exp:add")],
        [("📋 Xarajatlar tarixi", "exp:hist")],
    ))


def menu_debts(m):
    send(m.chat.id, "🤝 <b>Qarzlar</b> bo‘limi", kb(
        [("📤 Qarz berdim", "d:g"), ("📥 Qarz oldim", "d:t")],
        [("📋 Ochiq qarzlar", "d:l")],
    ))


def menu_goals(m):
    send(m.chat.id, "🎯 <b>Jamg‘arma</b> bo‘limi", kb(
        [("🆕 Yangi maqsad", "g:new")],
        [("📋 Maqsadlarim", "g:list")],
    ))


def stats_menu_markup():
    return kb(
        [("📅 Bugun", "st:p:today"), ("🗓 Shu oy", "st:p:month")],
        [("📈 Umumiy", "st:p:all"), ("🔍 Oy / yil bo‘yicha", "st:y")],
    )


def menu_stats(m):
    send(m.chat.id, "📊 <b>Statistika</b>\nQaysi davr bo‘yicha ko‘rmoqchisiz?", stats_menu_markup())


def menu_history(m):
    uid = m.from_user.id
    FILTERS[uid] = default_filter()
    text, markup = render_history(uid, 0)
    send(m.chat.id, text, markup)


def menu_balance(m):
    uid = m.from_user.id
    with SessionLocal() as s:
        a_inc, a_exp = sums(s, uid)
        t_inc, t_exp = sums(s, uid, period_bounds("today"))
        m_inc, m_exp = sums(s, uid, period_bounds("month"))
    send(
        m.chat.id,
        "💳 <b>Balans</b>\n\n"
        f"💰 Jami daromad: {money(a_inc)}\n"
        f"💸 Jami xarajat: {money(a_exp)}\n"
        f"💳 <b>Sof balans: {money(a_inc - a_exp)}</b>\n\n"
        f"📅 Bugun: +{money(t_inc)} / −{money(t_exp)}\n"
        f"🗓 Shu oy: +{money(m_inc)} / −{money(m_exp)}",
    )


def menu_settings(m):
    send(m.chat.id, "⚙️ <b>Sozlamalar</b>", kb(
        [("📤 JSON eksport", "set:export")],
        [("🗑 Barcha ma’lumotlarni o‘chirish", "set:wipe")],
        [("ℹ️ Bot haqida", "set:about")],
    ))


MENU_ROUTES = {
    B_INC: menu_income, B_EXP: menu_expense, B_DEBT: menu_debts, B_GOAL: menu_goals,
    B_STAT: menu_stats, B_HIST: menu_history, B_BAL: menu_balance, B_SET: menu_settings,
}


# ----------------------------------------------------------------------------
# MATNLI XABARLAR (holatlar)
# ----------------------------------------------------------------------------
@bot.message_handler(content_types=["text"])
@safe
def on_text(m):
    if m.chat.type != "private":
        return
    uid, chat, text = m.from_user.id, m.chat.id, (m.text or "").strip()

    route = MENU_ROUTES.get(text)
    if route:
        STATE.pop(uid, None)
        route(m)
        return

    st = STATE.get(uid)
    if not st:
        send(chat, "Iltimos, menyudan tanlang 👇", main_menu())
        return
    handle_state(m, uid, chat, text, st)


def handle_state(m, uid: int, chat: int, text: str, st: dict):
    act, d = st["action"], st["data"]

    # ---- summa kutilayotgan holatlar
    if act in ("inc_amount", "exp_amount", "edit_amount", "debt_amount",
               "goal_target", "goal_add", "flt_min"):
        amount = parse_amount(text)
        if amount is None:
            send(
                chat,
                "❌ Summa noto‘g‘ri. Faqat musbat son kiriting, masalan: <code>125000</code> yoki <code>12500.50</code>",
                cancel_markup(),
            )
            return

        if act == "inc_amount":
            set_state(uid, "inc_note", amount=amount)
            note_prompt(chat, "⏭ O‘tkazib yuborish")
        elif act == "exp_amount":
            set_state(uid, "exp_note", amount=amount, category=d["category"])
            note_prompt(chat, "⏭ O‘tkazib yuborish")
        elif act == "edit_amount":
            STATE.pop(uid, None)
            show_after_edit(chat, uid, d["id"], {"amount": amount})
        elif act == "debt_amount":
            STATE.pop(uid, None)
            with SessionLocal() as s:
                debt = Debt(
                    user_id=uid, person=d["person"], amount=amount,
                    direction=d["direction"], is_paid=False, created_at=utcnow(),
                )
                s.add(debt)
                s.commit()
            what = "Qarz berdingiz" if d["direction"] == "gave" else "Qarz oldingiz"
            send(
                chat,
                f"✅ <b>Qarz qayd etildi</b>\n\n{what}: {esc(d['person'])}\n💵 Summa: {money(amount)}",
                kb([("📋 Ochiq qarzlar", "d:l")]),
            )
        elif act == "goal_target":
            STATE.pop(uid, None)
            with SessionLocal() as s:
                goal = Goal(user_id=uid, name=d["name"], target=amount,
                            saved=Decimal("0"), created_at=utcnow())
                s.add(goal)
                s.commit()
            send(
                chat,
                f"✅ <b>Maqsad yaratildi</b>\n\n🎯 {esc(d['name'])}\n💵 Kerakli summa: {money(amount)}",
                kb([("📋 Maqsadlarim", "g:list")]),
            )
        elif act == "goal_add":
            STATE.pop(uid, None)
            with SessionLocal() as s:
                res = s.execute(
                    update(Goal)
                    .where(Goal.id == d["id"], Goal.user_id == uid)
                    .values(saved=Goal.saved + amount)
                )
                s.commit()
                updated = res.rowcount
                g = s.scalar(select(Goal).where(Goal.id == d["id"], Goal.user_id == uid))
            if not updated or g is None:
                send(chat, "❌ Maqsad topilmadi.")
                return
            send(
                chat,
                f"✅ Jamg‘armaga <b>{money(amount)}</b> qo‘shildi.\n\n" + goal_text(g),
                kb([("📋 Maqsadlarim", "g:list")]),
            )
        elif act == "flt_min":
            STATE.pop(uid, None)
            get_filter(uid)["min"] = amount
            text2, markup = render_history(uid, 0)
            send(chat, text2, markup)
        return

    # ---- matn kutilayotgan holatlar
    if act in ("inc_note", "exp_note", "edit_note"):
        note = None if text == "-" else text
        if note and len(note) > MAX_NOTE:
            send(chat, f"❌ Izoh juda uzun (maksimum {MAX_NOTE} belgi). Qisqaroq yozing.", cancel_markup())
            return
        finish_note(uid, chat, note)
    elif act == "debt_person":
        if not text or len(text) > MAX_NAME:
            send(chat, f"❌ Ism 1 dan {MAX_NAME} belgigacha bo‘lishi kerak.", cancel_markup())
            return
        set_state(uid, "debt_amount", direction=d["direction"], person=text)
        send(chat, "💵 Qarz summasini kiriting (masalan: <code>500000</code>):", cancel_markup())
    elif act == "goal_name":
        if not text or len(text) > MAX_NAME:
            send(chat, f"❌ Nom 1 dan {MAX_NAME} belgigacha bo‘lishi kerak.", cancel_markup())
            return
        set_state(uid, "goal_target", name=text)
        send(chat, "💵 Kerakli summani kiriting (masalan: <code>5000000</code>):", cancel_markup())
    else:
        STATE.pop(uid, None)
        send(chat, "Iltimos, menyudan tanlang 👇", main_menu())


# ----------------------------------------------------------------------------
# TARIX (filtr, sahifalash)
# ----------------------------------------------------------------------------
def tx_conditions(uid: int, f: dict) -> list:
    conds = [Transaction.user_id == uid]
    if f["type"] in ("income", "expense"):
        conds.append(Transaction.type == f["type"])
    b = period_bounds(f["period"])
    if b:
        conds += [Transaction.created_at >= b[0], Transaction.created_at < b[1]]
    if f["category"]:
        conds.append(Transaction.category == f["category"])
    if f["min"]:
        conds.append(Transaction.amount >= f["min"])
    return conds


TYPE_NAMES = {"all": "Hammasi", "income": "Daromad", "expense": "Xarajat"}
PERIOD_NAMES = {"all": "Hammasi", "today": "Bugun", "month": "Shu oy", "year": "Shu yil"}


def filter_text(f: dict) -> str:
    parts = [f"Tur: {TYPE_NAMES[f['type']]}", f"Davr: {PERIOD_NAMES[f['period']]}"]
    if f["category"]:
        parts.append(f"Kategoriya: {esc(f['category'])}")
    if f["min"]:
        parts.append(f"Min summa: {money(f['min'])}")
    parts.append("Saralash: " + ("sana bo‘yicha" if f["sort"] == "date" else "summa bo‘yicha"))
    return " • ".join(parts)


def render_history(uid: int, page: int):
    f = get_filter(uid)
    conds = tx_conditions(uid, f)
    order = (
        [Transaction.created_at.desc(), Transaction.id.desc()]
        if f["sort"] == "date"
        else [Transaction.amount.desc(), Transaction.id.desc()]
    )
    with SessionLocal() as s:
        count = s.scalar(select(func.count()).select_from(Transaction).where(*conds)) or 0
        pages = max(1, -(-count // PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        rows = s.scalars(
            select(Transaction).where(*conds).order_by(*order)
            .offset(page * PAGE_SIZE).limit(PAGE_SIZE)
        ).all()
        agg = {
            t: Decimal(v)
            for t, v in s.execute(
                select(Transaction.type, func.sum(Transaction.amount))
                .where(*conds).group_by(Transaction.type)
            ).all()
        }

    lines = ["📜 <b>Tarix</b>", f"<i>{filter_text(f)}</i>", ""]
    if not rows:
        lines.append("Hech narsa topilmadi.")
    else:
        lines += [tx_line(t) + "\n" for t in rows]
        lines.append(
            f"💰 {money(agg.get('income', 0))}  |  💸 {money(agg.get('expense', 0))}  "
            f"|  Jami: {count} ta"
        )

    buttons = [[(f"✏️ #{t.id}", f"tx:e:{t.id}"), (f"🗑 #{t.id}", f"tx:d:{t.id}")] for t in rows]
    nav = []
    if page > 0:
        nav.append(("⬅️", f"h:p:{page - 1}"))
    nav.append((f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("➡️", f"h:p:{page + 1}"))
    buttons.append(nav)
    buttons.append([("🔎 Filtr", "h:f"), ("📤 JSON eksport", "h:x")])
    return "\n".join(lines), kb(*buttons)


def render_filter(uid: int):
    f = get_filter(uid)

    def mark(cond, label):
        return ("✅ " if cond else "") + label

    text = f"🔎 <b>Tarix filtri</b>\n\n<i>{filter_text(f)}</i>"
    markup = kb(
        [(mark(f["type"] == "all", "Hammasi"), "h:t:all"),
         (mark(f["type"] == "income", "💰 Daromad"), "h:t:income"),
         (mark(f["type"] == "expense", "💸 Xarajat"), "h:t:expense")],
        [(mark(f["period"] == "today", "Bugun"), "h:pr:today"),
         (mark(f["period"] == "month", "Shu oy"), "h:pr:month"),
         (mark(f["period"] == "year", "Shu yil"), "h:pr:year"),
         (mark(f["period"] == "all", "Hammasi"), "h:pr:all")],
        [("🏷 Kategoriya", "h:c"), ("🔢 Min summa", "h:min")],
        [(mark(f["sort"] == "date", "📅 Sana bo‘yicha"), "h:s:date"),
         (mark(f["sort"] == "amount", "💵 Summa bo‘yicha"), "h:s:amount")],
        [("🧹 Tozalash", "h:r"), ("📋 Natijani ko‘rish", "h:p:0")],
    )
    return text, markup


# ----------------------------------------------------------------------------
# JAMG‘ARMA MATNI
# ----------------------------------------------------------------------------
def goal_text(g: Goal) -> str:
    pct = min(100, int(g.saved * 100 / g.target)) if g.target > 0 else 0
    filled = pct // 10
    bar = "▓" * filled + "░" * (10 - filled)
    text = (
        f"🎯 <b>{esc(g.name)}</b>\n"
        f"{bar} <b>{pct}%</b>\n"
        f"💰 Yig‘ilgan: {money(g.saved)}\n"
        f"🏁 Maqsad: {money(g.target)}"
    )
    if g.saved >= g.target:
        text += "\n🎉 Maqsadga erishildi!"
    else:
        text += f"\n⏳ Qoldi: {money(g.target - g.saved)}"
    return text


def render_goals(uid: int):
    with SessionLocal() as s:
        goals = s.scalars(
            select(Goal).where(Goal.user_id == uid).order_by(Goal.created_at.desc()).limit(15)
        ).all()
    if not goals:
        return "🎯 Hozircha jamg‘arma maqsadlari yo‘q.", kb([("🆕 Yangi maqsad", "g:new")])
    text = "🎯 <b>Jamg‘arma maqsadlarim</b>\n\n" + "\n\n".join(goal_text(g) for g in goals)
    rows = []
    for g in goals:
        short = g.name if len(g.name) <= 18 else g.name[:17] + "…"
        rows.append([(f"➕ {short}", f"g:add:{g.id}"), ("🗑", f"g:del:{g.id}")])
    rows.append([("🆕 Yangi maqsad", "g:new")])
    return text, kb(*rows)


# ----------------------------------------------------------------------------
# QARZLAR MATNI
# ----------------------------------------------------------------------------
def render_debts(uid: int):
    with SessionLocal() as s:
        rows = s.scalars(
            select(Debt).where(Debt.user_id == uid, Debt.is_paid.is_(False))
            .order_by(Debt.created_at.desc()).limit(20)
        ).all()
        agg = {
            d: Decimal(v)
            for d, v in s.execute(
                select(Debt.direction, func.sum(Debt.amount))
                .where(Debt.user_id == uid, Debt.is_paid.is_(False))
                .group_by(Debt.direction)
            ).all()
        }
    gave, took = agg.get("gave", Decimal(0)), agg.get("took", Decimal(0))
    if not rows:
        return "🤝 Ochiq qarzlar yo‘q. 👍", kb(
            [("📤 Qarz berdim", "d:g"), ("📥 Qarz oldim", "d:t")]
        )
    lines = ["🤝 <b>Ochiq qarzlar</b>", ""]
    for d in rows:
        if d.direction == "gave":
            who = f"📤 <b>{esc(d.person)}</b> sizdan qarz oldi"
        else:
            who = f"📥 Siz <b>{esc(d.person)}</b>dan qarz oldingiz"
        lines.append(f"<b>#{d.id}</b> {who}\n      💵 {money(d.amount)} • {fmt_dt(d.created_at)}")
    net = gave - took
    if net > 0:
        balance = f"Sizga qaytarilishi kerak: <b>{money(net)}</b>"
    elif net < 0:
        balance = f"Siz qaytarishingiz kerak: <b>{money(-net)}</b>"
    else:
        balance = "Hisob teng."
    lines += [
        "",
        f"📤 Berilgan qarzlar jami: {money(gave)}",
        f"📥 Olingan qarzlar jami: {money(took)}",
        f"⚖️ Umumiy hisob: {balance}",
    ]
    buttons = []
    for d in rows:
        short = d.person if len(d.person) <= 16 else d.person[:15] + "…"
        buttons.append([(f"✅ Qaytarildi: {short} (#{d.id})", f"d:p:{d.id}")])
    buttons.append([("📤 Qarz berdim", "d:g"), ("📥 Qarz oldim", "d:t")])
    return "\n".join(lines), kb(*buttons)


# ----------------------------------------------------------------------------
# STATISTIKA VA GRAFIK
# ----------------------------------------------------------------------------
def render_stats(uid: int, key: str):
    b = period_bounds(key)
    with SessionLocal() as s:
        inc, exp = sums(s, uid, b)
        cats = category_totals(s, uid, b)
    lines = [
        f"📊 <b>Statistika — {esc(period_label(key))}</b>",
        "",
        f"💰 Daromad: {money(inc)}",
        f"💸 Xarajat: {money(exp)}",
        f"💳 <b>Sof balans: {money(inc - exp)}</b>",
        "",
        "🏷 <b>Xarajatlar kategoriyalar bo‘yicha:</b>",
    ]
    if not cats:
        lines.append("Xarajatlar yo‘q.")
    for c, v in cats:
        pct = int(v * 100 / exp) if exp > 0 else 0
        lines.append(f"{CAT_EMOJI.get(c, '📦')} {esc(c)}: {money(v)} ({pct}%)")
    return "\n".join(lines), kb(
        [("📊 Grafik", f"st:g:{key}")],
        [("⬅️ Statistika menyusi", "st:menu")],
    )


def make_chart(inc: Decimal, exp: Decimal, cats, title: str) -> io.BytesIO:
    # Figure() — pyplot ishlatilmaydi, shuning uchun ko‘p oqimli rejimda xavfsiz.
    fig = Figure(figsize=(10, 4.8), dpi=110)
    ax1 = fig.add_subplot(1, 2, 1)
    ax2 = fig.add_subplot(1, 2, 2)

    if cats:
        ax1.pie(
            [float(v) for _, v in cats], labels=[c for c, _ in cats],
            autopct="%1.0f%%", startangle=90,
        )
        ax1.set_title("Xarajatlar (kategoriyalar bo'yicha)")
    else:
        ax1.text(0.5, 0.5, "Xarajat yo'q", ha="center", va="center")
        ax1.axis("off")

    values = [float(inc), float(exp)]
    ax2.bar(["Daromad", "Xarajat"], values, color=["#2e9e5b", "#d9534f"])
    ax2.set_title("Daromad va xarajat")
    ax2.set_ylabel("so'm")
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{int(x):,}".replace(",", " ")))
    for i, v in enumerate(values):
        ax2.text(i, v, f"{int(v):,}".replace(",", " "), ha="center", va="bottom", fontsize=9)

    fig.suptitle(title)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    buf.seek(0)
    buf.name = "moneyflow_grafik.png"
    return buf


# ----------------------------------------------------------------------------
# EKSPORT
# ----------------------------------------------------------------------------
def export_json(chat: int, uid: int):
    with SessionLocal() as s:
        txs = s.scalars(
            select(Transaction).where(Transaction.user_id == uid).order_by(Transaction.created_at)
        ).all()
        debts = s.scalars(
            select(Debt).where(Debt.user_id == uid).order_by(Debt.created_at)
        ).all()
        goals = s.scalars(
            select(Goal).where(Goal.user_id == uid).order_by(Goal.created_at)
        ).all()
    data = {
        "exported_at": datetime.now(TZ).isoformat(),
        "currency": "UZS",
        "transactions": [
            {
                "id": t.id, "type": t.type, "category": t.category,
                "amount": str(t.amount), "note": t.note,
                "created_at": t.created_at.astimezone(TZ).isoformat(),
            } for t in txs
        ],
        "debts": [
            {
                "id": d.id, "person": d.person, "amount": str(d.amount),
                "direction": d.direction, "is_paid": d.is_paid,
                "created_at": d.created_at.astimezone(TZ).isoformat(),
                "paid_at": d.paid_at.astimezone(TZ).isoformat() if d.paid_at else None,
            } for d in debts
        ],
        "goals": [
            {
                "id": g.id, "name": g.name, "target": str(g.target), "saved": str(g.saved),
                "created_at": g.created_at.astimezone(TZ).isoformat(),
            } for g in goals
        ],
    }
    buf = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    buf.name = f"moneyflow_{datetime.now(TZ):%Y%m%d_%H%M}.json"
    bot.send_document(chat, buf, caption="📤 Ma’lumotlaringiz (JSON formatida)")


# ----------------------------------------------------------------------------
# CALLBACK HANDLERLAR
# ----------------------------------------------------------------------------
def cb_noop(call, uid, chat, a):
    pass


def cb_cancel(call, uid, chat, a):
    STATE.pop(uid, None)
    edit(call, "❌ Amal bekor qilindi.")


def cb_skip(call, uid, chat, a):
    st = STATE.get(uid)
    if not st or not st["action"].endswith("_note"):
        edit(call, "⌛ Bu amal eskirgan. Menyudan qaytadan boshlang.")
        return
    edit(call, call.message.text or "📝 Izoh o‘tkazib yuborildi.")
    finish_note(uid, chat, None)


def cb_income(call, uid, chat, a):
    if a[0] == "add":
        set_state(uid, "inc_amount")
        send(chat, "💰 Daromad summasini kiriting (masalan: <code>1500000</code>):", cancel_markup())
    elif a[0] == "hist":
        FILTERS[uid] = default_filter("income")
        text, markup = render_history(uid, 0)
        send(chat, text, markup)


def category_markup(prefix: str):
    rows, row = [], []
    for i, c in enumerate(CATEGORIES):
        row.append((f"{CAT_EMOJI[c]} {c}", f"{prefix}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def cb_expense(call, uid, chat, a):
    if a[0] == "add":
        STATE.pop(uid, None)
        send(chat, "🏷 Xarajat kategoriyasini tanlang:", kb(*category_markup("ec"), CANCEL_ROW))
    elif a[0] == "hist":
        FILTERS[uid] = default_filter("expense")
        text, markup = render_history(uid, 0)
        send(chat, text, markup)


def cb_expense_category(call, uid, chat, a):
    idx = to_int(a[0]) if a else None
    if idx is None or not 0 <= idx < len(CATEGORIES):
        return
    cat = CATEGORIES[idx]
    set_state(uid, "exp_amount", category=cat)
    edit(call, f"🏷 Kategoriya: {CAT_EMOJI[cat]} <b>{cat}</b>")
    send(chat, "💸 Xarajat summasini kiriting (masalan: <code>25000</code>):", cancel_markup())


def cb_history(call, uid, chat, a):
    f = get_filter(uid)
    op = a[0]
    if op == "p":
        page = to_int(a[1]) if len(a) > 1 else 0
        text, markup = render_history(uid, page or 0)
        edit(call, text, markup)
        return
    if op == "f":
        pass
    elif op == "t" and len(a) > 1 and a[1] in TYPE_NAMES:
        f["type"] = a[1]
        if a[1] == "income":
            f["category"] = None
    elif op == "pr" and len(a) > 1 and a[1] in PERIOD_NAMES:
        f["period"] = a[1]
    elif op == "s" and len(a) > 1 and a[1] in ("date", "amount"):
        f["sort"] = a[1]
    elif op == "r":
        FILTERS[uid] = default_filter()
    elif op == "c":
        rows = category_markup("h:cs")
        rows.append([("Barchasi", "h:cs:x"), ("⬅️ Orqaga", "h:f")])
        edit(call, "🏷 Kategoriyani tanlang (faqat xarajatlar uchun):", kb(*rows))
        return
    elif op == "cs" and len(a) > 1:
        if a[1] == "x":
            f["category"] = None
        else:
            idx = to_int(a[1])
            if idx is None or not 0 <= idx < len(CATEGORIES):
                return
            f["category"] = CATEGORIES[idx]
            f["type"] = "expense"
    elif op == "min":
        set_state(uid, "flt_min")
        send(chat, "🔢 Minimal summani kiriting (shundan katta yoki teng yozuvlar ko‘rsatiladi):", cancel_markup())
        return
    elif op == "x":
        export_json(chat, uid)
        return
    text, markup = render_filter(uid)
    edit(call, text, markup)


def fetch_tx(uid: int, tx_id):
    if tx_id is None:
        return None
    with SessionLocal() as s:
        return s.scalar(select(Transaction).where(Transaction.id == tx_id, Transaction.user_id == uid))


def cb_tx(call, uid, chat, a):
    op = a[0]
    t = fetch_tx(uid, to_int(a[1]) if len(a) > 1 else None)
    if t is None:
        edit(call, "❌ Yozuv topilmadi (o‘chirilgan bo‘lishi mumkin).", kb([("📋 Tarixga qaytish", "h:p:0")]))
        return
    if op == "e":
        rows = [[("💵 Summa", f"te:a:{t.id}"), ("📝 Izoh", f"te:n:{t.id}")]]
        if t.type == "expense":
            rows.append([("🏷 Kategoriya", f"te:c:{t.id}")])
        rows.append([("⬅️ Orqaga", "h:p:0")])
        edit(call, "✏️ <b>Nimani tahrirlaymiz?</b>\n\n" + tx_line(t), kb(*rows))
    elif op == "d":
        edit(
            call,
            "🗑 <b>Yozuvni o‘chirishni tasdiqlaysizmi?</b>\n\n" + tx_line(t),
            kb([("✅ Ha, o‘chirish", f"tx:dy:{t.id}"), ("❌ Yo‘q", "h:p:0")]),
        )
    elif op == "dy":
        with SessionLocal() as s:
            s.execute(delete(Transaction).where(Transaction.id == t.id, Transaction.user_id == uid))
            s.commit()
            inc, exp = sums(s, uid)
        edit(
            call,
            f"🗑 Yozuv <b>#{t.id}</b> o‘chirildi.\n\n💳 Joriy balans: <b>{money(inc - exp)}</b>",
            kb([("📋 Tarixga qaytish", "h:p:0")]),
        )


def cb_tx_edit(call, uid, chat, a):
    op = a[0]
    t = fetch_tx(uid, to_int(a[1]) if len(a) > 1 else None)
    if t is None:
        edit(call, "❌ Yozuv topilmadi.", kb([("📋 Tarixga qaytish", "h:p:0")]))
        return
    if op == "a":
        set_state(uid, "edit_amount", id=t.id)
        send(chat, f"💵 Yangi summani kiriting (hozir: {money(t.amount)}):", cancel_markup())
    elif op == "n":
        set_state(uid, "edit_note", id=t.id)
        note_prompt(chat, "🧹 Izohni o‘chirish")
    elif op == "c" and t.type == "expense":
        edit(call, "🏷 Yangi kategoriyani tanlang:", kb(*category_markup(f"tc:{t.id}")))


def cb_tx_category(call, uid, chat, a):
    tx_id = to_int(a[0]) if len(a) > 1 else None
    idx = to_int(a[1]) if len(a) > 1 else None
    if tx_id is None or idx is None or not 0 <= idx < len(CATEGORIES):
        return
    t = fetch_tx(uid, tx_id)
    if t is None or t.type != "expense":
        edit(call, "❌ Yozuv topilmadi.", kb([("📋 Tarixga qaytish", "h:p:0")]))
        return
    edit(call, "⏳ Saqlanmoqda...")
    show_after_edit(chat, uid, tx_id, {"category": CATEGORIES[idx]})


def cb_debt(call, uid, chat, a):
    op = a[0]
    if op in ("g", "t"):
        direction = "gave" if op == "g" else "took"
        set_state(uid, "debt_person", direction=direction)
        who = "Kimga qarz berdingiz?" if op == "g" else "Kimdan qarz oldingiz?"
        send(chat, f"👤 {who} Ismini kiriting:", cancel_markup())
    elif op == "l":
        text, markup = render_debts(uid)
        edit(call, text, markup)
    elif op == "p":
        debt_id = to_int(a[1]) if len(a) > 1 else None
        if debt_id is None:
            return
        with SessionLocal() as s:
            res = s.execute(
                update(Debt)
                .where(Debt.id == debt_id, Debt.user_id == uid, Debt.is_paid.is_(False))
                .values(is_paid=True, paid_at=utcnow())
            )
            s.commit()
            updated = res.rowcount
        if updated:
            try:
                bot.answer_callback_query(call.id, "✅ Qarz qaytarilgan deb belgilandi")
            except Exception:  # noqa: BLE001
                pass
        text, markup = render_debts(uid)
        edit(call, text, markup)


def cb_goal(call, uid, chat, a):
    op = a[0]
    if op == "new":
        set_state(uid, "goal_name")
        send(chat, "🎯 Maqsad nomini kiriting (masalan: <i>Yangi telefon</i>):", cancel_markup())
    elif op == "list":
        text, markup = render_goals(uid)
        edit(call, text, markup)
    elif op in ("add", "del", "dy"):
        gid = to_int(a[1]) if len(a) > 1 else None
        if gid is None:
            return
        with SessionLocal() as s:
            g = s.scalar(select(Goal).where(Goal.id == gid, Goal.user_id == uid))
        if g is None:
            edit(call, "❌ Maqsad topilmadi.", kb([("📋 Maqsadlarim", "g:list")]))
            return
        if op == "add":
            set_state(uid, "goal_add", id=g.id)
            send(chat, f"💵 «{esc(g.name)}» uchun qo‘shiladigan summani kiriting:", cancel_markup())
        elif op == "del":
            edit(
                call,
                f"🗑 <b>Maqsadni o‘chirishni tasdiqlaysizmi?</b>\n\n{goal_text(g)}",
                kb([("✅ Ha, o‘chirish", f"g:dy:{g.id}"), ("❌ Yo‘q", "g:list")]),
            )
        else:
            with SessionLocal() as s:
                s.execute(delete(Goal).where(Goal.id == g.id, Goal.user_id == uid))
                s.commit()
            text, markup = render_goals(uid)
            edit(call, text, markup)


def cb_stats(call, uid, chat, a):
    op = a[0]
    if op == "menu":
        edit(call, "📊 <b>Statistika</b>\nQaysi davr bo‘yicha ko‘rmoqchisiz?", stats_menu_markup())
    elif op == "p" and len(a) > 1 and valid_period(a[1]):
        text, markup = render_stats(uid, a[1])
        edit(call, text, markup)
    elif op == "g" and len(a) > 1 and valid_period(a[1]):
        key = a[1]
        b = period_bounds(key)
        with SessionLocal() as s:
            inc, exp = sums(s, uid, b)
            cats = category_totals(s, uid, b)
        if inc == 0 and exp == 0:
            send(chat, "📭 Bu davr uchun ma’lumot yo‘q, grafik yaratib bo‘lmadi.")
            return
        title = period_label(key).replace("‘", "'")
        buf = make_chart(inc, exp, cats, title)
        bot.send_photo(chat, buf, caption=f"📊 <b>{esc(period_label(key))}</b>")
    elif op == "y":
        with SessionLocal() as s:
            lo, hi = s.execute(
                select(func.min(Transaction.created_at), func.max(Transaction.created_at))
                .where(Transaction.user_id == uid)
            ).one()
        now_year = datetime.now(TZ).year
        y1 = lo.astimezone(TZ).year if lo else now_year
        y2 = max(hi.astimezone(TZ).year if hi else now_year, now_year)
        years = list(range(max(y1, y2 - 11), y2 + 1))
        rows, row = [], []
        for y in years:
            row.append((str(y), f"st:ys:{y}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([("⬅️ Orqaga", "st:menu")])
        edit(call, "📅 Yilni tanlang:", kb(*rows))
    elif op == "ys" and len(a) > 1 and valid_period(a[1]) and len(a[1]) == 4:
        y = a[1]
        rows, row = [], []
        for i, name in enumerate(MONTHS, start=1):
            row.append((name, f"st:p:{y}-{i:02d}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        rows.append([(f"📆 Butun {y}-yil", f"st:p:{y}")])
        rows.append([("⬅️ Yillar", "st:y")])
        edit(call, f"🗓 {y}-yil: oyni tanlang:", kb(*rows))


def cb_settings(call, uid, chat, a):
    op = a[0]
    if op == "export":
        export_json(chat, uid)
    elif op == "wipe":
        edit(
            call,
            "⚠️ <b>Barcha ma’lumotlaringiz (daromad, xarajat, qarz, jamg‘arma) butunlay o‘chiriladi.</b>\n"
            "Bu amalni qaytarib bo‘lmaydi. Davom etasizmi?",
            kb([("✅ Ha, hammasini o‘chirish", "set:wipey")], [("❌ Yo‘q", "set:no")]),
        )
    elif op == "wipey":
        with SessionLocal() as s:
            s.execute(delete(Transaction).where(Transaction.user_id == uid))
            s.execute(delete(Debt).where(Debt.user_id == uid))
            s.execute(delete(Goal).where(Goal.user_id == uid))
            s.commit()
        STATE.pop(uid, None)
        FILTERS.pop(uid, None)
        edit(call, "🗑 Barcha ma’lumotlaringiz o‘chirildi.")
    elif op == "no":
        edit(call, "👌 Bekor qilindi, ma’lumotlar saqlanib qoldi.")
    elif op == "about":
        edit(
            call,
            "ℹ️ <b>MoneyFlow</b>\n\n"
            "Shaxsiy moliya boti: daromad, xarajat, qarzlar, jamg‘arma, statistika va grafiklar.\n"
            "Summalar so‘mda, vaqt O‘zbekiston vaqti (UTC+5) bo‘yicha ko‘rsatiladi.\n"
            "Ma’lumotlaringiz faqat sizga tegishli va boshqa foydalanuvchilarga ko‘rinmaydi.",
        )


CALLBACKS = {
    "noop": cb_noop, "cancel": cb_cancel, "skip": cb_skip,
    "inc": cb_income, "exp": cb_expense, "ec": cb_expense_category,
    "h": cb_history, "tx": cb_tx, "te": cb_tx_edit, "tc": cb_tx_category,
    "d": cb_debt, "g": cb_goal, "st": cb_stats, "set": cb_settings,
}


@bot.callback_query_handler(func=lambda c: True)
@safe
def on_callback(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:  # noqa: BLE001
        pass  # eskirgan so‘rov — e’tiborsiz qoldiramiz
    if call.message is None or call.message.chat.type != "private":
        return
    parts = (call.data or "").split(":")
    handler = CALLBACKS.get(parts[0])
    if handler is None or len(parts) < 2 and parts[0] not in ("noop", "cancel", "skip"):
        return
    handler(call, call.from_user.id, call.message.chat.id, parts[1:])


# ----------------------------------------------------------------------------
# ISHGA TUSHIRISH
# ----------------------------------------------------------------------------
def main():
    try:
        with engine.connect() as conn:
            conn.execute(sql_text("SELECT 1"))
        Base.metadata.create_all(engine)
        log.info("PostgreSQL ulandi: %s", engine.url.render_as_string(hide_password=True))
    except SQLAlchemyError:
        log.exception("Database bilan bog‘lanib bo‘lmadi")
        sys.exit(
            "XATO: PostgreSQL bazasiga ulanib bo‘lmadi. DATABASE_URL ni tekshiring "
            "(lokal ishlatsangiz Railway’ning public manzili kerak)."
        )

    try:
        me = bot.get_me()
    except Exception:  # noqa: BLE001
        log.exception("Telegramga ulanib bo‘lmadi")
        sys.exit("XATO: BOT_TOKEN noto‘g‘ri yoki Telegramga ulanib bo‘lmadi.")

    bot.remove_webhook()
    log.info("MoneyFlow ishga tushdi: @%s (long polling)", me.username)
    bot.infinity_polling(
        timeout=20, long_polling_timeout=20, skip_pending=True, logger_level=logging.WARNING
    )


if __name__ == "__main__":
    main()
