import os
import re
import json
import uuid
import time
import asyncio
import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, Defaults, MessageHandler, CallbackQueryHandler, filters

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS

# استيراد الوظائف المساعدة
from features.delivery_zones import get_delivery_price
from features.product_categories import is_fish, is_vegetable_fruit, is_meat

# --- إعدادات أساسية ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = "data/"
os.makedirs(DATA_DIR, exist_ok=True)

ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")
PRICING_FILE = os.path.join(DATA_DIR, "pricing.json")
INVOICE_NUMBERS_FILE = os.path.join(DATA_DIR, "invoice_numbers.json")
COUNTER_FILE = os.path.join(DATA_DIR, "invoice_counter.txt")

# المتغيرات العالمية (Shared Memory)
orders = {}
pricing = {}
invoice_numbers = {}
daily_profit = 0.0
save_lock = threading.Lock()

# --- وظائف إدارة البيانات ---
def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except: return default
    return default

def save_all_data():
    with save_lock:
        with open(ORDERS_FILE, "w", encoding="utf-8") as f: json.dump(orders, f, indent=4, ensure_ascii=False)
        with open(PRICING_FILE, "w", encoding="utf-8") as f: json.dump(pricing, f, indent=4, ensure_ascii=False)
        with open(INVOICE_NUMBERS_FILE, "w", encoding="utf-8") as f: json.dump(invoice_numbers, f, indent=4, ensure_ascii=False)

def get_next_invoice():
    if not os.path.exists(COUNTER_FILE):
        with open(COUNTER_FILE, "w") as f: f.write("1")
    with open(COUNTER_FILE, "r") as f:
        curr = int(f.read().strip())
    with open(COUNTER_FILE, "w") as f:
        f.write(str(curr + 1))
    return curr

# تحميل البيانات عند البدء
orders = load_json(ORDERS_FILE, {})
pricing = load_json(PRICING_FILE, {})
invoice_numbers = load_json(INVOICE_NUMBERS_FILE, {})

# --- Flask Web Server (الموقع المتكامل) ---
app = Flask(__name__)
CORS(app)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def status():
    return jsonify({
        "order_count": len(orders),
        "total_profit": sum((p.get('sell', 0) - p.get('buy', 0)) for o in pricing.values() for p in o.values())
    })

@app.route('/api/orders', methods=['GET'])
def get_orders():
    return jsonify({"orders": orders, "pricing": pricing, "invoice_numbers": invoice_numbers})

@app.route('/api/add_order', methods=['POST'])
def add_order():
    data = request.json
    oid = str(uuid.uuid4())[:8]
    orders[oid] = {
        "title": data['title'],
        "phone_number": data['phone'],
        "products": data['products'],
        "places_count": 0,
        "created_at": datetime.now().isoformat()
    }
    pricing[oid] = {p: {} for p in data['products']}
    invoice_numbers[oid] = get_next_invoice()
    save_all_data()
    return jsonify({"status": "success", "order_id": oid})

@app.route('/api/update_price', methods=['POST'])
def update_price():
    data = request.json
    oid = data['order_id']
    prod = data['product']
    pricing[oid][prod] = {
        "buy": float(data['buy']),
        "sell": float(data['sell']),
        "prepared_by_name": "الموقع"
    }
    save_all_data()
    return jsonify({"status": "success"})

@app.route('/api/finalize', methods=['POST'])
def finalize():
    data = request.json
    oid = data['order_id']
    orders[oid]['places_count'] = int(data['places_count'])
    # هنا يمكن إضافة منطق إرسال الفاتورة النهائية
    save_all_data()
    return jsonify({"status": "success"})

@app.route('/api/reset', methods=['POST'])
def reset_data():
    global orders, pricing, invoice_numbers
    orders.clear()
    pricing.clear()
    invoice_numbers.clear()
    with open(COUNTER_FILE, "w") as f: f.write("1")
    save_all_data()
    return jsonify({"status": "success"})

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# --- Telegram Bot (سيبقى كخيار احتياطي) ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("أهلاً بك! يمكنك إدارة الطلبات بالكامل من الموقع الآن.")

def main():
    # تشغيل الموقع في الخلفية
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # تشغيل البوت إذا كان التوكن موجوداً
    if TOKEN:
        bot_app = ApplicationBuilder().token(TOKEN).build()
        bot_app.add_handler(CommandHandler("start", start))
        logger.info("Bot started...")
        bot_app.run_polling()
    else:
        logger.warning("No TOKEN found. Bot disabled, only Web Server is running.")
        while True: time.sleep(10)

if __name__ == "__main__":
    main()
