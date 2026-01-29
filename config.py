# config.py
import os
from decimal import Decimal

# --- BINGX API ---
API_KEY = os.getenv("BINGX_API_KEY")
API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = os.getenv("BINGX_BASE", "https://open-api.bingx.com")

# --- TELEGRAM ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- DATABASE ---
DATABASE_URL = os.getenv("DATABASE_URL")  # Render PostgreSQL URL

# --- DEFAULT TRADE SETTINGS ---
LEVERAGE = int(os.getenv("LEVERAGE", "20"))
TRADE_SIZE = float(os.getenv("TRADE_SIZE", "100"))
TP_PERCENT = Decimal(os.getenv("TP_PERCENT", "0.5"))
SL_PERCENT = Decimal(os.getenv("SL_PERCENT", "40"))

# --- NEW: Default USD amount for first order ---
DEFAULT_TRADE_USD = float(os.getenv("DEFAULT_TRADE_USD", "1.0"))  # $ amount for initial order

# --- DCA SETTINGS ---
DCA_COUNT = int(os.getenv("DCA_COUNT", "4"))
DCA_DEVIATION_PERCENT = Decimal(os.getenv("DCA_DEVIATION_PERCENT", "5"))
DCA_VOLUME_MULTIPLIER = Decimal(os.getenv("DCA_VOLUME_MULTIPLIER", "2"))

# --- AUTO CLOSE ---
AUTO_CLOSE_FROM_DCA = int(os.getenv("AUTO_CLOSE_FROM_DCA", "1"))
AUTO_CLOSE_BUFFER = Decimal(os.getenv("AUTO_CLOSE_BUFFER", "0.0"))

# --- LIMITS ---
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "20"))

# --- WEB / EXECUTOR ---
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "5000"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")

# --- WORKER INTERVALS & LOGGING ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
TPSL_CHECK_INTERVAL = float(os.getenv("TPSL_CHECK_INTERVAL", "5"))
DCA_CHECK_INTERVAL = float(os.getenv("DCA_CHECK_INTERVAL", "30"))
MIN_SECONDS_BETWEEN_DCA = float(os.getenv("MIN_SECONDS_BETWEEN_DCA", "3600"))

# --- Defaults used by executor if payload omits them ---
DEFAULT_TP_PERCENT = TP_PERCENT
DEFAULT_SL_PERCENT = SL_PERCENT
DEFAULT_TRADE_QTY = TRADE_SIZE
