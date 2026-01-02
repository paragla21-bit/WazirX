from flask import Flask, request, jsonify
import ccxt
from wazirx_config import *
from datetime import datetime, timedelta
import json
import time
import requests
import threading
from functools import wraps

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
    """Decorator to retry functions on failure"""
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
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, data=data, timeout=5)
        if response.status_code != 200:
            log_message(f"⚠️ Telegram API error: {response.status_code}")
    except Exception as e:
        log_message(f"❌ Telegram error: {e}")

# ============= GET CURRENT BALANCE =============
@retry_on_failure(max_retries=3, delay=2)
def get_balance():
    try:
        balance = exchange.fetch_balance()
        usdt_free = balance.get('USDT', {}).get('free', 0)
        usdt_total = balance.get('USDT', {}).get('total', 0)
        
        return {
            'usdt_free': float(usdt_free),
            'usdt_total': float(usdt_total)
        }
    except Exception as e:
        log_message(f"❌ Balance fetch error: {e}")
        return {'usdt_free': 0, 'usdt_total': 0}

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
    global daily_pnl_usdt
    reset_daily_tracker()
    
    # Check if trading enabled
    if not TRADING_ENABLED:
        return False, "❌ Trading is disabled in config"
    
    # Daily Loss Check (USDT)
    with data_lock:
        if abs(daily_pnl_usdt) >= MAX_DAILY_LOSS_USDT:
            return False, f"❌ Daily loss limit reached: ${abs(daily_pnl_usdt):.2f}"
    
    # Max positions check
    with data_lock:
        if len(active_orders) >= MAX_OPEN_POSITIONS:
            return False, f"❌ Maximum positions reached: {len(active_orders)}/{MAX_OPEN_POSITIONS}"
    
    # Symbol Check
    symbol = data.get('symbol', '')
    mapped_symbol = SYMBOL_MAP.get(symbol, symbol).lower()
    
    if mapped_symbol not in ALLOWED_SYMBOLS:
        return False, f"❌ Symbol not allowed: {mapped_symbol}"
    
    # Balance Check
    balance = get_balance()
    if balance['usdt_free'] < MIN_BALANCE_USDT:
        return False, f"❌ Insufficient balance: ${balance['usdt_free']:.2f}"
    
    # Trading Hours Check (if restricted)
    if not TRADING_24_7:
        current_hour = datetime.now().hour
        if current_hour in RESTRICTED_HOURS:
            return False, f"❌ Trading restricted at {current_hour}:00 IST"
    
    return True, "✅ All safety checks passed"

# ============= CALCULATE POSITION SIZE =============
def calculate_position_size(symbol, entry_price, stop_loss_price):
    try:
        balance = get_balance()
        usdt_free = balance['usdt_free']
        
        if usdt_free <= MIN_BALANCE_USDT:
            return 0, "Insufficient balance"
        
        # Available capital for this trade
        available_capital = usdt_free - MIN_BALANCE_USDT
        
        # Risk amount (max 2% of available capital)
        risk_amount = available_capital * (RISK_PER_TRADE_PERCENT / 100)
        risk_amount = min(risk_amount, MAX_POSITION_SIZE_USDT * 0.02)
        
        # SL distance
        sl_distance_percent = abs(entry_price - stop_loss_price) / entry_price
        
        if sl_distance_percent <= 0:
            return 0, "Invalid SL distance"
        
        # Position size in USDT
        position_size_usdt = risk_amount / sl_distance_percent
        position_size_usdt = min(position_size_usdt, MAX_POSITION_SIZE_USDT)
        position_size_usdt = min(position_size_usdt, available_capital * 0.8)  # Max 80%
        
        # Convert to crypto quantity
        quantity = position_size_usdt / entry_price
        
        # Get market info for precision
        markets = exchange.load_markets()
        market = markets.get(symbol.upper())
        
        if market:
            # Round to exchange precision
            precision = market.get('precision', {}).get('amount')
            if precision:
                quantity = round(quantity, precision)
            
            # Check minimum order size
            min_amount = market.get('limits', {}).get('amount', {}).get('min', 0)
            if min_amount and quantity < min_amount:
                return 0, f"Order below minimum: {min_amount}"
        
        # Minimum order size check (WazirX minimum ~$1)
        min_order_usdt = 1.0
        if quantity * entry_price < min_order_usdt:
            return 0, f"Order size too small (min ${min_order_usdt})"
        
        return quantity, "OK"
        
    except Exception as e:
        log_message(f"❌ Position size calculation error: {e}")
        return 0, str(e)

