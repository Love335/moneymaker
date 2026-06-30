# config.example.py
# Copy this file to config.py and fill in your real credentials.
# config.py is gitignored and must never be committed.

# ── Avanza credentials ────────────────────────────────────────
AVANZA_USERNAME    = "your_username_here"
AVANZA_PASSWORD    = "your_password_here"
AVANZA_TOTP_SECRET = "your_totp_secret_here"
AVANZA_ACCOUNT_ID =  "your_account_id_here"

# ── Optional: Telegram alerts ─────────────────────────────────
# Leave as None to disable Telegram notifications
TELEGRAM_BOT_TOKEN = None
TELEGRAM_CHAT_ID   = None