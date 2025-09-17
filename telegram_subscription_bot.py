#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram admin-only bot for managing 1-month products/subscriptions.
Features:
- SQLite storage (data.db)
- Default duration: 30 days (configurable)
- Add flow: (1) description (free text) (2) purchase date
- List active, list expired, renew, finish, search, CSV export
- Inline keyboard (glass buttons) across the bot
- Daily summary at 09:00 local TZ (from TZ env; default Asia/Dubai) via JobQueue
- Admin management INSIDE the bot (add/remove/list) via inline buttons
- Scheduled ZIP backups of the whole bot folder, configurable from inside the bot (send via Telegram)

Environment (.env) example:
  BOT_TOKEN=123456:ABC...
  ADMIN_CHAT_ID=6391654120,123456789    # optional, comma-separated list for initial seeding
  TZ=Asia/Tehran                        # optional; default Asia/Dubai
  DB_PATH=/root/bot/data.db             # optional; default ./data.db
  BACKUP_SRC=/root/bot                  # optional; default folder of this script
  MAX_BACKUP_MB=45                      # optional; max doc size to send (MB), else path is sent

Requirements:
  pip3 install --upgrade "python-telegram-bot[job-queue]>=20,<21" python-dateutil
"""

from __future__ import annotations

import os
import sqlite3
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from telegram import (
    Update,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# -------------------- Emoji variables (Unicode) --------------------
EMOJI_RENEW   = "\U0001F504"   # 🔄
EMOJI_CLOSE   = "\u274C"       # ❌
EMOJI_ACTIVE  = "\u2705"       # ✅
EMOJI_EXPIRE  = "\u23F0"       # ⏰
EMOJI_NONE    = "\u2728"       # ✨
EMOJI_MENU    = "\U0001F4CB"   # 📋
EMOJI_ADD     = "\u2795"       # ➕
EMOJI_REMOVE  = "\u2796"       # ➖
EMOJI_ADMIN   = "\U0001F464"   # 👤
# -------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", "data.db")
DEFAULT_TZ = os.environ.get("TZ", "Asia/Dubai")
DEFAULT_DURATION_DAYS = 30

# Backup config
BACKUP_SRC = os.environ.get("BACKUP_SRC", str(Path(__file__).resolve().parent))
MAX_BACKUP_MB = int(os.environ.get("MAX_BACKUP_MB", "45"))

# Conversation states
ASK_DESC, ASK_DATE = range(2)

# Flag key for admin add/remove capture
AWAITING_ADMIN_ACTION_KEY = "awaiting_admin_action"  # "add" | "remove"


# ===================== Time helpers =====================
def now_tz() -> datetime:
    return datetime.now(ZoneInfo(DEFAULT_TZ))


# ===================== Data layer =====================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                buyer_id TEXT,
                purchase_date TEXT NOT NULL, -- ISO date (YYYY-MM-DD)
                duration_days INTEGER NOT NULL DEFAULT 30,
                expires_at TEXT NOT NULL, -- ISO datetime with tzinfo
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        # defaults
        cur = conn.execute("SELECT value FROM settings WHERE key='default_duration_days'")
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?)",
                ("default_duration_days", str(DEFAULT_DURATION_DAYS)),
            )
        if conn.execute("SELECT value FROM settings WHERE key='backup_enabled'").fetchone() is None:
            conn.execute("INSERT INTO settings(key, value) VALUES('backup_enabled','0')")
        if conn.execute("SELECT value FROM settings WHERE key='backup_interval_hours'").fetchone() is None:
            conn.execute("INSERT INTO settings(key, value) VALUES('backup_interval_hours','24')")
    seed_admins_from_env()


def seed_admins_from_env():
    """Seed admins table from ADMIN_CHAT_ID env (comma-separated)."""
    raw = os.environ.get("ADMIN_CHAT_ID", "").strip()
    if not raw:
        return
    ids = []
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            ids.append(int(x))
    if not ids:
        return
    now = now_tz().isoformat()
    with db() as conn:
        for cid in ids:
            conn.execute(
                "INSERT OR IGNORE INTO admins(chat_id, created_at) VALUES(?, ?)",
                (cid, now),
            )


def get_admin_ids() -> List[int]:
    with db() as conn:
        rows = conn.execute("SELECT chat_id FROM admins ORDER BY id ASC").fetchall()
    return [r["chat_id"] for r in rows]


def add_admin_id(chat_id: int) -> bool:
    try:
        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO admins(chat_id, created_at) VALUES(?, ?)",
                (chat_id, now_tz().isoformat()),
            )
        return True
    except Exception:
        return False


def remove_admin_id(chat_id: int) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM admins WHERE chat_id=?", (chat_id,))
        return cur.rowcount > 0


def get_setting(key: str, default: str) -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_default_days() -> int:
    try:
        return int(get_setting("default_duration_days", str(DEFAULT_DURATION_DAYS)))
    except Exception:
        return DEFAULT_DURATION_DAYS


def set_default_days(n: int):
    set_setting("default_duration_days", str(n))


def is_backup_enabled() -> bool:
    return get_setting("backup_enabled", "0") == "1"


def get_backup_interval_hours() -> int:
    try:
        return int(get_setting("backup_interval_hours", "24"))
    except Exception:
        return 24


# ===================== Models & helpers =====================
@dataclass
class Product:
    id: int
    description: str
    buyer_id: Optional[str]
    purchase_date: str  # YYYY-MM-DD
    duration_days: int
    expires_at: str  # ISO datetime
    is_active: int
    created_at: str
    updated_at: str


def parse_date(text: str) -> datetime:
    """Parse a date string like 2025-09-14 or natural text; returns timezone-aware midnight in DEFAULT_TZ."""
    dt = dateparser.parse(text, dayfirst=True, yearfirst=True)
    if not dt:
        raise ValueError("Cannot parse date. Use e.g. 2025-09-14 or 14/09/2025.")
    local = datetime(dt.year, dt.month, dt.day, tzinfo=ZoneInfo(DEFAULT_TZ))
    return local


def compute_expiry(purchase_dt: datetime, days: Optional[int] = None) -> datetime:
    d = days if days is not None else get_default_days()
    return purchase_dt + timedelta(days=d)


def human_summary(row: sqlite3.Row) -> str:
    status = f"{EMOJI_ACTIVE} فعال" if row["is_active"] else f"{EMOJI_CLOSE} غیرفعال"
    exp = dateparser.isoparse(row["expires_at"]).astimezone(ZoneInfo(DEFAULT_TZ)).strftime("%Y-%m-%d")
    return (
        f"#{row['id']} — {row['description']}\n"
        f"خریدار/آیدی: {row['buyer_id'] or '-'}\n"
        f"تاریخ خرید: {row['purchase_date']} | مدت: {row['duration_days']} روز\n"
        f"تاریخ انقضا: {exp} | وضعیت: {status}"
    )


# ===================== Keyboards =====================
def main_menu_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(f"{EMOJI_ADD} افزودن", callback_data="menu:add"),
         InlineKeyboardButton(f"{EMOJI_MENU} فهرست", callback_data="menu:list")],
        [InlineKeyboardButton(f"{EMOJI_EXPIRE} منقضی‌ها", callback_data="menu:expired"),
         InlineKeyboardButton("📥 خروجی CSV", callback_data="menu:export")],
        [InlineKeyboardButton("🧰 بکاپ‌گیری", callback_data="menu:backup")],
        [InlineKeyboardButton(f"{EMOJI_ADMIN} مدیریت ادمین‌ها", callback_data="menu:admins")]
    ]
    return InlineKeyboardMarkup(kb)


def admins_menu_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(f"{EMOJI_ADD} افزودن ادمین", callback_data="admins:add"),
         InlineKeyboardButton(f"{EMOJI_REMOVE} حذف ادمین", callback_data="admins:remove")],
        [InlineKeyboardButton("📜 لیست ادمین‌ها", callback_data="admins:list")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:home")]
    ]
    return InlineKeyboardMarkup(kb)


def backup_menu_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("📦 بکاپ الآن", callback_data="backup:now")],
        [InlineKeyboardButton("⏱ هر 6 ساعت",  callback_data="backup:on:6"),
         InlineKeyboardButton("⏱ هر 12 ساعت", callback_data="backup:on:12"),
         InlineKeyboardButton("⏱ هر 24 ساعت", callback_data="backup:on:24")],
        [InlineKeyboardButton("🛑 غیرفعال", callback_data="backup:off")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:home")]
    ]
    return InlineKeyboardMarkup(kb)


# ===================== Auth =====================
def is_admin(update: Update) -> bool:
    chat_id = update.effective_user.id if update.effective_user else None
    admins = get_admin_ids()
    if not admins:
        # Bootstrap: if no admins yet, allow private chat
        return update.effective_chat.type == "private"
    return chat_id in admins


async def guard_admin(update: Update) -> bool:
    if not is_admin(update):
        await update.effective_chat.send_message("دسترسی مجاز نیست.")
        return False
    return True


# ===================== Backup helpers =====================
def make_backup_zip() -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    src = Path(BACKUP_SRC).resolve()
    out_dir = Path("/tmp")
    out_base = out_dir / f"bot-backup-{ts}"
    archive_path = shutil.make_archive(str(out_base), "zip", root_dir=str(src))
    return Path(archive_path)


async def send_backup_to_admins(context: ContextTypes.DEFAULT_TYPE, caption: str = "📦 بکاپ"):
    path = make_backup_zip()
    size_mb = path.stat().st_size / (1024 * 1024)
    admin_ids = get_admin_ids()
    if not admin_ids:
        return

    if size_mb > MAX_BACKUP_MB:
        msg = (f"⚠️ حجم بکاپ {size_mb:.1f}MB از حد مجاز ({MAX_BACKUP_MB}MB) بزرگ‌تر است.\n"
               f"مسیر فایل روی سرور: {path}")
        for aid in admin_ids:
            try:
                await context.bot.send_message(chat_id=aid, text=msg)
            except Exception:
                pass
        return

    for aid in admin_ids:
        try:
            with path.open("rb") as f:
                await context.bot.send_document(chat_id=aid, document=f, filename=path.name, caption=caption)
        except Exception:
            pass


def reschedule_backup_job(app: Application):
    """(Re)configure the repeating backup job based on settings."""
    if not app.job_queue:
        print("JobQueue در دسترس نیست؛ زمان‌بندی بکاپ غیرفعال شد.")
        return

    # remove old jobs
    for job in app.job_queue.get_jobs_by_name("backup_job"):
        job.schedule_removal()

    if not is_backup_enabled():
        print("Auto-backup disabled.")
        return

    hours = get_backup_interval_hours()
    interval_seconds = max(1, hours) * 3600
    first_run = datetime.now(ZoneInfo(DEFAULT_TZ)) + timedelta(minutes=5)

    async def backup_job_callback(context: ContextTypes.DEFAULT_TYPE):
        await send_backup_to_admins(context, caption=f"📦 بکاپ خودکار (هر {hours} ساعت)")

    app.job_queue.run_repeating(
        backup_job_callback,
        interval=interval_seconds,
        first=first_run,
        name="backup_job",
    )
    print(f"Auto-backup enabled every {hours} hours.")


# ===================== Bot logic =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.effective_chat.send_message("👋 سلام! از منوی زیر انتخاب کن:", reply_markup=main_menu_kb())


# ---------- Add flow ----------
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return ConversationHandler.END
    await update.effective_chat.send_message(
        "توضیحات محصول را بفرست (نام محصول، آیدی خریدار، توضیحات…)\n\n"
        "مثال:\n«VPN Pro | buyer:@ali | سفارش ۱۲۳۴»",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:home")]])
    )
    return ASK_DESC


async def add_got_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_desc"] = update.message.text.strip()
    await update.message.reply_text(
        "تاریخ خرید را بفرست.\nفرمت پیشنهادی: 2025-09-14 یا 14/09/2025",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="menu:home")]])
    )
    return ASK_DATE


async def add_got_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        desc = context.user_data.get("new_desc")
        if not desc:
            await update.message.reply_text("ابتدا توضیحات را بفرست (/add).", reply_markup=main_menu_kb())
            return ConversationHandler.END
        pdate_local = parse_date(update.message.text.strip())
        expires = compute_expiry(pdate_local)
        created = now_tz().isoformat()
        with db() as conn:
            conn.execute(
                """
                INSERT INTO products(description, buyer_id, purchase_date, duration_days, expires_at, is_active, created_at, updated_at)
                VALUES(?, NULL, ?, ?, ?, 1, ?, ?)
                """,
                (
                    desc,
                    pdate_local.date().isoformat(),
                    get_default_days(),
                    expires.isoformat(),
                    created,
                    created,
                ),
            )
            new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        kb = [[InlineKeyboardButton(f"{EMOJI_RENEW} تمدید دوباره", callback_data=f"renew:{new_id}")],
              [InlineKeyboardButton("⬅️ بازگشت به منو", callback_data="menu:home")]]
        await update.message.reply_text(
            f"{EMOJI_ACTIVE} ثبت شد. آیتم #{new_id} تا {expires:%Y-%m-%d} معتبر است.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    except Exception as e:
        await update.message.reply_text(f"خطا در ثبت: {e}", reply_markup=main_menu_kb())
    finally:
        context.user_data.pop("new_desc", None)
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("new_desc", None)
    await update.message.reply_text("لغو شد.", reply_markup=main_menu_kb())
    return ConversationHandler.END


# ---------- List / expired ----------
async def list_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM products
            WHERE is_active=1
            ORDER BY datetime(expires_at) ASC
            LIMIT 50
            """
        ).fetchall()

    if not rows:
        await update.effective_chat.send_message("مورد فعالی وجود ندارد.", reply_markup=main_menu_kb())
        return

    for r in rows:
        text = human_summary(r)
        keyboard = [
            [
                InlineKeyboardButton(f"{EMOJI_RENEW} تمدید", callback_data=f"renew:{r['id']}"),
                InlineKeyboardButton(f"{EMOJI_CLOSE} بستن", callback_data=f"finish:{r['id']}"),
            ]
        ]
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def list_expired(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    now = now_tz().isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM products
            WHERE is_active=1 AND datetime(expires_at) <= datetime(?)
            ORDER BY datetime(expires_at) ASC
            LIMIT 200
            """,
            (now,),
        ).fetchall()
    if not rows:
        await update.effective_chat.send_message("هیچ مورد منقضی‌شده‌ای نداریم.", reply_markup=main_menu_kb())
        return
    for r in rows:
        text = human_summary(r)
        keyboard = [[InlineKeyboardButton(f"{EMOJI_RENEW} تمدید", callback_data=f"renew:{r['id']}"),
                     InlineKeyboardButton(f"{EMOJI_CLOSE} بستن", callback_data=f"finish:{r['id']}")]]
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard))


# ---------- Renew / finish (commands) ----------
async def renew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    if not context.args:
        await update.message.reply_text("استفاده: /renew <id> [روز]", reply_markup=main_menu_kb())
        return
    try:
        pid = int(context.args[0])
        extra_days = int(context.args[1]) if len(context.args) >= 2 else get_default_days()
    except Exception:
        await update.message.reply_text("شناسه یا روزها نامعتبر است.", reply_markup=main_menu_kb())
        return

    with db() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            await update.message.reply_text("یافت نشد.", reply_markup=main_menu_kb())
            return
        base_expiry = dateparser.isoparse(row["expires_at"]).astimezone(ZoneInfo(DEFAULT_TZ))
        new_expiry = base_expiry + timedelta(days=extra_days)
        conn.execute(
            "UPDATE products SET expires_at=?, duration_days=duration_days+?, updated_at=? WHERE id=?",
            (new_expiry.isoformat(), extra_days, now_tz().isoformat(), pid),
        )
    await update.message.reply_text(f"{EMOJI_RENEW} تمدید شد. انقضای جدید: {new_expiry.strftime('%Y-%m-%d')}")


async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    if not context.args:
        await update.message.reply_text("استفاده: /finish <id>", reply_markup=main_menu_kb())
        return
    try:
        pid = int(context.args[0])
    except Exception:
        await update.message.reply_text("شناسه نامعتبر است.", reply_markup=main_menu_kb())
        return

    with db() as conn:
        conn.execute(
            "UPDATE products SET is_active=0, updated_at=? WHERE id=?",
            (now_tz().isoformat(), pid),
        )
    await update.message.reply_text(f"{EMOJI_CLOSE} بسته شد.")


# ---------- Search ----------
async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    q = " ".join(context.args) if context.args else ""
    if not q:
        await update.message.reply_text("استفاده: /find <متن>", reply_markup=main_menu_kb())
        return
    pattern = f"%{q}%"
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM products
            WHERE description LIKE ? OR (buyer_id IS NOT NULL AND buyer_id LIKE ?)
            ORDER BY datetime(expires_at) ASC
            LIMIT 100
            """,
            (pattern, pattern),
        ).fetchall()
    if not rows:
        await update.message.reply_text("چیزی پیدا نشد.", reply_markup=main_menu_kb())
        return
    await update.message.reply_text("\n\n".join(human_summary(r) for r in rows))


