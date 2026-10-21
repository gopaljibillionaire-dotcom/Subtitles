"""Kiyotaka Sales Tracker V4

Tracks new document posts in configured Sold and Unsold Telegram channels,
shows reports/statistics, and posts a temporary System Test report to the
configured broadcast channel. Only the System Test report is auto-deleted
(300 seconds after posting); daily reports are not scheduled for deletion.

Install:
    pip install -r requirements.txt

Before running, set BOT_TOKEN and OWNER_ID below (or use environment variables).
"""

import asyncio
import logging
import os
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# CONFIGURATION — set these two values before starting the bot.
# You can also use environment variables BOT_TOKEN and OWNER_ID.
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8988298879:AAEpjEIW4zMIKFikhGBrVk-AJoTzHvT4lBQ").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "7123919486"))  # Replace with your Telegram user ID.

BOT_NAME = "Kiyotaka Sales Tracker V4"
SUPPORT_USERNAME = "@AlwaysKiyotaka"
DB_FILE = "kiyotaka_sales_v4.db"
TEST_REPORT_DELETE_SECONDS = 300  # 5 minutes; applies ONLY to System Test reports.
DAILY_JOB_NAME = "kiyotaka_daily_sales_report"
CHANNEL_ID_RE = re.compile(r"-100\d+\Z")

try:
    TIMEZONE = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:  # Helps Android/Pydroid builds without system tz data.
    TIMEZONE = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("kiyotaka_sales_tracker")

HEADER = (
    "╔══════════════════════════════╗\n"
    "║       ✦  ᴋɪʏᴏᴛᴀᴋᴀ  ✦       ║\n"
    "║      ꜱᴀʟᴇꜱ ᴛʀᴀᴄᴋᴇʀ ᴠ4      ║\n"
    "╚══════════════════════════════╝"
)
TAGLINE = "ᴛʀᴀᴄᴋ ꜰɪʟᴇꜱ. ᴍᴏɴɪᴛᴏʀ ꜱᴀʟᴇꜱ.\nᴀᴜᴛᴏᴍᴀᴛᴇ ʏᴏᴜʀ ʀᴇᴘᴏʀᴛꜱ."
BOT_DESCRIPTION = (
    "✦ ᴋɪʏᴏᴛᴀᴋᴀ ꜱᴀʟᴇꜱ ᴛʀᴀᴄᴋᴇʀ ᴠ4 ✦\n"
    "📊 Track Sold & Unsold file posts.\n"
    "🗓 Get daily reports and file statistics.\n"
    "⚙️ Per-user channels, settings, and statistics.\n"
    "📞 Support: @AlwaysKiyotaka"
)

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_FILE, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 20000")
    return connection


def init_database() -> None:
    with db_connect() as conn:
        # Legacy/global table is retained only for backwards compatibility and migration.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS file_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "chat_id INTEGER NOT NULL,"
            "message_id INTEGER NOT NULL,"
            "category TEXT NOT NULL CHECK(category IN ('sold', 'unsold')),"
            "file_name TEXT NOT NULL DEFAULT '',"
            "posted_at TEXT NOT NULL,"
            "UNIQUE(chat_id, message_id))"
        )
        # Upgrade databases created by older bot versions. SQLite does not add
        # columns to an existing table when CREATE TABLE IF NOT EXISTS is used.
        legacy_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(file_events)").fetchall()
        }
        if "file_name" not in legacy_columns:
            conn.execute(
                "ALTER TABLE file_events ADD COLUMN file_name TEXT NOT NULL DEFAULT ''"
            )
            legacy_columns.add("file_name")
        # Older database versions may also lack posted_at. Add it with a safe
        # default so existing rows remain readable and startup cannot fail.
        if "posted_at" not in legacy_columns:
            conn.execute(
                "ALTER TABLE file_events ADD COLUMN posted_at TEXT NOT NULL DEFAULT ''"
            )
            legacy_columns.add("posted_at")
        # Backfill timestamps from older column names when available.
        timestamp_source = next(
            (name for name in ("created_at", "timestamp", "date", "event_time")
             if name in legacy_columns),
            None,
        )
        if timestamp_source:
            conn.execute(
                f"UPDATE file_events SET posted_at = CAST({timestamp_source} AS TEXT) "
                "WHERE posted_at = '' AND " + timestamp_source + " IS NOT NULL"
            )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_settings ("
            "user_id INTEGER NOT NULL,"
            "key TEXT NOT NULL,"
            "value TEXT NOT NULL,"
            "PRIMARY KEY(user_id, key))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_file_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "user_id INTEGER NOT NULL,"
            "chat_id INTEGER NOT NULL,"
            "message_id INTEGER NOT NULL,"
            "category TEXT NOT NULL CHECK(category IN ('sold', 'unsold')),"
            "file_name TEXT NOT NULL DEFAULT '',"
            "posted_at TEXT NOT NULL,"
            "UNIQUE(user_id, chat_id, message_id))"
        )
        # Preserve old defaults in the legacy table, but never expose them to other users.
        defaults = {
            "sold_channel": "",
            "unsold_channel": "",
            "broadcast_channel": "",
            "report_time": "21:00",
            "report_sticker": "",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )
            # Import prior one-global-config installations into the owner's private config only.
            conn.execute(
                "INSERT OR IGNORE INTO user_settings(user_id, key, value) "
                "SELECT ?, key, value FROM settings WHERE key = ?",
                (OWNER_ID, key),
            )
        # Migrate previously captured events to the owner's private history where possible.
        sold = conn.execute(
            "SELECT value FROM user_settings WHERE user_id = ? AND key = 'sold_channel'",
            (OWNER_ID,),
        ).fetchone()
        unsold = conn.execute(
            "SELECT value FROM user_settings WHERE user_id = ? AND key = 'unsold_channel'",
            (OWNER_ID,),
        ).fetchone()
        sold_id = str(sold[0]) if sold else ""
        unsold_id = str(unsold[0]) if unsold else ""
        if sold_id or unsold_id:
            conn.execute(
                "INSERT OR IGNORE INTO user_file_events "
                "(user_id, chat_id, message_id, category, file_name, posted_at) "
                "SELECT ?, chat_id, message_id, "
                "CASE WHEN CAST(chat_id AS TEXT) = ? THEN 'sold' ELSE 'unsold' END, "
                "file_name, posted_at FROM file_events "
                "WHERE CAST(chat_id AS TEXT) = ? OR CAST(chat_id AS TEXT) = ?",
                (OWNER_ID, sold_id, sold_id, unsold_id),
            )


