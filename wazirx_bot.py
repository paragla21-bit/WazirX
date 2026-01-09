from flask import Flask, request, jsonify
import ccxt
from wazirx_config import *
from datetime import datetime, timedelta
import json
import time
import requests
import threading
from functools import wraps
import os

app = Flask(__name__)

# ============= EXCHANGE SETUP =============
exchange = ccxt.wazirx({
    'apiKey': WAZIRX_API_KEY,
    'secret': WAZIRX_SECRET_KEY,
    'enableRateLimit': RATE_LIMIT_ENABLED,
    'timeout': REQUEST_TIMEOUT_SECONDS * 1000,
    'options': {
        'defaultType': 'spot',
    }
})

# ============= THREAD-SAFE DATA STRUCTURES =============
data_lock = threading.Lock()  # Protect shared data

# Daily tracking
daily_pnl_usdt = 0
daily_pnl_inr = 0
last_reset_date = datetime.now().date()
total_trades_today = 0
winning_trades_today = 0
losing_trades_today = 0

# Active orders
active_orders = {}  # {order_id: {symbol, side, sl, tp, entry_price, ...}}

# ============= RETRY DECORATOR =============
def retry_on_failure(max_retries=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise
                    log_message(f"⚠️ Retry {attempt + 1}/{max_retries} for {func.__name__}: {e}")
                    time.sleep(delay * (attempt + 1))
            return None
        return wrapper
    return decorator

# ============= DAILY TRACKING =============
def reset_daily_tracker():
    global daily_pnl_usdt, daily_pnl_inr, last_reset_date, total_trades_today
    global winning_trades_today, losing_trades_today
    
    today = datetime.now().date()
    if today != last_reset_date:
        with data_lock:
            daily_pnl_usdt = 0
            daily_pnl_inr = 0
            total_trades_today = 0
            winning_trades_today = 0
            losing_trades_today = 0
            last_reset_date = today
        log_message(f"✅ Daily tracker reset: {today}")

# ============= LOGGING =============
log_lock = threading.Lock()

def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    
    with log_lock:
        print(log_entry)
        if LOG_TRADES_TO_FILE:
            try:
                with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
                    f.write(log_entry + "\n")
            except Exception as e:
                print(f"❌ Logging error: {e}")

# ============= TELEGRAM NOTIFICATIONS =============
def send_telegram(message):
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        log_message(f"❌ Telegram error: {e}")

# ============= GET CURRENT BALANCE =============
@retry_on_failure(max_retries=3, delay=2)
def get_balance():
    try:
        balance = exchange.fetch_balance()
        usdt_free = balance.get('USDT', {}).get('free', 0)
        return {'usdt_free': float(usdt_free or 0)}
    except Exception as e:
        log_message(f"❌ Balance fetch error: {e}")
        return {'usdt_free': 0}

# ============= GET CURRENT PRICE =============
@retry_on_failure(max_retries=3, delay=1)
def get_current_price(symbol):
    try:
        ticker = exchange.fetch_ticker(symbol)
        return float(ticker['last'])
    except Exception as e:
        log_message(f"❌ Price fetch error for {symbol}: {e}")
        return None

# ============= SAFETY CHECKS =============
def check_safety_limits(data):
    reset_daily_tracker()
    if not TRADING_ENABLED:
        return False, "❌ Trading is disabled"
    
    with data_lock:
        if abs(daily_pnl_usdt) >= MAX_DAILY_LOSS_USDT:
            return False, f"❌ Daily loss limit reached: ${abs(daily_pnl_usdt):.2f}"
        if len(active_orders) >= MAX_OPEN_POSITIONS:
            return False, f"❌ Max positions reached"

    symbol = data.get('symbol', '')
    mapped_symbol = SYMBOL_MAP.get(symbol, symbol).lower()
    if mapped_symbol not in ALLOWED_SYMBOLS:
        return False, f"❌ Symbol not allowed: {mapped_symbol}"
    
    balance = get_balance()
    if balance['usdt_free'] < MIN_BALANCE_USDT:
        return False, f"❌ Low balance: ${balance['usdt_free']:.2f}"
    
    return True, "✅ Safe"

# ============= CALCULATE POSITION SIZE =============
def calculate_position_size(symbol, entry_price, stop_loss_price):
    try:
        balance = get_balance()
        available_capital = balance['usdt_free'] - MIN_BALANCE_USDT
        if available_capital <= 0: return 0, "No available capital"
        
        risk_amount = available_capital * (RISK_PER_TRADE_PERCENT / 100)
        sl_distance_percent = abs(entry_price - stop_loss_price) / entry_price
        
        if sl_distance_percent <= 0: return 0, "Invalid SL"
        
        pos_size_usdt = min(risk_amount / sl_distance_percent, MAX_POSITION_SIZE_USDT)
        quantity = pos_size_usdt / entry_price
        
        # Precision handling
        markets = exchange.load_markets()
        market = markets.get(symbol.upper())
        if market:
            precision = market.get('precision', {}).get('amount')
            if precision is not None: quantity = round(quantity, precision)
        
        if quantity * entry_price < 1.0: # Fixed Line 206 Syntax here
            return 0, "Order size < $1"
            
        return quantity, "OK"
    except Exception as e:
        return 0, str(e)

# ============= PLACE ORDER =============
@retry_on_failure(max_retries=2, delay=3)
def place_order(symbol, side, quantity, entry_price, sl_price, tp_price):
    try:
        if DRY_RUN:
            order_id = f'DRY_{int(time.time())}'
            with data_lock:
                active_orders[order_id] = {'symbol': symbol, 'side': side, 'quantity': quantity, 'entry_price': entry_price, 'sl_price': sl_price, 'tp_price': tp_price, 'timestamp': datetime.now(), 'status': 'dry_run'}
            return {'id': order_id}

        order = exchange.create_limit_order(symbol.upper(), side, quantity, entry_price)
        with data_lock:
            active_orders[order['id']] = {'symbol': symbol, 'side': side, 'quantity': quantity, 'entry_price': entry_price, 'sl_price': sl_price, 'tp_price': tp_price, 'timestamp': datetime.now(), 'status': 'open'}
        return order
    except Exception as e:
        log_message(f"❌ Order error: {e}")
        raise

# ============= MONITOR & WEBHOOK =============
def monitor_active_orders():
    with data_lock:
        items = list(active_orders.items())
    for oid, info in items:
        # Simplification: In real usage, add SL/TP check logic here
        pass

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if not data: return jsonify({"error": "No data"}), 400
        
        log_message(f"📨 ALERT: {json.dumps(data)}")
        is_safe, msg = check_safety_limits(data)
        if not is_safe: return jsonify({"status": "rejected", "reason": msg}), 400

        action = data.get('action', '').upper()
        symbol = SYMBOL_MAP.get(data.get('symbol'), data.get('symbol'))
        if not symbol.endswith('/USDT'): symbol = f"{symbol}/USDT"
        
        price = float(data.get('price', 0))
        sl = float(data.get('sl', 0))
        tp = float(data.get('tp', 0))
        
        quantity, q_msg = calculate_position_size(symbol, price, sl)
        if quantity <= 0: return jsonify({"error": q_msg}), 400

        order = place_order(symbol, action.lower(), quantity, price, sl, tp)
        return jsonify({"status": "success", "order_id": order.get('id')}), 200
    except Exception as e:
        log_message(f"❌ Webhook Crash: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/health')
def health():
    return jsonify({"status": "active", "time": str(datetime.now())}), 200

def start_monitor():
    def loop():
        while True:
            monitor_active_orders()
            time.sleep(30)
    threading.Thread(target=loop, daemon=True).start()

if __name__ == '__main__':
    start_monitor()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