# ---------- Settings ----------
async def set_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    if not context.args:
        kb = [[InlineKeyboardButton("30 روز", callback_data="setdays:30"),
               InlineKeyboardButton("60 روز", callback_data="setdays:60"),
               InlineKeyboardButton("90 روز", callback_data="setdays:90")]]
        await update.message.reply_text("مدت پیش‌فرض را انتخاب یا به‌صورت عددی بفرست:", reply_markup=InlineKeyboardMarkup(kb))
        return
    try:
        n = int(context.args[0])
        if n <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("عدد نامعتبر است.", reply_markup=main_menu_kb())
        return
    set_default_days(n)
    await update.message.reply_text(f"مدت پیش‌فرض روی {n} روز تنظیم شد.")


# ---------- Export CSV ----------
async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    import csv
    path = "export_products.csv"
    with db() as conn, open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "id","description","buyer_id","purchase_date","duration_days",
            "expires_at","is_active","created_at","updated_at",
        ])
        for r in conn.execute("SELECT * FROM products ORDER BY id ASC"):
            writer.writerow([
                r["id"], r["description"], r["buyer_id"], r["purchase_date"],
                r["duration_days"], r["expires_at"], r["is_active"],
                r["created_at"], r["updated_at"],
            ])
    await update.message.reply_document(InputFile(path))


