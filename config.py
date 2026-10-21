"""Configuration for Kiyotaka Sales Tracker V4.
Edit BOT_TOKEN and OWNER_ID here before running, or set environment variables.
"""
import os
import re

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "123456789"))  # Your numeric Telegram user ID

BOT_NAME = "Kiyotaka Sales Tracker V4"
SUPPORT_USERNAME = "@AlwaysKiyotaka"
DB_FILE = "kiyotaka_sales_v4.db"
TEST_REPORT_DELETE_SECONDS = 300  # Test report only; daily reports are not deleted.
DAILY_JOB_NAME = "kiyotaka_daily_sales_report"
CHANNEL_ID_RE = re.compile(r"-100\d+\Z")

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