def get_setting(key: str, default: str = "", user_id: int | None = None) -> str:
    table = "user_settings" if user_id is not None else "settings"
    with db_connect() as conn:
        if user_id is None:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
                (user_id, key),
            ).fetchone()
    return str(row["value"]) if row else default


def set_setting(key: str, value: str, user_id: int | None = None) -> None:
    with db_connect() as conn:
        if user_id is None:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        else:
            conn.execute(
                "INSERT INTO user_settings(user_id, key, value) VALUES(?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (user_id, key, value),
            )


def get_channel_id(key: str, user_id: int | None = None) -> int | None:
    raw = get_setting(key, user_id=user_id).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def get_day_bounds(day: date) -> tuple[str, str]:
    start_dt = datetime.combine(day, time.min, tzinfo=TIMEZONE)
    end_dt = start_dt + timedelta(days=1)
    return start_dt.isoformat(), end_dt.isoformat()


def get_counts_for_day(day: date | None = None, user_id: int | None = None) -> dict[str, int]:
    day = day or datetime.now(TIMEZONE).date()
    counts = {"sold": 0, "unsold": 0}
    if user_id is None:
        return counts
    start_text, end_text = get_day_bounds(day)
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) AS amount FROM user_file_events "
            "WHERE user_id = ? AND posted_at >= ? AND posted_at < ? GROUP BY category",
            (user_id, start_text, end_text),
        ).fetchall()
    for row in rows:
        counts[str(row["category"])] = int(row["amount"])
    return counts


def get_total_counts(user_id: int | None = None) -> dict[str, int]:
    counts = {"sold": 0, "unsold": 0}
    if user_id is None:
        return counts
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) AS amount FROM user_file_events "
            "WHERE user_id = ? GROUP BY category", (user_id,)
        ).fetchall()
    for row in rows:
        counts[str(row["category"])] = int(row["amount"])
    return counts


def get_recent_history(user_id: int, days: int = 7) -> list[tuple[date, int, int]]:
    today = datetime.now(TIMEZONE).date()
    history: list[tuple[date, int, int]] = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        counts = get_counts_for_day(day, user_id)
        history.append((day, counts["sold"], counts["unsold"]))
    return history


def build_daily_report(day: date | None = None, user_id: int | None = None) -> str:
    day = day or datetime.now(TIMEZONE).date()
    counts = get_counts_for_day(day, user_id)
    total = counts["sold"] + counts["unsold"]
    return (
        "╔══════════════════════════════╗\n"
        "║       📊 ᴅᴀɪʟʏ ʀᴇᴘᴏʀᴛ       ║\n"
        "╚══════════════════════════════╝\n\n"
        f"📅 ᴅᴀᴛᴇ: {day.strftime('%d %b %Y')}\n"
        f"✅ ꜱᴏʟᴅ ꜰɪʟᴇꜱ: {counts['sold']}\n"
        f"❌ ᴜɴꜱᴏʟᴅ ꜰɪʟᴇꜱ: {counts['unsold']}\n"
        f"📦 ᴛᴏᴛᴀʟ ɴᴇᴡ ꜰɪʟᴇꜱ: {total}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "ᴋɪʏᴏᴛᴀᴋᴀ ꜱᴀʟᴇꜱ ᴛʀᴀᴄᴋᴇʀ ᴠ4"
    )


# ---------------------------------------------------------------------------
# USER INTERFACE
# ---------------------------------------------------------------------------
def is_owner(user_id: int | None) -> bool:
    return user_id is not None and user_id == OWNER_ID


async def owner_only(update: Update) -> bool:
    if is_owner(update.effective_user.id if update.effective_user else None):
        return True
    query = update.callback_query
    if query:
        try:
            await query.answer("ᴏᴡɴᴇʀ ᴏɴʟʏ.", show_alert=True)
        except TelegramError:
            pass
    elif update.effective_message:
        await update.effective_message.reply_text("⛔ ᴛʜɪꜱ ᴘᴀɴᴇʟ ɪꜱ ᴏᴡɴᴇʀ-ᴏɴʟʏ.")
    return False


def main_keyboard(owner: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("📢 Add Channels"), KeyboardButton("📊 Daily Sales Report")],
        [KeyboardButton("📁 Account File Stats"), KeyboardButton("🧪 System Test")],
        [KeyboardButton("📜 Report History"), KeyboardButton("⚙️ Settings")],
        [KeyboardButton("📞 Contact Us")],
    ]
    return ReplyKeyboardMarkup(
        keyboard, resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Choose an option",
    )