# ---------- Admins: inline flows ----------
async def handle_admins_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, sub: str):
    if sub == "list":
        ids = get_admin_ids()
        if not ids:
            await update.effective_chat.send_message("ادمینی ثبت نشده.", reply_markup=admins_menu_kb())
            return
        text = "👥 لیست ادمین‌ها:\n" + "\n".join(f"- `{cid}`" for cid in ids)
        await update.effective_chat.send_message(text, reply_markup=admins_menu_kb(), parse_mode="Markdown")
    elif sub == "add":
        context.user_data[AWAITING_ADMIN_ACTION_KEY] = "add"
        await update.effective_chat.send_message(
            "آی‌دی عددی کاربر را بفرست تا به ادمین‌ها اضافه شود.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:admins")]])
        )
    elif sub == "remove":
        context.user_data[AWAITING_ADMIN_ACTION_KEY] = "remove"
        await update.effective_chat.send_message(
            "آی‌دی عددی ادمینی که می‌خواهی حذف شود را بفرست.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:admins")]])
        )
    else:
        await update.effective_chat.send_message("گزینه نامعتبر.", reply_markup=admins_menu_kb())


async def maybe_capture_admin_id_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """If waiting for admin add/remove, handle numeric text here."""
    action = context.user_data.get(AWAITING_ADMIN_ACTION_KEY)
    if not action:
        return False
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("لطفاً فقط آی‌دی عددی بفرست.", reply_markup=admins_menu_kb())
        return True
    chat_id = int(text)
    if action == "add":
        ok = add_admin_id(chat_id)
        if ok:
            await update.message.reply_text(f"✅ ادمین با آی‌دی {chat_id} اضافه شد.", reply_markup=admins_menu_kb())
        else:
            await update.message.reply_text("خطا در افزودن ادمین.", reply_markup=admins_menu_kb())
    elif action == "remove":
        ok = remove_admin_id(chat_id)
        if ok:
            await update.message.reply_text(f"✅ ادمین با آی‌دی {chat_id} حذف شد.", reply_markup=admins_menu_kb())
        else:
            await update.message.reply_text("چنین ادمینی یافت نشد.", reply_markup=admins_menu_kb())
    context.user_data.pop(AWAITING_ADMIN_ACTION_KEY, None)
    return True