# ============= PLACE ORDER =============
@retry_on_failure(max_retries=2, delay=3)
def place_order(symbol, side, quantity, entry_price, sl_price, tp_price):
    try:
        if DRY_RUN:
            order_id = f'DRY_RUN_{int(time.time())}'
            log_message(f"🔍 DRY RUN: Would place {side.upper()} {quantity} {symbol} @ ${entry_price}")
            
            # Still track in active orders for monitoring
            with data_lock:
                active_orders[order_id] = {
                    'symbol': symbol,
                    'side': side,
                    'quantity': quantity,
                    'entry_price': entry_price,
                    'sl_price': sl_price,
                    'tp_price': tp_price,
                    'timestamp': datetime.now(),
                    'status': 'dry_run',
                    'filled_quantity': quantity
                }
            
            return {
                'id': order_id,
                'status': 'dry_run',
                'symbol': symbol,
                'side': side,
                'price': entry_price,
                'amount': quantity
            }
        
        # Calculate limit price with slippage
        if side == 'buy':
            limit_price = entry_price * (1 + SLIPPAGE_PERCENT / 100)
        else:
            limit_price = entry_price * (1 - SLIPPAGE_PERCENT / 100)
        
        # Round price to exchange precision
        markets = exchange.load_markets()
        market = markets.get(symbol.upper())
        if market:
            price_precision = market.get('precision', {}).get('price')
            if price_precision:
                limit_price = round(limit_price, price_precision)
        
        # Place limit order
        order = exchange.create_limit_order(
            symbol=symbol.upper(),
            side=side,
            amount=quantity,
            price=limit_price
        )
        
        log_message(f"✅ Order placed: {order['id']} | {side.upper()} {quantity} {symbol} @ ${limit_price}")
        
        # Store order info for SL/TP management
        with data_lock:
            active_orders[order['id']] = {
                'symbol': symbol,
                'side': side,
                'quantity': quantity,
                'entry_price': limit_price,
                'sl_price': sl_price,
                'tp_price': tp_price,
                'timestamp': datetime.now(),
                'status': 'open',
                'filled_quantity': 0
            }
        
        # Send Telegram notification
        msg = f"🚀 <b>Order Placed</b>\n"
        msg += f"Symbol: {symbol.upper()}\n"
        msg += f"Side: {side.upper()}\n"
        msg += f"Quantity: {quantity}\n"
        msg += f"Price: ${limit_price:.4f}\n"
        msg += f"SL: ${sl_price:.4f}\n"
        msg += f"TP: ${tp_price:.4f}"
        send_telegram(msg)
        
        return order
        
    except Exception as e:
        log_message(f"❌ Order placement error: {e}")
        send_telegram(f"❌ Order Failed: {str(e)}")
        raise  # Re-raise for retry mechanism

# ============= CLOSE POSITION =============
@retry_on_failure(max_retries=3, delay=2)
def close_position(order_id, order_info, reason):
    try:
        symbol = order_info['symbol']
        side = order_info['side']
        quantity = order_info.get('filled_quantity', order_info['quantity'])
        entry_price = order_info['entry_price']
        
        current_price = get_current_price(symbol.upper())
        if not current_price:
            log_message(f"⚠️ Could not get price for {symbol}, skipping close")
            return False
        
        # Determine close side
        close_side = 'sell' if side == 'buy' else 'buy'
        
        if DRY_RUN:
            log_message(f"🔍 DRY RUN: Would close {close_side.upper()} {quantity} {symbol} @ ${current_price}")
        else:
            # Place market order to close
            close_order = exchange.create_market_order(
                symbol=symbol.upper(),
                side=close_side,
                amount=quantity
            )
            log_message(f"✅ Position closed: {close_order['id']}")
        
        # Calculate P&L
        if side == 'buy':
            pnl = (current_price - entry_price) * quantity
        else:
            pnl = (entry_price - current_price) * quantity
        
        # Update global stats
        global daily_pnl_usdt, winning_trades_today, losing_trades_today
        with data_lock:
            daily_pnl_usdt += pnl
            if pnl > 0:
                winning_trades_today += 1
            else:
                losing_trades_today += 1
        
        log_message(f"🔔 Position closed: {reason} | P&L: ${pnl:.2f}")
        
        # Telegram notification
        emoji = "✅" if pnl > 0 else "❌"
        msg = f"{emoji} <b>Position Closed</b>\n"
        msg += f"Reason: {reason}\n"
        msg += f"P&L: ${pnl:.2f}\n"
        msg += f"Symbol: {symbol.upper()}\n"
        msg += f"Entry: ${entry_price:.4f}\n"
        msg += f"Exit: ${current_price:.4f}"
        send_telegram(msg)
        
        return True
        
    except Exception as e:
        log_message(f"❌ Position close error: {e}")
        raise

