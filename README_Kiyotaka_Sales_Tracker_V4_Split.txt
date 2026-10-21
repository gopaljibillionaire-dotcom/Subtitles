Kiyotaka Sales Tracker V4 — split-file package

Files required together in the same folder:
- Main.py: bot logic; run this file
- Config.py: BOT_TOKEN, OWNER_ID, and bot settings
- requirements.txt: Python dependencies
- runtime.txt: Python runtime hint for supported hosting platforms

Setup:
1. Open Config.py and replace PASTE_YOUR_BOT_TOKEN_HERE with your Telegram bot token.
2. Set OWNER_ID to your numeric Telegram user ID.
3. Install dependencies: pip install -r requirements.txt
4. Run Main.py.

Keep kiyotaka_sales_v4.db in the same folder between restarts; do not delete it if you want to preserve saved data.
For Pydroid 3, runtime.txt is generally not used; install requirements and run Main.py.