# ---------- Inline buttons handler ----------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data

    # Menus
    if data == "menu:home":
        await query.edit_message_text("👋 سلام! از منوی زیر انتخاب کن:", reply_markup=main_menu_kb())
        return
    if data == "menu:list":
        await list_active(update, context)
        return
    if data == "menu:expired":
        await list_expired(update, context)
        return
    if data == "menu:export":
        await export_csv(update, context)
        return
    if data == "menu:admins":
        await query.edit_message_text("مدیریت ادمین‌ها:", reply_markup=admins_menu_kb())
        return
    if data == "menu:backup":
        await query.edit_message_text("تنظیمات بکاپ‌گیری:", reply_markup=backup_menu_kb())
        return

    # Admins submenu
    if data.startswith("admins:"):
        _, sub = data.split(":", 1)
        await handle_admins_menu(update, context, sub)
        return

    # Settings quick set
    if data.startswith("setdays:"):
        _, n = data.split(":", 1)
        try:
            days = int(n)
            set_default_days(days)
            await query.edit_message_text(f"مدت پیش‌فرض روی {days} روز تنظیم شد.", reply_markup=main_menu_kb())
        except Exception:
            await query.edit_message_text("خطای تنظیم مدت.", reply_markup=main_menu_kb())
        return

    # Backup actions
    if data.startswith("backup:"):
        parts = data.split(":")
        if parts[1] == "now":
            await query.edit_message_text("⏳ در حال ساخت بکاپ…")
            await send_backup_to_admins(context, caption="📦 بکاپ دستی (الان)")
            await query.edit_message_text("✅ بکاپ ساخته و ارسال شد.", reply_markup=backup_menu_kb())
            return
        if parts[1] == "on" and len(parts) == 3:
            try:
                hours = int(parts[2])
                set_setting("backup_enabled", "1")
                set_setting("backup_interval_hours", str(hours))
                reschedule_backup_job(context.application)
                await query.edit_message_text(f"✅ بکاپ خودکار هر {hours} ساعت فعال شد.", reply_markup=backup_menu_kb())
            except Exception:
                await query.edit_message_text("❗️ مقدار ساعت نامعتبر است.", reply_markup=backup_menu_kb())
            return
        if parts[1] == "off":
            set_setting("backup_enabled", "0")
            reschedule_backup_job(context.application)
            await query.edit_message_text("🛑 بکاپ خودکار غیرفعال شد.", reply_markup=backup_menu_kb())
            return

    # Product actions
    if ":" in data:
        action, pid_s = data.split(":", 1)
        if action in {"renew", "finish"} and pid_s.isdigit():
            pid = int(pid_s)
            if action == "renew":
                with db() as conn:
                    row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
                    if not row:
                        await query.edit_message_text("یافت نشد.", reply_markup=main_menu_kb())
                        return
                    base_expiry = dateparser.isoparse(row["expires_at"]).astimezone(ZoneInfo(DEFAULT_TZ))
                    days_to_add = get_default_days()
                    new_expiry = base_expiry + timedelta(days=days_to_add)
                    conn.execute(
                        "UPDATE products SET expires_at=?, duration_days=duration_days+?, updated_at=? WHERE id=?",
                        (new_expiry.isoformat(), days_to_add, now_tz().isoformat(), pid),
                    )
                await query.edit_message_text(
                    f"{EMOJI_RENEW} آیتم #{pid} تمدید شد.\nانقضای جدید: {new_expiry.strftime('%Y-%m-%d')}",
                    reply_markup=main_menu_kb()
                )
                return
            elif action == "finish":
                with db() as conn:
                    conn.execute(
                        "UPDATE products SET is_active=0, updated_at=? WHERE id=?",
                        (now_tz().isoformat(), pid),
                    )
                await query.edit_message_text(f"{EMOJI_CLOSE} آیتم #{pid} بسته شد.", reply_markup=main_menu_kb())
                return

    await query.edit_message_text("عملیات پشتیبانی نمی‌شود.", reply_markup=main_menu_kb())


