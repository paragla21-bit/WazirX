# wazirx_config.py
import os
from dotenv import load_dotenv

load_dotenv()

# ============= API CREDENTIALS =============
WAZIRX_API_KEY = os.getenv("uAmqQjmmwUYwPu04T8zOXAGgwO42DjHWrtEjh1K66l0HzKUgJLPAr98ThDYX8355", "")
WAZIRX_SECRET_KEY = os.getenv("ENmclYgpDUMfc90dHwuEOo4rjWUt5GrAoabKFtYU", "")

# ============= TRADING SYMBOLS (WazirX Format) =============
# WazirX uses lowercase with no separator: btcusdt, ethusdt
SYMBOL_MAP = {
    "BTCUSD": "btcusdt",
    "ETHUSD": "ethusdt",
    "BNBUSD": "bnbusdt",
    "SOLUSD": "solusdt",
    "MATICUSD": "maticusdt",
    "ADAUSD": "adausdt",
    "DOGEUSDT": "dogeusdt",
    "XRPUSDT": "xrpusdt"
}

# ============= RISK MANAGEMENT =============
MAX_DAILY_LOSS_INR = 2000  # ₹2000 max loss per day
MAX_DAILY_LOSS_USDT = 25  # $25 equivalent
MAX_POSITION_SIZE_USDT = 100  # Max $100 per trade
RISK_PER_TRADE_PERCENT = 2  # 2% of balance
MIN_BALANCE_USDT = 10  # Minimum USDT to keep in account

# ============= ORDER SETTINGS =============
ORDER_TYPE = "limit"  # WazirX works better with limit orders
SLIPPAGE_PERCENT = 0.2  # 0.2% slippage for limit orders
ORDER_TIMEOUT_SECONDS = 30  # Cancel order if not filled

# ============= ALLOWED TRADING =============
ALLOWED_SYMBOLS = ["btcusdt", "ethusdt", "bnbusdt", "solusdt"]
TRADING_ENABLED = True
DRY_RUN = False  # Set True for testing without real orders

# ============= STOP LOSS & TAKE PROFIT =============
USE_PERCENTAGE_SL_TP = True  # Use % based SL/TP if exchange doesn't support
EMERGENCY_STOP_LOSS_PERCENT = 5  # Emergency stop at 5% loss
AUTO_MOVE_TO_BREAKEVEN = True  # Move SL to breakeven at 2:1 RR
BREAKEVEN_TRIGGER_RR = 2.0  # Trigger breakeven at 2:1 RR

# ============= TRADING HOURS =============
# WazirX operates 24/7, but you can restrict hours
TRADING_24_7 = True
RESTRICTED_HOURS = []  # Example: [0, 1, 2, 3]  (12am-4am IST)

# ============= TELEGRAM NOTIFICATIONS =============
TELEGRAM_ENABLED = False
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ============= LOGGING =============
LOG_TRADES_TO_FILE = True
LOG_FILE_PATH = "trades_log.txt"