def settings_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("🕒 Change Report Time"), KeyboardButton("🎟 Set Report Sticker")],
        [KeyboardButton("🗑 Remove Report Sticker"), KeyboardButton("📊 Report Status")],
        [KeyboardButton("📜 Report History"), KeyboardButton("📢 Add Channels")],
        [KeyboardButton("🏠 Main Menu")],
    ]
    return ReplyKeyboardMarkup(
        keyboard, resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Choose a setting",
    )


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> None:
    text = f"{HEADER}\n\n{TAGLINE}\n\nᴄʜᴏᴏꜱᴇ ᴀɴ ᴏᴘᴛɪᴏɴ."
    if edit and update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, reply_markup=main_keyboard())
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=main_keyboard())


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    report_time = get_setting("report_time", "21:00", user_id=user_id)
    sticker_state = "ᴇɴᴀʙʟᴇᴅ" if get_setting("report_sticker", user_id=user_id) else "ᴏꜰꜰ"
    text = (
        "╔══════════════════════╗\n"
        "║      ⚙️ ꜱᴇᴛᴛɪɴɢꜱ       ║\n"
        "╚══════════════════════╝\n\n"
        f"🕒 ʀᴇᴘᴏʀᴛ ᴛɪᴍᴇ: {report_time} (IST)\n"
        f"🎟 ʀᴇᴘᴏʀᴛ ꜱᴛɪᴄᴋᴇʀ: {sticker_state}\n\n"
        "ᴄʜᴏᴏꜱᴇ ᴀɴ ᴀᴄᴛɪᴏɴ ᴜꜱɪɴɢ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ.\n"
        "ᴛʜᴇꜱᴇ ꜱᴇᴛᴛɪɴɢꜱ ʙᴇʟᴏɴɢ ᴏɴʟʏ ᴛᴏ ʏᴏᴜ."
    )
    if update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=settings_keyboard())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_main_menu(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A-to-Z feature guide, kept in the same small-caps style as the bot UI."""
    text = (
        "╔══════════════════════════════╗\n"
        "║      ✦  ᴋɪʏᴏᴛᴀᴋᴀ  ✦       ║\n"
        "║        ʜᴇʟᴘ ᴄᴇɴᴛᴇʀ         ║\n"
        "╚══════════════════════════════╝\n\n"
        "📊 ʙᴏᴛ ꜰᴇᴀᴛᴜʀᴇꜱ & ᴄᴏᴍᴍᴀɴᴅꜱ\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📢 1. ᴀᴅᴅ ᴄʜᴀɴɴᴇʟꜱ\n"
        "➜ ᴄᴏɴꜰɪɢᴜʀᴇ ᴛʜᴇ ꜱᴏʟᴅ, ᴜɴꜱᴏʟᴅ\n"
        "   ᴀɴᴅ ʀᴇᴘᴏʀᴛ/ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴄʜᴀɴɴᴇʟꜱ.\n\n"
        "📊 2. ᴅᴀɪʟʏ ꜱᴀʟᴇꜱ ʀᴇᴘᴏʀᴛ\n"
        "➜ ᴠɪᴇᴡ ᴛᴏᴅᴀʏ'ꜱ ꜱᴏʟᴅ, ᴜɴꜱᴏʟᴅ\n"
        "   ᴀɴᴅ ᴛᴏᴛᴀʟ ᴀᴄᴄᴏᴜɴᴛ ꜰɪʟᴇ ᴄᴏᴜɴᴛꜱ.\n\n"
        "📁 3. ᴀᴄᴄᴏᴜɴᴛ ꜰɪʟᴇ ꜱᴛᴀᴛꜱ\n"
        "➜ ᴄʜᴇᴄᴋ ᴛʜᴇ ᴄᴏᴜɴᴛ ᴏꜰ ɴᴇᴡ\n"
        "   ꜱᴏʟᴅ ᴀɴᴅ ᴜɴꜱᴏʟᴅ ᴅᴏᴄᴜᴍᴇɴᴛꜱ.\n\n"
        "🧪 4. ꜱʏꜱᴛᴇᴍ ᴛᴇꜱᴛ\n"
        "➜ ꜱᴇɴᴅ ᴀ ᴛᴇꜱᴛ ʀᴇᴘᴏʀᴛ ᴛᴏ ᴛʜᴇ\n"
        "   ᴄᴏɴꜰɪɢᴜʀᴇᴅ ʀᴇᴘᴏʀᴛ ᴄʜᴀɴɴᴇʟ.\n"
        "➜ ᴛʜᴇ ᴛᴇꜱᴛ ᴍᴇꜱꜱᴀɢᴇ ɪꜱ ᴅᴇʟᴇᴛᴇᴅ\n"
        "   ᴀꜰᴛᴇʀ 5 ᴍɪɴᴜᴛᴇꜱ.\n\n"
        "📜 5. ʀᴇᴘᴏʀᴛ ʜɪꜱᴛᴏʀʏ\n"
        "➜ ᴠɪᴇᴡ ᴘʀᴇᴠɪᴏᴜꜱʟʏ ꜱᴀᴠᴇᴅ ʀᴇᴘᴏʀᴛꜱ.\n\n"
        "⚙️ 6. ꜱᴇᴛᴛɪɴɢꜱ\n"
        "➜ ᴄᴏɴꜰɪɢᴜʀᴇ ᴛʜᴇ ʀᴇᴘᴏʀᴛ ᴛɪᴍᴇ\n"
        "   ᴀɴᴅ ʀᴇᴘᴏʀᴛ ꜱᴛɪᴄᴋᴇʀ ᴏᴘᴛɪᴏɴ.\n\n"
        "📞 7. ᴄᴏɴᴛᴀᴄᴛ ᴜꜱ\n"
        f"➜ ꜱᴜᴘᴘᴏʀᴛ: {SUPPORT_USERNAME}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⌨️ ᴄᴏᴍᴍᴀɴᴅ ʟɪꜱᴛ\n"
        "• /start — ᴏᴘᴇɴ ᴛʜᴇ ᴍᴀɪɴ ᴍᴇɴᴜ\n"
        "• /help — ᴏᴘᴇɴ ᴛʜɪꜱ ɢᴜɪᴅᴇ\n"
        "• /report — ᴅᴀɪʟʏ ꜱᴀʟᴇꜱ ʀᴇᴘᴏʀᴛ\n"
        "• /stats — ᴀᴄᴄᴏᴜɴᴛ ꜰɪʟᴇ ꜱᴛᴀᴛꜱ\n"
        "• /setup — ꜱᴇᴛ ᴜᴘ ᴄʜᴀɴɴᴇʟꜱ (ᴀʟʟ ᴜꜱᴇʀꜱ)\n"
        "• /settings — ʀᴇᴘᴏʀᴛ ꜱᴇᴛᴛɪɴɢꜱ (ᴀʟʟ ᴜꜱᴇʀꜱ)\n"
        "• /test — ꜱᴇɴᴅ ᴀ ᴛᴇꜱᴛ ʀᴇᴘᴏʀᴛ (ᴀʟʟ ᴜꜱᴇʀꜱ)\n"
        "• /cancel — ᴄᴀɴᴄᴇʟ ᴀɴ ᴀᴄᴛɪᴠᴇ ꜱᴇᴛᴜᴘ\n\n"
        "📌 ɴᴏᴛᴇꜱ\n"
        "➜ ᴀᴅᴅ ᴛʜᴇ ʙᴏᴛ ᴀꜱ ᴀɴ ᴀᴅᴍɪɴ ɪɴ ᴛʜᴇ\n"
        "   ᴄʜᴀɴɴᴇʟꜱ ɪᴛ ɴᴇᴇᴅꜱ ᴛᴏ ʀᴇᴀᴅ/ᴘᴏꜱᴛ ɪɴ.\n"
        "➜ ᴏɴʟʏ ɴᴇᴡ ᴅᴏᴄᴜᴍᴇɴᴛ ᴘᴏꜱᴛꜱ ᴀꜰᴛᴇʀ\n"
        "   ᴛʜᴇ ʙᴏᴛ ꜱᴛᴀʀᴛꜱ ᴄᴀɴ ʙᴇ ᴄᴏᴜɴᴛᴇᴅ.\n"
        "➜ ᴅᴀɪʟʏ ʀᴇᴘᴏʀᴛꜱ ᴀʀᴇ ɴᴏᴛ ᴀᴜᴛᴏ-ᴅᴇʟᴇᴛᴇᴅ.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👨‍💻 ᴅᴇᴠᴇʟᴏᴘᴇʀ: {SUPPORT_USERNAME}"
    )
    await update.effective_message.reply_text(
        text,
        reply_markup=main_keyboard(is_owner(update.effective_user.id if update.effective_user else None)),
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting", None)
    await update.effective_message.reply_text("✅ ᴄᴜʀʀᴇɴᴛ ꜱᴇᴛᴜᴘ ᴄᴀɴᴄᴇʟʟᴇᴅ.")


# ---------------------------------------------------------------------------
# CHANNEL SETUP AND SETTINGS INPUT
# ---------------------------------------------------------------------------
async def configure_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Channel setup is a normal feature: every user, including the owner, can use it.
    context.user_data["awaiting"] = "sold_channel"
    message = update.effective_message
    if message:
        await message.reply_text(
            "📢 ᴄʜᴀɴɴᴇʟ ꜱᴇᴛᴜᴘ (1/3)\n\n"
            "ꜱᴇɴᴅ ᴛʜᴇ ꜱᴏʟᴅ ᴄʜᴀɴɴᴇʟ ɪᴅ.\n"
            "ᴇxᴀᴍᴘʟᴇ: `-1001234567890`\n\n"
            "ᴛɪᴘ: ᴜꜱᴇ /cancel ᴛᴏ ꜱᴛᴏᴘ.",
            parse_mode="Markdown",
        )


async def validate_channel_access(
    bot,
    channel_id: int,
    *,
    require_post: bool = False,
    require_delete: bool = False,
) -> tuple[bool, str]:
    try:
        chat = await bot.get_chat(channel_id)
        if chat.type != "channel":
            return False, "ᴛʜɪꜱ ɪᴅ ɪꜱ ɴᴏᴛ ᴀ ᴛᴇʟᴇɢʀᴀᴍ ᴄʜᴀɴɴᴇʟ."
        me = await bot.get_me()
        member = await bot.get_chat_member(channel_id, me.id)
        if member.status not in ("administrator", "creator"):
            return False, "ᴛʜᴇ ʙᴏᴛ ɪꜱ ɴᴏᴛ ᴀɴ ᴀᴅᴍɪɴ ɪɴ ᴛʜɪꜱ ᴄʜᴀɴɴᴇʟ."
        if require_post and getattr(member, "can_post_messages", True) is False:
            return False, "ɢɪᴠᴇ ᴛʜᴇ ʙᴏᴛ ᴘᴇʀᴍɪꜱꜱɪᴏɴ ᴛᴏ ᴘᴏꜱᴛ ᴍᴇꜱꜱᴀɢᴇꜱ."
        if require_delete and getattr(member, "can_delete_messages", False) is False:
            return False, "ɢɪᴠᴇ ᴛʜᴇ ʙᴏᴛ ᴛʜᴇ ᴅᴇʟᴇᴛᴇ ᴍᴇꜱꜱᴀɢᴇꜱ ᴘᴇʀᴍɪꜱꜱɪᴏɴ."
        return True, f"✅ ᴀᴄᴄᴇꜱꜱ ᴄᴏɴꜰɪʀᴍᴇᴅ: {chat.title}"
    except TelegramError as exc:
        logger.warning("Channel validation failed for %s: %s", channel_id, exc)
        return False, (
            "ᴄʜᴀɴɴᴇʟ ᴠᴇʀɪꜰɪᴄᴀᴛɪᴏɴ ꜰᴀɪʟᴇᴅ. ᴄʜᴇᴄᴋ ᴛʜᴇ ɪᴅ ᴀɴᴅ ᴀᴅᴅ ᴛʜᴇ ʙᴏᴛ ᴀꜱ ᴀɴ ᴀᴅᴍɪɴ."
        )


async def receive_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Ignore channel posts; any user can complete their interactive setup.
    if update.channel_post or update.edited_channel_post or not update.effective_message:
        return

    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return

    raw = (update.effective_message.text or "").strip()
    if awaiting in ("sold_channel", "unsold_channel", "broadcast_channel"):
        if not CHANNEL_ID_RE.fullmatch(raw):
            await update.effective_message.reply_text(
                "❌ ɪɴᴠᴀʟɪᴅ ɪᴅ. ᴄʜᴀɴɴᴇʟ ɪᴅ `-100...` ꜰᴏʀᴍᴀᴛ ᴍᴇɪɴ ꜱᴇɴᴅ ᴋᴀʀᴏ.",
                parse_mode="Markdown",
            )
            return

        user_id = update.effective_user.id if update.effective_user else None
        if user_id is None:
            return
        previous_ids = {
            key: get_channel_id(key, user_id=user_id)
            for key in ("sold_channel", "unsold_channel", "broadcast_channel")
        }
        for other_key, other_id in previous_ids.items():
            if other_key != awaiting and other_id == int(raw):
                await update.effective_message.reply_text(
                    "❌ ᴇᴀᴄʜ ꜱᴇᴛᴜᴘ ᴍᴇɪɴ ᴛᴇᴇɴ ᴀʟᴀɢ ᴄʜᴀɴɴᴇʟ ɪᴅ ᴅᴏ."
                )
                return

        is_broadcast = awaiting == "broadcast_channel"
        ok, message = await validate_channel_access(
            context.bot,
            int(raw),
            require_post=is_broadcast,
            require_delete=is_broadcast,
        )
        if not ok:
            await update.effective_message.reply_text(f"❌ {message}")
            return

        set_setting(awaiting, raw, user_id=update.effective_user.id)
        if awaiting == "sold_channel":
            context.user_data["awaiting"] = "unsold_channel"
            await update.effective_message.reply_text(
                f"{message}\n\n📢 ᴄʜᴀɴɴᴇʟ ꜱᴇᴛᴜᴘ (2/3)\n"
                "ᴀʙ ᴜɴꜱᴏʟᴅ ᴄʜᴀɴɴᴇʟ ɪᴅ ꜱᴇɴᴅ ᴋᴀʀᴏ."
            )
        elif awaiting == "unsold_channel":
            context.user_data["awaiting"] = "broadcast_channel"
            await update.effective_message.reply_text(
                f"{message}\n\n📢 ᴄʜᴀɴɴᴇʟ ꜱᴇᴛᴜᴘ (3/3)\n"
                "ᴀʙ ʀᴇᴘᴏʀᴛ/ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴄʜᴀɴɴᴇʟ ɪᴅ ꜱᴇɴᴅ ᴋᴀʀᴏ.\n"
                "ɴᴏᴛᴇ: ʙᴏᴛ ᴋᴏ ᴘᴏꜱᴛ ᴀᴜʀ ᴅᴇʟᴇᴛᴇ ᴍᴇꜱꜱᴀɢᴇꜱ ᴘᴇʀᴍɪꜱꜱɪᴏɴ ᴄʜᴀʜɪʏᴇ."
            )
        else:
            context.user_data.pop("awaiting", None)
            schedule_daily_report(context.application, update.effective_user.id)
            await update.effective_message.reply_text(
                f"{message}\n\n✅ ᴛᴇᴇɴᴏɴ ᴄʜᴀɴɴᴇʟꜱ ꜱᴇᴛ ʜᴏ ɢᴀʏᴇ.\n"
                "🧪 ᴀᴜᴛᴏ-ᴅᴇʟᴇᴛᴇ ᴛᴇꜱᴛ ᴋᴀʀɴᴇ ᴋᴇ ʟɪʏᴇ System Test ᴅᴀʙᴀᴏ."
            )
        return

    if awaiting == "report_time":
        try:
            parsed = datetime.strptime(raw, "%H:%M").time()
        except ValueError:
            await update.effective_message.reply_text(
                "❌ ᴛɪᴍᴇ 24-hour ꜰᴏʀᴍᴀᴛ ᴍᴇɪɴ ᴅᴏ: `21:00` ʏᴀ `09:30`.",
                parse_mode="Markdown",
            )
            return
        set_setting("report_time", parsed.strftime("%H:%M"), user_id=update.effective_user.id)
        context.user_data.pop("awaiting", None)
        schedule_daily_report(context.application, update.effective_user.id)
        await update.effective_message.reply_text(
            f"✅ ᴅᴀɪʟʏ ʀᴇᴘᴏʀᴛ ᴛɪᴍᴇ ꜱᴇᴛ: {parsed.strftime('%H:%M')} IST."
        )
        return


async def receive_sticker_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.channel_post or update.edited_channel_post or not update.effective_message:
        return
    if context.user_data.get("awaiting") != "report_sticker":
        return
    sticker = update.effective_message.sticker
    if not sticker:
        return
    set_setting("report_sticker", sticker.file_id, user_id=update.effective_user.id)
    context.user_data.pop("awaiting", None)
    await update.effective_message.reply_text("✅ ʀᴇᴘᴏʀᴛ ꜱᴛɪᴄᴋᴇʀ ꜱᴀᴠᴇᴅ.")


# ---------------------------------------------------------------------------
# FILE TRACKING AND REPORTS
# ---------------------------------------------------------------------------
async def track_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.channel_post
    if not message or not message.document:
        return
    chat_id = message.chat_id
    file_name = message.document.file_name or "unnamed file"
    posted_at = (message.date or datetime.now(timezone.utc)).astimezone(TIMEZONE).isoformat()

    # A channel can be configured by multiple users. Record the same post separately
    # for each user who configured this channel, using their own Sold/Unsold mapping.
    try:
        with db_connect() as conn:
            mappings = conn.execute(
                "SELECT user_id, key FROM user_settings "
                "WHERE key IN ('sold_channel', 'unsold_channel') AND value = ?",
                (str(chat_id),),
            ).fetchall()
            if not mappings:
                return
            for mapping in mappings:
                user_id = int(mapping["user_id"])
                category = "sold" if mapping["key"] == "sold_channel" else "unsold"
                conn.execute(
                    "INSERT OR IGNORE INTO user_file_events "
                    "(user_id, chat_id, message_id, category, file_name, posted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, chat_id, message.message_id, category, file_name, posted_at),
                )
            # Keep one legacy copy for installations that inspect the original table.
            first_category = "sold" if mappings[0]["key"] == "sold_channel" else "unsold"
            conn.execute(
                "INSERT OR IGNORE INTO file_events "
                "(chat_id, message_id, category, file_name, posted_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, message.message_id, first_category, file_name, posted_at),
            )
    except sqlite3.Error:
        logger.exception("Could not store a file event")


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    await update.effective_message.reply_text(build_daily_report(user_id=user_id))


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_stats(update, context)


async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    today_counts = get_counts_for_day(user_id=user_id)
    all_counts = get_total_counts(user_id=user_id)
    total_today = today_counts["sold"] + today_counts["unsold"]
    total_all = all_counts["sold"] + all_counts["unsold"]
    text = (
        "╔══════════════════════════════╗\n"
        "║       📁 ꜰɪʟᴇ ꜱᴛᴀᴛꜱ       ║\n"
        "╚══════════════════════════════╝\n\n"
        "📅 ᴛᴏᴅᴀʏ\n"
        f"✅ ꜱᴏʟᴅ: {today_counts['sold']}\n"
        f"❌ ᴜɴꜱᴏʟᴅ: {today_counts['unsold']}\n"
        f"📦 ᴛᴏᴛᴀʟ: {total_today}\n\n"
        "📚 ᴀʟʟ-ᴛɪᴍᴇ\n"
        f"✅ ꜱᴏʟᴅ: {all_counts['sold']}\n"
        f"❌ ᴜɴꜱᴏʟᴅ: {all_counts['unsold']}\n"
        f"📦 ᴛᴏᴛᴀʟ: {total_all}\n\n"
        "ɴᴏᴛᴇ: ᴇᴀᴄʜ ᴜꜱᴇʀ'ꜱ ꜱᴛᴀᴛꜱ ᴀɴᴅ ʜɪꜱᴛᴏʀʏ ᴀʀᴇ ꜱᴇᴘᴀʀᴀᴛᴇ."
    )
    await update.effective_message.reply_text(text, reply_markup=main_keyboard())


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = int(context.job.data)
    channel_id = get_channel_id("broadcast_channel", user_id=user_id)
    if not channel_id:
        logger.warning("Scheduled report skipped for user %s: no broadcast channel", user_id)
        return
    try:
        await context.bot.send_message(
            chat_id=channel_id, text=build_daily_report(user_id=user_id)
        )
        sticker_id = get_setting("report_sticker", user_id=user_id)
        if sticker_id:
            await context.bot.send_sticker(chat_id=channel_id, sticker=sticker_id)
        logger.info("Daily report sent for user %s to channel %s", user_id, channel_id)
    except TelegramError:
        logger.exception("Failed to send scheduled daily report for user %s", user_id)


def schedule_daily_report(application: Application, user_id: int) -> None:
    job_queue = application.job_queue
    if job_queue is None:
        logger.error(
            "JobQueue is unavailable. Install dependencies with: "
            'pip install "python-telegram-bot[job-queue]>=21.10,<23.0"'
        )
        return
    job_name = f"{DAILY_JOB_NAME}_{user_id}"
    for existing_job in job_queue.get_jobs_by_name(job_name):
        existing_job.schedule_removal()
    raw_time = get_setting("report_time", "21:00", user_id=user_id)
    try:
        parsed = datetime.strptime(raw_time, "%H:%M").time()
    except ValueError:
        parsed = time(hour=21, minute=0)
    local_time = time(hour=parsed.hour, minute=parsed.minute, tzinfo=TIMEZONE)
    job_queue.run_daily(
        daily_report_job, time=local_time, name=job_name, data=user_id
    )
    logger.info("Daily report scheduled for user %s at %s IST", user_id, raw_time)


def schedule_all_daily_reports(application: Application) -> None:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM user_settings WHERE key = 'broadcast_channel' AND value != ''"
        ).fetchall()
    for row in rows:
        schedule_daily_report(application, int(row["user_id"]))


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    lines = ["╔══════════════════════╗", "║    🗓 7-ᴅᴀʏ ʜɪꜱᴛᴏʀʏ    ║", "╚══════════════════════╝", ""]
    for day, sold, unsold in get_recent_history(user_id, 7) if user_id is not None else []:
        lines.append(f"{day.strftime('%d %b')}: ✅ {sold}  |  ❌ {unsold}  |  ᴛᴏᴛᴀʟ {sold + unsold}")
    await update.effective_message.reply_text("\n".join(lines), reply_markup=main_keyboard())


async def show_system_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    configured = {
        "ꜱᴏʟᴅ": get_channel_id("sold_channel", user_id=user_id),
        "ᴜɴꜱᴏʟᴅ": get_channel_id("unsold_channel", user_id=user_id),
        "ʀᴇᴘᴏʀᴛ": get_channel_id("broadcast_channel", user_id=user_id),
    }
    channel_lines = [f"{'✅' if channel_id else '⚠️'} {name}: {'SET' if channel_id else 'NOT SET'}" for name, channel_id in configured.items()]
    queue_ok = context.application.job_queue is not None
    text = (
        "╔══════════════════════╗\n"
        "║    🖥 ꜱʏꜱᴛᴇᴍ ꜱᴛᴀᴛᴜꜱ    ║\n"
        "╚══════════════════════╝\n\n"
        + "\n".join(channel_lines)
        + f"\n\n🕒 ʀᴇᴘᴏʀᴛ ᴛɪᴍᴇ: {get_setting('report_time', '21:00', user_id=user_id)} IST"
        + f"\n⏱ ᴊᴏʙ Qᴜᴇᴜᴇ: {'✅ READY' if queue_ok else '❌ MISSING'}"
        + f"\n🎟 ʀᴇᴘᴏʀᴛ ꜱᴛɪᴄᴋᴇʀ: {'✅ SET' if get_setting('report_sticker', user_id=user_id) else 'OFF'}"
        + "\n\nᴅᴀᴛᴀʙᴀꜱᴇ: ✅ SQLITE"
    )
    await update.effective_message.reply_text(text, reply_markup=settings_keyboard())


# ---------------------------------------------------------------------------
# SYSTEM TEST — test report is the ONLY message this code auto-deletes.
# ---------------------------------------------------------------------------
async def delete_test_report(application: Application, channel_id: int, message_id: int) -> None:
    await asyncio.sleep(TEST_REPORT_DELETE_SECONDS)
    try:
        await application.bot.delete_message(chat_id=channel_id, message_id=message_id)
        logger.info("Deleted temporary system test report %s from %s", message_id, channel_id)
    except (TelegramError, BadRequest):
        logger.exception(
            "Could not auto-delete system test report. Confirm bot has delete-message permission in the report channel."
        )


async def check_channel_for_test(bot, channel_id: int | None) -> str:
    if not channel_id:
        return "❌ NOT CONFIGURED"
    ok, message = await validate_channel_access(bot, channel_id)
    return "✅ ADMIN ACCESS" if ok else f"❌ {message}"


async def run_system_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await query.answer("Starting system test…")

    message = update.effective_message
    user_id = update.effective_user.id if update.effective_user else None
    broadcast_id = get_channel_id("broadcast_channel", user_id=user_id)
    if not broadcast_id:
        if message:
            await message.reply_text("❌ ʀᴇᴘᴏʀᴛ ᴄʜᴀɴɴᴇʟ ɴᴏᴛ ꜱᴇᴛ. ᴜꜱᴇ 📢 Set Channels ɪɴ ᴛʜᴇ ᴍᴀɪɴ ᴍᴇɴᴜ.")
        return

    sold_status = await check_channel_for_test(context.bot, get_channel_id("sold_channel", user_id=user_id))
    unsold_status = await check_channel_for_test(context.bot, get_channel_id("unsold_channel", user_id=user_id))
    queue_status = "✅ READY" if context.application.job_queue else "❌ MISSING"
    try:
        with db_connect() as conn:
            conn.execute("SELECT 1").fetchone()
        db_status = "✅ CONNECTED"
    except sqlite3.Error as exc:
        logger.exception("Database test failed")
        db_status = f"❌ ERROR: {type(exc).__name__}"

    report_day = datetime.now(TIMEZONE).date()
    counts = get_counts_for_day(report_day, user_id=user_id)
    total = counts["sold"] + counts["unsold"]
    date_text = f"{report_day.day} {report_day.strftime('%B %Y')}"
    test_report = (
        "🧪 TEST REPORT\n"
        "📊 ᴀᴄᴄᴏᴜɴᴛ ꜱᴀʟᴇꜱ ʀᴇᴘᴏʀᴛ\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 ᴅᴀᴛᴇ : {date_text}\n\n"
        f"📦 ᴛᴏᴛᴀʟ ᴀᴄᴄᴏᴜɴᴛꜱ : {total}\n\n"
        f"✅ ꜱᴏʟᴅ ᴀᴄᴄᴏᴜɴᴛꜱ : {counts['sold']}\n"
        f"❌ ᴜɴꜱᴏʟᴅ ᴀᴄᴄᴏᴜɴᴛꜱ : {counts['unsold']}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ ꜱᴛᴀᴛᴜꜱ : ᴄᴏᴍᴘʟᴇᴛᴇ ✓\n\n"
        "⏱️ ᴛʜɪꜱ ᴛᴇꜱᴛ ʀᴇᴘᴏʀᴛ ᴡɪʟʟ ʙᴇ ᴅᴇʟᴇᴛᴇᴅ ɪɴ 5 ᴍɪɴᴜᴛᴇꜱ."
    )
    try:
        sent_message = await context.bot.send_message(chat_id=broadcast_id, text=test_report)
        # Schedule deletion for this exact test message only. Normal report code never calls this.
        context.application.create_task(
            delete_test_report(context.application, broadcast_id, sent_message.message_id),
            name=f"delete-system-test-{sent_message.message_id}",
        )
        if message:
            await message.reply_text(
                "✅ ᴛᴇꜱᴛ ʀᴇᴘᴏʀᴛ ᴄʜᴀɴɴᴇʟ ᴘᴀʀ ᴘᴏꜱᴛ ʜᴏ ɢᴀʏɪ.\n"
                "🧹 ʏᴇʜ ᴛᴇꜱᴛ ᴍᴇꜱꜱᴀɢᴇ 5 ᴍɪɴᴜᴛᴇ ʙᴀᴀᴅ ᴅᴇʟᴇᴛᴇ ʜᴏɢɪ.\n"
                "📌 ᴅᴀɪʟʏ ʀᴇᴘᴏʀᴛꜱ ᴘᴀʀ ɪꜱꜱᴇ ᴋᴏɪ ᴀꜱᴀʀ ɴᴀʜɪɴ ʜᴏɢᴀ."
            )
    except TelegramError as exc:
        logger.exception("Could not post system test report")
        if message:
            await message.reply_text(
                "❌ ᴛᴇꜱᴛ ʀᴇᴘᴏʀᴛ ᴘᴏꜱᴛ ɴᴀʜɪɴ ʜᴜɪ. ʀᴇᴘᴏʀᴛ ᴄʜᴀɴɴᴇʟ ɪᴅ, ʙᴏᴛ ᴀᴅᴍɪɴ ᴀᴜʀ ᴘᴏꜱᴛ ᴘᴇʀᴍɪꜱꜱɪᴏɴ ᴄʜᴇᴄᴋ ᴋᴀʀᴏ.\n"
                f"ᴇʀʀᴏʀ: {type(exc).__name__}"
            )


# ---------------------------------------------------------------------------
# CALLBACKS AND COMMANDS
# ---------------------------------------------------------------------------
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_settings(update, context)


async def setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await configure_channels(update, context)


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_system_test(update, context)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    data = query.data or ""

    if data == "menu:test":
        await run_system_test(update, context)
        return

    await query.answer()

    if data == "menu:home":
        await show_main_menu(update, context, edit=True)
    elif data == "menu:report":
        await query.message.reply_text(build_daily_report(user_id=update.effective_user.id if update.effective_user else None))
    elif data == "menu:stats":
        await show_stats(update, context)
    elif data == "menu:contact":
        await query.message.reply_text(f"📞 ꜱᴜᴘᴘᴏʀᴛ: {SUPPORT_USERNAME}")
    elif data == "menu:setup":
        await configure_channels(update, context)
    elif data == "menu:settings":
        await show_settings(update, context)
    elif data == "settings:time":
        context.user_data["awaiting"] = "report_time"
        await query.message.reply_text(
            "🕒 ʀᴇᴘᴏʀᴛ ᴛɪᴍᴇ ʙʜᴇᴊᴏ (IST, 24-hour format).\n"
            "ᴇxᴀᴍᴘʟᴇ: `21:00` ʏᴀ `09:30`.\nᴄᴀɴᴄᴇʟ: /cancel",
            parse_mode="Markdown",
        )
    elif data == "settings:sticker":
        context.user_data["awaiting"] = "report_sticker"
        await query.message.reply_text("🎟 ᴀʙ ᴡᴏʜ ꜱᴛɪᴄᴋᴇʀ ʙʜᴇᴊᴏ ᴊᴏ ʜᴀʀ ᴅᴀɪʟʏ ʀᴇᴘᴏʀᴛ ᴋᴇ ʙᴀᴀᴅ ʙʜᴇᴊɴᴀ ʜᴀɪ.\nᴄᴀɴᴄᴇʟ: /cancel")
    elif data == "settings:remove_sticker":
        set_setting("report_sticker", "", user_id=update.effective_user.id if update.effective_user else None)
        await query.message.reply_text("✅ ʀᴇᴘᴏʀᴛ ꜱᴛɪᴄᴋᴇʀ ʀᴇᴍᴏᴠᴇᴅ.")
    elif data == "settings:history":
        await show_history(update, context)
    elif data == "settings:status":
        await show_system_status(update, context)


async def text_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the persistent main-menu keyboard shared by all users."""
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    if text == "📢 Add Channels":
        await configure_channels(update, context)  # configure_channels is available to all users
    elif text == "📊 Daily Sales Report":
        await message.reply_text(build_daily_report(user_id=update.effective_user.id if update.effective_user else None), reply_markup=main_keyboard())
    elif text == "📁 Account File Stats":
        await show_stats(update, context)
        await message.reply_text("ᴍᴀɪɴ ᴍᴇɴᴜ ʀᴇᴀᴅʏ.", reply_markup=main_keyboard())
    elif text == "🧪 System Test":
        await run_system_test(update, context)  # available to all users
    elif text == "📜 Report History":
        await show_history(update, context)
        await message.reply_text("ᴍᴀɪɴ ᴍᴇɴᴜ ʀᴇᴀᴅʏ.", reply_markup=main_keyboard())
    elif text == "⚙️ Settings":
        await show_settings(update, context)
    elif text == "🕒 Change Report Time":
        context.user_data["awaiting"] = "report_time"
        await message.reply_text(
            "🕒 ʀᴇᴘᴏʀᴛ ᴛɪᴍᴇ ʙʜᴇᴊᴏ (IST, 24-hour format).\n"
            "ᴇxᴀᴍᴘʟᴇ: `21:00` ʏᴀ `09:30`.\nᴄᴀɴᴄᴇʟ: /cancel",
            parse_mode="Markdown",
        )
    elif text == "🎟 Set Report Sticker":
        context.user_data["awaiting"] = "report_sticker"
        await message.reply_text("🎟 ᴀʙ ᴡᴏʜ ꜱᴛɪᴄᴋᴇʀ ʙʜᴇᴊᴏ ᴊᴏ ʜᴀʀ ᴅᴀɪʟʏ ʀᴇᴘᴏʀᴛ ᴋᴇ ʙᴀᴀᴅ ʙʜᴇᴊɴᴀ ʜᴀɪ.\nᴄᴀɴᴄᴇʟ: /cancel", reply_markup=settings_keyboard())
    elif text == "🗑 Remove Report Sticker":
        set_setting("report_sticker", "", user_id=update.effective_user.id if update.effective_user else None)
        await message.reply_text("✅ ʀᴇᴘᴏʀᴛ ꜱᴛɪᴄᴋᴇʀ ʀᴇᴍᴏᴠᴇᴅ.", reply_markup=settings_keyboard())
    elif text == "📊 Report Status":
        await show_system_status(update, context)
    elif text == "🏠 Main Menu":
        context.user_data.pop("awaiting", None)
        await show_main_menu(update, context)
    elif text == "📞 Contact Us":
        await message.reply_text(
            f"╔══════════════════════╗\n║    📞 ꜱᴜᴘᴘᴏʀᴛ    ║\n╚══════════════════════╝\n\n{SUPPORT_USERNAME}",
            reply_markup=main_keyboard(),
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot error", exc_info=context.error)


async def post_init(application: Application) -> None:
    init_database()
    schedule_all_daily_reports(application)
    try:
        await application.bot.set_my_description(description=BOT_DESCRIPTION)
    except TelegramError:
        logger.warning("Could not update bot profile description automatically; set it in @BotFather if needed.")

    # Send the startup announcement privately to the owner, not to any channel.
    try:
        online_time = datetime.now(TIMEZONE).strftime("%d %B %Y • %H:%M:%S IST")
        await application.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                "📢 ᴀɴɴᴏᴜɴᴄᴇᴍᴇɴᴛ\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⚡ {BOT_NAME.upper()} ɪꜱ ɴᴏᴡ ᴏɴʟɪɴᴇ.\n\n"
                "ʙᴏᴛ ꜱʏꜱᴛᴇᴍꜱ ᴀʀᴇ ʀᴇᴀᴅʏ.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "ᴛʏᴘᴇ /start ᴛᴏ ɢᴇᴛ ꜱᴛᴀʀᴛᴇᴅ.\n\n"
                f"🕒 {online_time}"
            ),
        )
    except TelegramError:
        logger.warning(
            "Could not DM the startup announcement to OWNER_ID. Open the bot and send /start first.",
            exc_info=True,
        )
    logger.info("%s started", BOT_NAME)


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Set BOT_TOKEN in kiyotaka_sales_tracker_v4.py, or define the BOT_TOKEN environment variable."
        )
    if OWNER_ID == 123456789:
        logger.warning("OWNER_ID is still the example value. Set it to your own Telegram numeric user ID.")

    init_database()
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("setup", setup_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("setting", settings_command))
    application.add_handler(CommandHandler("test", test_command))

    # Buttons and channel document tracking
    application.add_handler(MessageHandler(filters.Document.ALL, track_file))

    # The same main-menu keyboard is shown to everyone; the callback functions enforce owner permissions.
    menu_labels = r"^(📢 Add Channels|📊 Daily Sales Report|📁 Account File Stats|🧪 System Test|📜 Report History|⚙️ Settings|📞 Contact Us|🕒 Change Report Time|🎟 Set Report Sticker|🗑 Remove Report Sticker|📊 Report Status|🏠 Main Menu)$"
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(menu_labels), text_menu_button))

    # Interactive setup input is available to all users.
    application.add_handler(MessageHandler(filters.Sticker.ALL, receive_sticker_input))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text_input))

    application.add_error_handler(error_handler)
    logger.info("Starting polling…")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