# ---------- Daily summary job ----------
async def daily_summary(context: ContextTypes.DEFAULT_TYPE):
    admin_ids = get_admin_ids()
    if not admin_ids:
        return
    now = now_tz()
    soon = now + timedelta(days=2)
    with db() as conn:
        exp = conn.execute(
            "SELECT * FROM products WHERE is_active=1 AND datetime(expires_at) <= datetime(?) ORDER BY datetime(expires_at) ASC",
            (now.isoformat(),),
        ).fetchall()
        upcoming = conn.execute(
            "SELECT * FROM products WHERE is_active=1 AND datetime(expires_at) > datetime(?) AND datetime(expires_at) <= datetime(?) ORDER BY datetime(expires_at) ASC",
            (now.isoformat(), soon.isoformat()),
        ).fetchall()

    parts = []
    if exp:
        parts.append("📍 منقضی‌شده‌ها:\n" + "\n\n".join(human_summary(r) for r in exp))
    if upcoming:
        parts.append("⏳ تا ۴۸ ساعت آینده:\n" + "\n\n".join(human_summary(r) for r in upcoming))
    if not parts:
        parts.append(f"امروز موردی برای پیگیری نیست {EMOJI_NONE}")
    text = "\n\n".join(parts)

    for aid in admin_ids:
        try:
            await context.bot.send_message(chat_id=aid, text=text)
        except Exception:
            pass


