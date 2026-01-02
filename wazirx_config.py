import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ============= WAZIRX API CREDENTIALS =============
WAZIRX_API_KEY = os.getenv("WAZIRX_API_KEY", "")
WAZIRX_SECRET_KEY = os.getenv("WAZIRX_SECRET_KEY", "")

# ============= TRADING CONTROLS =============
TRADING_ENABLED = True  # Master switch
DRY_RUN = True  # Simulation mode

# ============= RISK MANAGEMENT =============
RISK_PER_TRADE_PERCENT = 1.0
MAX_POSITION_SIZE_USDT = 100
MIN_BALANCE_USDT = 10
MAX_DAILY_LOSS_USDT = 50

# ============= TRADING PARAMETERS =============
SLIPPAGE_PERCENT = 0.5
MAX_OPEN_POSITIONS = 3

# ============= SYMBOL MAPPING =============
SYMBOL_MAP = {
    'BTCUSD': 'BTC/USDT',
    'ETHUSD': 'ETH/USDT',
    'BNBUSD': 'BNB/USDT',
    'XRPUSD': 'XRP/USDT',
    'ADAUSD': 'ADA/USDT',
    'SOLUSD': 'SOL/USDT',
    'DOGEUSD': 'DOGE/USDT',
    'MATICUSD': 'MATIC/USDT',
    'DOTUSD': 'DOT/USDT',
    'SHIBUSD': 'SHIB/USDT',
}

# ============= ALLOWED SYMBOLS =============
ALLOWED_SYMBOLS = [
    'btc/usdt',
    'eth/usdt',
    'bnb/usdt',
    'xrp/usdt',
    'ada/usdt',
    'sol/usdt',
    'doge/usdt',
    'matic/usdt',
    'dot/usdt',
    'shib/usdt',
]

# ============= TRADING HOURS =============
TRADING_24_7 = True
RESTRICTED_HOURS = [0, 1, 2, 3, 4, 5]

# ============= LOGGING =============
LOG_TRADES_TO_FILE = True
LOG_FILE_PATH = "trading_bot.log"

# ============= TELEGRAM NOTIFICATIONS =============
TELEGRAM_ENABLED = True
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ============= STOP LOSS / TAKE PROFIT =============
DEFAULT_SL_PERCENT = 2.0
DEFAULT_TP_PERCENT = 4.0
TRAILING_STOP_ENABLED = False
TRAILING_STOP_PERCENT = 1.5

# ============= ORDER MONITORING =============
ORDER_CHECK_INTERVAL_SECONDS = 5
ORDER_TIMEOUT_MINUTES = 30

# ============= EXCHANGE SETTINGS =============
RATE_LIMIT_ENABLED = True
REQUEST_TIMEOUT_SECONDS = 10