# config.py
import os

# Webhook
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/testorder")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "5000"))

# Database
DATABASE_URL = os.getenv("DATABASE_URL")

# BingX API
BINGX_API_KEY = os.getenv("BINGX_API_KEY")
BINGX_API_SECRET = os.getenv("BINGX_API_SECRET")
BINGX_BASE = os.getenv("BINGX_BASE", "https://open-api.bingx.com")

# Trading defaults
DEFAULT_LEVERAGE = int(os.getenv("LEVERAGE", "20"))
DEFAULT_TRADE_USD = float(os.getenv("DEFAULT_TRADE_USD", "1"))
MIN_QTY = float(os.getenv("MIN_QTY", "0.000001"))
STEP_SIZE = float(os.getenv("STEP_SIZE", "0.000001"))

# DCA / TPSL
DCA_CHECK_INTERVAL = int(os.getenv("DCA_CHECK_INTERVAL", "30"))
TPSL_CHECK_INTERVAL = int(os.getenv("TPSL_CHECK_INTERVAL", "5"))

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