# ===================== App wiring =====================
def build_app(token: str) -> Application:
    init_db()
    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_active))
    app.add_handler(CommandHandler("expired", list_expired))
    app.add_handler(CommandHandler("renew", renew))
    app.add_handler(CommandHandler("finish", finish))
    app.add_handler(CommandHandler("find", find))
    app.add_handler(CommandHandler("setdefaultdays", set_default))
    app.add_handler(CommandHandler("export", export_csv))

    # Conversation: /add and inline menu:add
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            CallbackQueryHandler(add_start, pattern="^menu:add$"),
        ],
        states={
            ASK_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_desc)],
            ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_date)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
        name="add_conv",
        persistent=False,
    )
    app.add_handler(conv)

    # Capture admin add/remove numeric input (outside conversations)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: maybe_capture_admin_id_text(u, c)))

    # Inline button handler
    app.add_handler(CallbackQueryHandler(on_button))

    # Daily summary at 09:00
    if app.job_queue:
        tz = ZoneInfo(DEFAULT_TZ)
        app.job_queue.run_daily(
            daily_summary,
            time=datetime.now(tz).replace(hour=9, minute=0, second=0, microsecond=0).timetz()
        )
    else:
        print("JobQueue در دسترس نیست؛ یادآوری روزانه غیرفعال شد.")

    # Auto-backup schedule according to settings
    reschedule_backup_job(app)

    return app


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Please set BOT_TOKEN env var.")
    app = build_app(token)
    print("Bot is running… Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
