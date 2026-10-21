"""Kiyotaka Sales Tracker configuration.

Use host environment variables for deployment secrets. Never save your real
Telegram bot token in this file or commit it to a public repository.
"""

import os
import re
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Intentionally blank by default; define BOT_TOKEN in your host's environment.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

try:
    OWNER_ID = int(os.getenv("OWNER_ID", "0").strip() or "0")
except ValueError as exc:
    raise RuntimeError("OWNER_ID must be your numeric Telegram user ID.") from exc

BOT_NAME = "Kiyotaka Sales Tracker V4"
SUPPORT_USERNAME = "@AlwaysKiyotaka"
DB_FILE = os.getenv("DB_FILE", "kiyotaka_sales_v4.db").strip() or "kiyotaka_sales_v4.db"
TEST_REPORT_DELETE_SECONDS = 300  # Only temporary System Test reports are deleted.
DAILY_JOB_NAME = "kiyotaka_daily_sales_report"
CHANNEL_ID_RE = re.compile(r"-100\d+\Z")

try:
    TIMEZONE = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:  # Fallback for runtimes without installed timezone data.
    TIMEZONE = timezone(timedelta(hours=5, minutes=30))

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
