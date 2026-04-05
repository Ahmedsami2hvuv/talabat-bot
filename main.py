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

# استيراد الوظائف المساعدة من الملفات الموجودة
from features.delivery_zones import get_delivery_price, get_matching_zone_name
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

# المتغيرات العالمية
orders = {}
pricing = {}
invoice_numbers = {}
save_lock = threading.Lock()

# --- وظائف المساعدة للتحليل الذكي ---
def _extract_phone_from_text(text):
    raw = re.sub(r"[\s\-]", "", text)
    m = re.search(r"(?:\+?964)?0?7\d{9}", raw)
    if m:
        digits = re.sub(r"\D", "", m.group(0))
        if digits.startswith("964"): digits = digits[3:]
        if digits.startswith("7") and len(digits) >= 10: return "0" + digits[:10]
        if digits.startswith("0") and len(digits) >= 11: return digits[:11]
    return "مطلوب"

def parse_bulk_order(raw_text):
    lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
    phone = _extract_phone_from_text(raw_text)

    # محاولة جلب المنطقة من النص
    try:
        zone = get_matching_zone_name(raw_text)
    except:
        zone = None

    title = zone if zone else (lines[0] if lines else "عنوان غير معروف")

    # استخراج المنتجات (استبعاد سطر الرقم وسطر المنطقة)
    products = []
    for line in lines:
        if phone != "مطلوب" and phone in line.replace(" ", ""): continue
        if zone and zone in line: continue
        if len(line) < 2: continue
        products.append(line)

    return title, phone, products

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

# --- Flask Web Server ---
app = Flask(__name__)
CORS(app)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/orders', methods=['GET'])
def get_orders():
    return jsonify({"orders": orders, "pricing": pricing, "invoice_numbers": invoice_numbers})

@app.route('/api/add_order', methods=['POST'])
def add_order():
    data = request.json
    raw_text = data.get('raw_text', '')

    title, phone, products = parse_bulk_order(raw_text)

    if not products:
        return jsonify({"status": "error", "message": "لم يتم العثور على منتجات في النص"}), 400

    oid = str(uuid.uuid4())[:8]
    orders[oid] = {
        "title": title,
        "phone_number": phone,
        "products": products,
        "places_count": 0,
        "created_at": datetime.now().isoformat()
    }
    pricing[oid] = {p: {} for p in products}
    invoice_numbers[oid] = get_next_invoice()
    save_all_data()
    return jsonify({"status": "success", "order_id": oid, "parsed": {"title": title, "phone": phone, "products": products}})

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

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
def main():
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    if TOKEN:
        bot_app = ApplicationBuilder().token(TOKEN).build()
        logger.info("Bot started...")
        bot_app.run_polling()
    else:
        while True: time.sleep(10)

if __name__ == "__main__":
    main()
