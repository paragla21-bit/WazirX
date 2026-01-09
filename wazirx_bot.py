# wazirx_bot.py
from flask import Flask, request, jsonify
import ccxt
from wazirx_config import *
from datetime import datetime, timedelta
import json
import time
import requests

app = Flask(__name__)

# ============= INITIALIZE WAZIRX =============
exchange = ccxt.wazirx({
    'apiKey': uAmqQjmmwUYwPu04T8zOXAGgwO42DjHWrtEjh1K66l0HzKUgJLPAr98ThDYX8355,
    'secret': ENmclYgpDUMfc90dHwuEOo4rjWUt5GrAoabKFtYU,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'spot',  # WazirX only supports spot trading
    }
})

# ============= DAILY TRACKING =============
daily_pnl_usdt = 0
daily_pnl_inr = 0
last_reset_date = datetime.now().date()
total_trades_today = 0
winning_trades_today = 0
losing_trades_today = 0

# ============= ACTIVE ORDERS TRACKING =============
active_orders = {}  # {order_id: {symbol, side, sl, tp, entry_price}}

def reset_daily_tracker():
    global daily_pnl_usdt, daily_pnl_inr, last_reset_date, total_trades_today
    global winning_trades_today, losing_trades_today
    
    today = datetime.now().date()
    if today != last_reset_date:
        daily_pnl_usdt = 0
        daily_pnl_inr = 0
        total_trades_today = 0
        winning_trades_today = 0
        losing_trades_today = 0
        last_reset_date = today
        log_message(f"✅ Daily tracker reset: {today}")

# ============= LOGGING =============
def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
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
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        log_message(f"❌ Telegram error: {e}")

# ============= GET CURRENT BALANCE =============
def get_balance():
    try:
        balance = exchange.fetch_balance()
        usdt_free = balance.get('USDT', {}).get('free', 0)
        usdt_total = balance.get('USDT', {}).get('total', 0)
        
        return {
            'usdt_free': usdt_free,
            'usdt_total': usdt_total
        }
    except Exception as e:
        log_message(f"❌ Balance fetch error: {e}")
        return {'usdt_free': 0, 'usdt_total': 0}

# ============= GET CURRENT PRICE =============
def get_current_price(symbol):
    try:
        ticker = exchange.fetch_ticker(symbol)
        return ticker['last']
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
    if abs(daily_pnl_usdt) >= MAX_DAILY_LOSS_USDT:
        return False, f"❌ Daily loss limit reached: ${abs(daily_pnl_usdt):.2f}"
    
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
        
        # Risk amount
        risk_amount = available_capital * (RISK_PER_TRADE_PERCENT / 100)
        risk_amount = min(risk_amount, MAX_POSITION_SIZE_USDT * 0.02)  # Max 2% risk
        
        # SL distance
        sl_distance_percent = abs(entry_price - stop_loss_price) / entry_price
        
        if sl_distance_percent <= 0:
            return 0, "Invalid SL distance"
        
        # Position size in USDT
        position_size_usdt = risk_amount / sl_distance_percent
        position_size_usdt = min(position_size_usdt, MAX_POSITION_SIZE_USDT)
        position_size_usdt = min(position_size_usdt, available_capital * 0.8)  # Max 80% of available
        
        # Convert to crypto quantity
        quantity = position_size_usdt / entry_price
        
        # Get market info for precision
        markets = exchange.load_markets()
        market = markets.get(symbol.upper())
        
        if market:
            # Round to exchange precision
            precision = market['precision']['amount']
            if precision:
                quantity = round(quantity, precision)
        
        # Minimum order size check (WazirX minimum ~$1)
        min_order_usdt = 1.0
        if quantity * entry_price < min_order_usdt:
            return 0, f"Order size too small (min ${min_order_usdt})"
        
        return quantity, "OK"
        
    except Exception as e:
        log_message(f"❌ Position size calculation error: {e}")
        return 0, str(e)