# ============= CHECK ORDER TIMEOUT =============
def check_order_timeout(order_id, order_info):
    """Cancel orders that haven't filled within timeout period"""
    try:
        if DRY_RUN:
            return False
        
        order_time = order_info['timestamp']
        time_elapsed = datetime.now() - order_time
        
        if time_elapsed > timedelta(minutes=ORDER_TIMEOUT_MINUTES):
            # Check if order is still open
            try:
                order_status = exchange.fetch_order(order_id, order_info['symbol'].upper())
                if order_status['status'] == 'open':
                    # Cancel the order
                    exchange.cancel_order(order_id, order_info['symbol'].upper())
                    log_message(f"⏱️ Order timeout cancelled: {order_id}")
                    return True
            except Exception as e:
                log_message(f"⚠️ Timeout check error for {order_id}: {e}")
        
        return False
        
    except Exception as e:
        log_message(f"❌ Timeout check error: {e}")
        return False

# ============= MONITOR ORDERS (SL/TP Management) =============
def monitor_active_orders():
    """Monitor active orders for SL/TP conditions"""
    try:
        with data_lock:
            orders_to_monitor = list(active_orders.items())
        
        for order_id, order_info in orders_to_monitor:
            try:
                symbol = order_info['symbol']
                
                # Check for timeout
                if check_order_timeout(order_id, order_info):
                    with data_lock:
                        if order_id in active_orders:
                            del active_orders[order_id]
                    continue
                
                # Get current price
                current_price = get_current_price(symbol.upper())
                if not current_price:
                    continue
                
                # Check if order is filled (skip for dry run)
                if not DRY_RUN and order_info.get('status') != 'filled':
                    try:
                        order_status = exchange.fetch_order(order_id, symbol.upper())
                        if order_status['status'] == 'closed' or order_status['status'] == 'filled':
                            with data_lock:
                                active_orders[order_id]['status'] = 'filled'
                                active_orders[order_id]['filled_quantity'] = float(order_status.get('filled', order_info['quantity']))
                        else:
                            continue  # Order not filled yet
                    except Exception as e:
                        log_message(f"⚠️ Order status check failed for {order_id}: {e}")
                        continue
                
                # Check SL/TP conditions
                entry_price = order_info['entry_price']
                sl_price = order_info['sl_price']
                tp_price = order_info['tp_price']
                side = order_info['side']
                
                should_close = False
                close_reason = ""
                
                if side == 'buy':
                    # Long position
                    if current_price <= sl_price:
                        should_close = True
                        close_reason = "Stop Loss Hit"
                    elif current_price >= tp_price:
                        should_close = True
                        close_reason = "Take Profit Hit"
                else:
                    # Short position
                    if current_price >= sl_price:
                        should_close = True
                        close_reason = "Stop Loss Hit"
                    elif current_price <= tp_price:
                        should_close = True
                        close_reason = "Take Profit Hit"
                
                # Close position if needed
                if should_close:
                    if close_position(order_id, order_info, close_reason):
                        with data_lock:
                            if order_id in active_orders:
                                del active_orders[order_id]
                
            except Exception as e:
                log_message(f"❌ Error monitoring order {order_id}: {e}")
                continue
                
    except Exception as e:
        log_message(f"❌ Order monitoring error: {e}")

