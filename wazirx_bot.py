import os
import json
import time
import threading
import requests
import ccxt
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
from functools import wraps
from wazirx_config import *

app = Flask(__name__)

# ============= GLOBAL DATA & LOCKS =============
data_lock = threading.Lock()
log_lock = threading.Lock()

# Tracking Variables
daily_pnl_usdt = 0
last_reset_date = datetime.now().date()
total_trades_today = 0
winning_trades_today = 0
losing_trades_today = 0
active_orders = {}  # Format: {order_id: {data}}

# ============= EXCHANGE INITIALIZATION =============
def get_exchange_instance():
    return ccxt.wazirx({
        'apiKey': WAZIRX_API_KEY,
        'secret': WAZIRX_SECRET_KEY,
        'enableRateLimit': True,
        'timeout': 30000,
        'options': {'defaultType': 'spot'}
    })

exchange = get_exchange_instance()

# ============= LOGGING & NOTIFICATIONS =============
def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    with log_lock:
        print(log_entry)
        if LOG_TRADES_TO_FILE:
            try:
                with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
                    f.write(log_entry + "\n")
            except: pass

def send_telegram(message):
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log_message(f"❌ Telegram Error: {e}")

# ============= HELPER DECORATORS =============
def retry_on_failure(max_retries=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        log_message(f"🚨 Final attempt failed for {func.__name__}: {e}")
                        return None
                    time.sleep(delay * (attempt + 1))
            return None
        return wrapper
    return decorator

# ============= CORE EXCHANGE FUNCTIONS =============
@retry_on_failure()
def fetch_safe_balance():
    """Fetch balance with None-check to prevent unpacking errors"""
    balance = exchange.fetch_balance()
    if not balance or 'USDT' not in balance:
        return {'free': 0, 'total': 0}
    return {
        'free': float(balance['USDT'].get('free', 0)),
        'total': float(balance['USDT'].get('total', 0))
    }

@retry_on_failure()
def get_current_price(symbol):
    ticker = exchange.fetch_ticker(symbol.upper())
    return float(ticker['last']) if ticker else None

# ============= TRADING LOGIC =============
def reset_daily_stats_if_needed():
    global daily_pnl_usdt, last_reset_date, total_trades_today
    global winning_trades_today, losing_trades_today
    
    today = datetime.now().date()
    with data_lock:
        if today != last_reset_date:
            daily_pnl_usdt = 0
            total_trades_today = 0
            winning_trades_today = 0
            losing_trades_today = 0
            last_reset_date = today
            log_message("📅 Daily stats reset for new day.")

def calculate_position_size(symbol, entry_price, sl_price):
    try:
        balance_data = fetch_safe_balance()
        if not balance_data:
            return 0, "Could not fetch balance from Exchange"
            
        usdt_free = balance_data['free']
        usable_balance = usdt_free - MIN_BALANCE_USDT
        
        if usable_balance < 1:
            return 0, f"Low Balance: ${usdt_free:.2f}"
            
        # Risk management
        risk_per_trade = usable_balance * (RISK_PER_TRADE_PERCENT / 100)
        sl_percent = abs(entry_price - sl_price) / entry_price
        
        if sl_percent <= 0: return 0, "Invalid SL/Price ratio"
        
        # Position Size in USDT
        pos_size_usdt = risk_per_trade / sl_percent
        pos_size_usdt = min(pos_size_usdt, MAX_POSITION_SIZE_USDT)
        pos_size_usdt = min(pos_size_usdt, usable_balance * 0.9) # Max 90% use
        
        qty = pos_size_usdt / entry_price
        
        # Precision Handling
        markets = exchange.load_markets()
        market = markets.get(symbol.upper())
        if market and 'precision' in market:
            amt_precision = market['precision'].get('amount', 8)
            qty = round(qty, amt_precision)
            
        # Min Order Check
        if (qty * entry_price) < 1.0:
            return 0, "Calculated order size below $1 minimum"
            
        return qty, "OK"
    except Exception as e:
        return 0, f"Calc Error: {str(e)}"

@retry_on_failure(max_retries=2)
def execute_trade(symbol, side, qty, price, sl, tp):
    try:
        if DRY_RUN:
            oid = f"DRY_{int(time.time())}"
            log_message(f"🔍 [DRY RUN] {side.upper()} {qty} {symbol} @ {price}")
        else:
            # WazirX limit order
            order = exchange.create_limit_order(symbol.upper(), side.lower(), qty, price)
            oid = order['id']
            log_message(f"✅ [LIVE] Order Placed: {oid}")

        with data_lock:
            active_orders[oid] = {
                'symbol': symbol, 'side': side.lower(), 'qty': qty,
                'entry': price, 'sl': sl, 'tp': tp,
                'time': datetime.now(), 'status': 'open'
            }
        
        send_telegram(f"🔔 <b>Trade Opened</b>\nSymbol: {symbol}\nSide: {side}\nQty: {qty}\nPrice: {price}\nSL: {sl}\nTP: {tp}")
        return oid
    except Exception as e:
        log_message(f"❌ Execution Error: {e}")
        return None

# ============= MONITORING THREAD =============
def monitor_positions():
    """Background task to check for SL/TP"""
    while True:
        try:
            reset_daily_stats_if_needed()
            with data_lock:
                current_orders = list(active_orders.items())
            
            for oid, info in current_orders:
                symbol = info['symbol']
                curr_price = get_current_price(symbol)
                
                if not curr_price: continue
                
                side = info['side']
                sl = info['sl']
                tp = info['tp']
                
                trigger_close = False
                reason = ""
                
                if side == 'buy':
                    if curr_price <= sl: trigger_close, reason = True, "Stop Loss"
                    elif curr_price >= tp: trigger_close, reason = True, "Take Profit"
                else: # Sell
                    if curr_price >= sl: trigger_close, reason = True, "Stop Loss"
                    elif curr_price <= tp: trigger_close, reason = True, "Take Profit"
                
                if trigger_close:
                    handle_close(oid, info, curr_price, reason)
                    
        except Exception as e:
            log_message(f"⚠️ Monitor Error: {e}")
        time.sleep(20)

def handle_close(oid, info, exit_price, reason):
    global daily_pnl_usdt, winning_trades_today, losing_trades_today
    
    # Calculate PnL
    pnl = (exit_price - info['entry']) * info['qty'] if info['side'] == 'buy' else (info['entry'] - exit_price) * info['qty']
    
    if not DRY_RUN:
        try:
            close_side = 'sell' if info['side'] == 'buy' else 'buy'
            exchange.create_market_order(info['symbol'].upper(), close_side, info['qty'])
        except Exception as e:
            log_message(f"❌ Failed to close live order {oid}: {e}")
            return

    with data_lock:
        daily_pnl_usdt += pnl
        if pnl > 0: winning_trades_today += 1
        else: losing_trades_today += 1
        if oid in active_orders: del active_orders[oid]
        
    log_message(f"📉 Closed {info['symbol']} at {exit_price} ({reason}). PnL: ${pnl:.2f}")
    send_telegram(f"🏁 <b>Trade Closed</b>\nSymbol: {info['symbol']}\nReason: {reason}\nExit: {exit_price}\nP&L: ${pnl:.2f}")

# ============= FLASK ROUTES =============
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        raw_data = request.get_data()
        data = json.loads(raw_data)
        
        log_message(f"📥 Signal: {json.dumps(data)}")
        
        # Validation
        safe, msg = check_safety_limits(data)
        if not safe:
            return jsonify({"status": "rejected", "reason": msg}), 400
            
        symbol_raw = data.get('symbol', 'BTCUSD')
        symbol = SYMBOL_MAP.get(symbol_raw, symbol_raw)
        if "/USDT" not in symbol: symbol += "/USDT"
        
        action = data.get('action', '').lower()
        price = float(data.get('price', 0))
        sl = float(data.get('sl', 0))
        tp = float(data.get('tp', 0))
        
        if price <= 0 or sl <= 0:
            return jsonify({"status": "error", "reason": "Invalid Price/SL"}), 400

        # Process
        qty, q_msg = calculate_position_size(symbol, price, sl)
        if qty <= 0:
            return jsonify({"status": "error", "reason": q_msg}), 400
            
        order_id = execute_trade(symbol, action, qty, price, sl, tp)
        
        if order_id:
            return jsonify({"status": "success", "id": order_id}), 200
        return jsonify({"status": "error", "reason": "Execution failed"}), 500

    except Exception as e:
        log_message(f"🚨 Webhook Crash: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

def check_safety_limits(data):
    if not TRADING_ENABLED: return False, "Trading Disabled"
    
    with data_lock:
        if abs(daily_pnl_usdt) >= MAX_DAILY_LOSS_USDT:
            return False, "Daily Loss Limit Reached"
        if len(active_orders) >= MAX_OPEN_POSITIONS:
            return False, "Max Positions Open"
            
    return True, "Safe"

@app.route('/health')
def health():
    balance = fetch_safe_balance()
    return jsonify({
        "status": "alive",
        "balance_usdt": balance['free'] if balance else "Error",
        "active_trades": len(active_orders),
        "daily_pnl": f"${daily_pnl_usdt:.2f}"
    }), 200

# ============= MAIN START =============
if __name__ == '__main__':
    log_message("🚀 Bot starting...")
    
    # Verify Connection
    test_balance = fetch_safe_balance()
    if test_balance is not None:
        log_message(f"✅ Connected to WazirX. Balance: ${test_balance['free']}")
    else:
        log_message("❌ Connection Failed! Check API Keys and IP restrictions.")

    # Start Monitor Thread
    t = threading.Thread(target=monitor_positions, daemon=True)
    t.start()
    
    # Render dynamic port
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