# ============= PLACE ORDER =============
def place_order(symbol, side, quantity, entry_price, sl_price, tp_price):
    try:
        if DRY_RUN:
            log_message(f"🔍 DRY RUN: Would place {side.upper()} {quantity} {symbol} @ ${entry_price}")
            return {
                'id': f'DRY_RUN_{int(time.time())}',
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
        if market and market['precision']['price']:
            price_precision = market['precision']['price']
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
        active_orders[order['id']] = {
            'symbol': symbol,
            'side': side,
            'quantity': quantity,
            'entry_price': limit_price,
            'sl_price': sl_price,
            'tp_price': tp_price,
            'timestamp': datetime.now()
        }
        
        # Send Telegram notification
        msg = f"🚀 <b>Order Placed</b>\n"
        msg += f"Symbol: {symbol.upper()}\n"
        msg += f"Side: {side.upper()}\n"
        msg += f"Quantity: {quantity}\n"
        msg += f"Price: ${limit_price:.2f}\n"
        msg += f"SL: ${sl_price:.2f}\n"
        msg += f"TP: ${tp_price:.2f}"
        send_telegram(msg)
        
        return order
        
    except Exception as e:
        log_message(f"❌ Order placement error: {e}")
        send_telegram(f"❌ Order Failed: {str(e)}")
        return None

# ============= MONITOR ORDERS (SL/TP Management) =============
def monitor_active_orders():
    """
    WazirX doesn't support native SL/TP, so we monitor manually
    """
    try:
        for order_id, order_info in list(active_orders.items()):
            symbol = order_info['symbol']
            current_price = get_current_price(symbol.upper())
            
            if not current_price:
                continue
            
            entry_price = order_info['entry_price']
            sl_price = order_info['sl_price']
            tp_price = order_info['tp_price']
            side = order_info['side']
            quantity = order_info['quantity']
            
            # Check if order is filled
            try:
                order_status = exchange.fetch_order(order_id, symbol.upper())
                if order_status['status'] != 'closed':
                    continue  # Order not filled yet
            except:
                continue
            
            # Check SL/TP conditions
            should_close = False
            close_reason = ""
            
            if side == 'buy':
                # Long position
                if current_price <= sl_price:
                    should_close = True
                    close_reason = "SL Hit"
                elif current_price >= tp_price:
                    should_close = True
                    close_reason = "TP Hit"
            else:
                # Short position
                if current_price >= sl_price:
                    should_close = True
                    close_reason = "SL Hit"
                elif current_price <= tp_price:
                    should_close = True
                    close_reason = "TP Hit"
            
            # Close position if needed
            if should_close:
                close_side = 'sell' if side == 'buy' else 'buy'
                close_order = exchange.create_market_order(
                    symbol=symbol.upper(),
                    side=close_side,
                    amount=quantity
                )
                
                # Calculate P&L
                if side == 'buy':
                    pnl = (current_price - entry_price) * quantity
                else:
                    pnl = (entry_price - current_price) * quantity
                
                global daily_pnl_usdt, winning_trades_today, losing_trades_today
                daily_pnl_usdt += pnl
                
                if pnl > 0:
                    winning_trades_today += 1
                else:
                    losing_trades_today += 1
                
                log_message(f"🔔 Position closed: {close_reason} | P&L: ${pnl:.2f}")
                
                # Telegram notification
                emoji = "✅" if pnl > 0 else "❌"
                msg = f"{emoji} <b>Position Closed</b>\n"
                msg += f"Reason: {close_reason}\n"
                msg += f"P&L: ${pnl:.2f}\n"
                msg += f"Symbol: {symbol.upper()}"
                send_telegram(msg)
                
                # Remove from active orders
                del active_orders[order_id]
                
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
        symbol = SYMBOL_MAP.get(tv_symbol, tv_symbol).lower()
        
        # Validate
        if action not in ['BUY', 'SELL']:
            return jsonify({"status": "error", "reason": "Invalid action"}), 400
        
        if price <= 0 or sl <= 0 or tp <= 0:
            return jsonify({"status": "error", "reason": "Invalid price/SL/TP"}), 400
        
        # Calculate position size
        side = 'buy' if action == 'BUY' else 'sell'
        quantity, qty_msg = calculate_position_size(symbol, price, sl)
        
        if quantity <= 0:
            return jsonify({"status": "error", "reason": f"Position size error: {qty_msg}"}), 400
        
        # Place order
        order = place_order(symbol, side, quantity, price, sl, tp)
        
        if order:
            global total_trades_today
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
        
        return jsonify({
            "status": "running",
            "exchange": "WazirX",
            "balance_usdt": balance['usdt_free'],
            "daily_pnl_usdt": daily_pnl_usdt,
            "trades_today": total_trades_today,
            "winning_trades": winning_trades_today,
            "losing_trades": losing_trades_today,
            "active_orders": len(active_orders),
            "time": str(datetime.now())
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ============= GET POSITIONS =============
@app.route('/positions', methods=['GET'])
def get_positions():
    return jsonify({
        "active_orders": len(active_orders),
        "orders": list(active_orders.values())
    }), 200

# ============= BACKGROUND ORDER MONITOR =============
def start_order_monitor():
    import threading
    
    def monitor_loop():
        while True:
            try:
                monitor_active_orders()
                time.sleep(5)  # Check every 5 seconds
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
    log_message(f"Allowed Symbols: {ALLOWED_SYMBOLS}")
    log_message("="*80 + "\n")
    
    # Start order monitoring
    start_order_monitor()
    
    # Start Flask server
    app.run(host='0.0.0.0', port=5000, debug=False)