# ============= WEBHOOK ENDPOINT =============
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        log_message("\n" + "="*80)
        log_message(f"📨 ALERT RECEIVED | {datetime.now()}")
        log_message(json.dumps(data, indent=2))
        log_message("="*80)
        
        # Safety checks
        is_safe, msg = check_safety_limits(data)
        if not is_safe:
            log_message(msg)
            return jsonify({"status": "rejected", "reason": msg}), 400
        
        # Extract data
        action = data.get('action', '').upper()
        tv_symbol = data.get('symbol', '')
        price = float(data.get('price', 0))
        sl = float(data.get('sl', 0))
        tp = float(data.get('tp', 0))
        
        # Map symbol to WazirX format
        symbol = SYMBOL_MAP.get(tv_symbol, tv_symbol)
        if not symbol.endswith('/USDT'):
            symbol = f"{symbol}/USDT"
        
        # Validate
        if action not in ['BUY', 'SELL']:
            return jsonify({"status": "error", "reason": "Invalid action"}), 400
        
        if price <= 0:
            return jsonify({"status": "error", "reason": "Invalid price"}), 400
        
        # Use defaults if SL/TP not provided
        if sl <= 0:
            sl = price * (1 - DEFAULT_SL_PERCENT / 100) if action == 'BUY' else price * (1 + DEFAULT_SL_PERCENT / 100)
        
        if tp <= 0:
            tp = price * (1 + DEFAULT_TP_PERCENT / 100) if action == 'BUY' else price * (1 - DEFAULT_TP_PERCENT / 100)
        
        # Calculate position size
        side = 'buy' if action == 'BUY' else 'sell'
        quantity, qty_msg = calculate_position_size(symbol, price, sl)
        
        if quantity <= 0:
            return jsonify({"status": "error", "reason": f"Position size error: {qty_msg}"}), 400
        
        # Place order
        order = place_order(symbol, side, quantity, price, sl, tp)
        
        if order:
            global total_trades_today
            with data_lock:
                total_trades_today += 1
            
            return jsonify({
                "status": "success",
                "order_id": order.get('id'),
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "entry_price": price,
                "sl": sl,
                "tp": tp,
                "trades_today": total_trades_today
            }), 200
        else:
            return jsonify({"status": "error", "reason": "Order placement failed"}), 500
        
    except Exception as e:
        log_message(f"❌ Webhook error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ============= HEALTH CHECK =============
@app.route('/health', methods=['GET'])
def health():
    try:
        balance = get_balance()
        
        with data_lock:
            response_data = {
                "status": "running",
                "exchange": "WazirX",
                "balance_usdt": balance['usdt_free'],
                "daily_pnl_usdt": round(daily_pnl_usdt, 2),
                "trades_today": total_trades_today,
                "winning_trades": winning_trades_today,
                "losing_trades": losing_trades_today,
                "active_orders": len(active_orders),
                "max_positions": MAX_OPEN_POSITIONS,
                "trading_enabled": TRADING_ENABLED,
                "dry_run": DRY_RUN,
                "time": str(datetime.now())
            }
        
        return jsonify(response_data), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ============= GET POSITIONS =============
@app.route('/positions', methods=['GET'])
def get_positions():
    with data_lock:
        positions_data = {
            "active_orders": len(active_orders),
            "max_positions": MAX_OPEN_POSITIONS,
            "orders": []
        }
        
        for order_id, order_info in active_orders.items():
            positions_data["orders"].append({
                "order_id": order_id,
                "symbol": order_info['symbol'],
                "side": order_info['side'],
                "quantity": order_info['quantity'],
                "entry_price": order_info['entry_price'],
                "sl_price": order_info['sl_price'],
                "tp_price": order_info['tp_price'],
                "status": order_info.get('status', 'unknown'),
                "timestamp": str(order_info['timestamp'])
            })
    
    return jsonify(positions_data), 200

# ============= CLOSE ALL POSITIONS (EMERGENCY) =============
@app.route('/close_all', methods=['POST'])
def close_all_positions():
    """Emergency endpoint to close all positions"""
    try:
        with data_lock:
            orders_to_close = list(active_orders.items())
        
        closed_count = 0
        for order_id, order_info in orders_to_close:
            try:
                if close_position(order_id, order_info, "Manual Close All"):
                    closed_count += 1
                    with data_lock:
                        if order_id in active_orders:
                            del active_orders[order_id]
            except Exception as e:
                log_message(f"❌ Failed to close {order_id}: {e}")
        
        return jsonify({
            "status": "success",
            "closed_positions": closed_count,
            "message": f"Closed {closed_count} positions"
        }), 200
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ============= BACKGROUND ORDER MONITOR =============
def start_order_monitor():
    def monitor_loop():
        while True:
            try:
                monitor_active_orders()
                time.sleep(ORDER_CHECK_INTERVAL_SECONDS)
            except Exception as e:
                log_message(f"❌ Monitor loop error: {e}")
                time.sleep(10)
    
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    log_message("✅ Order monitor thread started")

# ============= MAIN =============
if __name__ == '__main__':
    log_message("\n" + "="*80)
    log_message("🚀 WAZIRX ICT TRADING BOT STARTING...")
    log_message(f"Trading Enabled: {TRADING_ENABLED}")
    log_message(f"Dry Run: {DRY_RUN}")
    log_message(f"Max Positions: {MAX_OPEN_POSITIONS}")
    log_message(f"Risk Per Trade: {RISK_PER_TRADE_PERCENT}%")
    log_message(f"Max Daily Loss: ${MAX_DAILY_LOSS_USDT}")
    log_message(f"Allowed Symbols: {len(ALLOWED_SYMBOLS)}")
    log_message("="*80 + "\n")
    
    # Verify exchange connection
    try:
        balance = get_balance()
        log_message(f"✅ Exchange connected | Balance: ${balance['usdt_free']:.2f} USDT")
    except Exception as e:
        log_message(f"❌ Exchange connection failed: {e}")
    
    # Start order monitoring
    start_order_monitor()
    
    # Send startup notification
    send_telegram("🚀 <b>Trading Bot Started</b>\n\nBot is now monitoring for signals.")
    
    # Start Flask server
    app.run(host='0.0.0.0', port=5000, debug=False)