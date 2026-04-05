import os
import re
import json
import uuid
import time
import asyncio
import logging
import threading
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, Defaults, MessageHandler, CallbackQueryHandler, filters

# استيراد الوظائف المساعدة من الملفات الموجودة
from features.delivery_zones import get_delivery_price, get_matching_zone_name

# --- إعدادات أساسية ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# الاتصال بقاعدة البيانات (PostgreSQL)
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    return None

def init_db():
    conn = get_db_connection()
    if conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    phone_number TEXT,
                    products TEXT[],
                    places_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pricing (
                    order_id TEXT REFERENCES orders(id) ON DELETE CASCADE,
                    product TEXT,
                    buy NUMERIC,
                    sell NUMERIC,
                    prepared_by TEXT,
                    PRIMARY KEY (order_id, product)
                )
            """)
            cur.execute("CREATE TABLE IF NOT EXISTS invoice_counter (count INTEGER)")
            cur.execute("SELECT count FROM invoice_counter")
            if not cur.fetchone(): cur.execute("INSERT INTO invoice_counter VALUES (1)")
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully.")

init_db()

# --- وظائف المساعدة للتحليل الذكي ---
def _extract_phone_from_text(text):
    raw = re.sub(r"[\s\-]", "", text)
    m = re.search(r"(?:\+?964|0)?7\d{9}", raw)
    if m:
        digits = re.sub(r"\D", "", m.group(0))
        if digits.startswith("964"): digits = digits[3:]
        if digits.startswith("7") and len(digits) >= 10: return "0" + digits[:10]
        if digits.startswith("0") and len(digits) >= 11: return digits[:11]
    return "مطلوب"

def parse_bulk_order(raw_text):
    lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
    phone = _extract_phone_from_text(raw_text)
    try:
        zone = get_matching_zone_name(raw_text)
    except:
        zone = None
    title = zone if zone else (lines[0] if lines else "عنوان غير معروف")

    products = []
    # تنظيف الرقم للمقارنة
    clean_phone = re.sub(r"\D", "", phone) if phone != "مطلوب" else ""

    for line in lines:
        line_clean = re.sub(r"\D", "", line)
        # إذا السطر يحتوي على الرقم، نتجاهله ولا نعتبره منتجاً
        if clean_phone and clean_phone in line_clean and len(line_clean) >= 9: continue
        if title and title == line: continue
        if zone and zone in line: continue
        if len(line) < 2: continue
        products.append(line)
    return title, phone, products

# --- وظائف إدارة البيانات (قاعدة البيانات) ---
def fetch_all_data_db():
    conn = get_db_connection()
    if not conn: return {}, {}, {}
    orders_dict = {}
    pricing_dict = {}
    invoice_dict = {}

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM orders ORDER BY created_at DESC")
        rows = cur.fetchall()
        for i, r in enumerate(rows):
            oid = r['id']
            orders_dict[oid] = {
                "id": oid,
                "title": r['title'],
                "phone_number": r['phone_number'],
                "products": r['products'],
                "places_count": r['places_count'],
                "created_at": r['created_at'].isoformat()
            }
            invoice_dict[oid] = i + 1

        cur.execute("SELECT * FROM pricing")
        rows_p = cur.fetchall()
        for rp in rows_p:
            oid = rp['order_id']
            if oid not in pricing_dict: pricing_dict[oid] = {}
            pricing_dict[oid][rp['product']] = {"buy": float(rp['buy']), "sell": float(rp['sell']), "prepared_by": rp['prepared_by']}

    conn.close()
    return orders_dict, pricing_dict, invoice_dict

# --- Flask Web Server ---
app = Flask(__name__)
CORS(app)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/orders')
def get_orders():
    o, p, inv = fetch_all_data_db()
    from features.product_categories import is_meat, is_fish, is_vegetable_fruit
    from features.fixed_prices import suggest_fixed_prices
    
    categories = {}
    suggested_pricing = {}
    
    for oid, order in o.items():
        categories[oid] = {}
        suggested_pricing[oid] = {}
        
        for prod in order['products']:
             cat = "unknown"
             if is_meat(prod): cat = "meat"
             elif is_fish(prod): cat = "fish"
             elif is_vegetable_fruit(prod): cat = "veg"
             categories[oid][prod] = cat
             
             fixed = suggest_fixed_prices(prod)
             if fixed:
                 suggested_pricing[oid][prod] = {"buy": fixed['buy_total'], "sell": fixed['sell_total']}
             
    return jsonify({"orders": o, "pricing": p, "invoice_numbers": inv, "categories": categories, "suggested_pricing": suggested_pricing})

@app.route('/api/add_order', methods=['POST'])
def add_order():
    data = request.json
    raw_text = data.get('raw_text', '')
    confirmed_zone = data.get('confirmed_zone')
    title, phone, products = parse_bulk_order(raw_text)
    
    if not confirmed_zone:
        from features.delivery_zones import get_matching_zone_name, get_all_close_zones_from_words, get_closest_zone_names
        matched_zone = get_matching_zone_name(raw_text)
        if not matched_zone:
            suggestions = get_all_close_zones_from_words(raw_text, per_word_n=2, cutoff=0.35)
            if not suggestions and title and title != "عنوان غير معروف":
                try:
                    suggestions = get_closest_zone_names(title, n=6, cutoff=0.2)
                except Exception:
                    pass
            unique_suggestions = list(dict.fromkeys(suggestions))[:6]
            return jsonify({
                "status": "needs_zone",
                "suggestions": unique_suggestions,
                "parsed_data": {
                    "fallback_title": title
                }
            })
        title = matched_zone
    else:
        title = confirmed_zone

    oid = str(uuid.uuid4())[:8]
    conn = get_db_connection()
    if conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO orders (id, title, phone_number, products) VALUES (%s, %s, %s, %s)", (oid, title, phone, products))
        conn.commit()
        conn.close()
    return jsonify({"status": "success"})

@app.route('/api/update_price', methods=['POST'])
def update_price():
    data = request.json
    oid, prod, buy, sell = data['order_id'], data['product'], data['buy'], data['sell']
    conn = get_db_connection()
    if conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pricing (order_id, product, buy, sell, prepared_by)
                VALUES (%s, %s, %s, %s, 'الموقع')
                ON CONFLICT (order_id, product) DO UPDATE SET buy = %s, sell = %s
            """, (oid, prod, buy, sell, buy, sell))
        conn.commit()
        conn.close()
    return jsonify({"status": "success"})

@app.route('/api/finalize', methods=['POST'])
def finalize():
    data = request.json
    conn = get_db_connection()
    if conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET places_count = %s WHERE id = %s", (data['places_count'], data['order_id']))
        conn.commit()
        conn.close()
    return jsonify({"status": "success"})

@app.route('/api/get_invoice/<oid>')
def get_invoice(oid):
    o, p, inv = fetch_all_data_db()
    if oid not in o:
        return jsonify({"error": "Order not found"})
    order = o[oid]
    pricing = p.get(oid, {})
    invoice_num = inv.get(oid, "??")
    
    title = order['title']
    phone = order['phone_number']
    places_count = order['places_count']
    products = order['products']
    
    from features.delivery_zones import get_delivery_price
    del_price = get_delivery_price(title)
    
    prep_fee = 0
    if places_count == 2: prep_fee = 1
    elif places_count == 3: prep_fee = 2
    elif places_count >= 4: prep_fee = 3
    
    text = f"📋 أبو الأكبر للتوصيل 🚀\n-----------------------------------\n 🔢: #{invoice_num}\n🏠  : {title}\n📞   : {phone}\n\nتفاصيل الطلب :  "
    
    total = 0
    for prod in products:
        pData = pricing.get(prod)
        if pData and pData.get('sell'):
            sell = float(pData['sell'])
            sell_str = int(sell) if sell.is_integer() else sell
            old_total = total
            total += sell
            text += f"\n\n– {prod} بـ{sell_str}"
            if old_total == 0:
                text += f"\n• {int(total) if float(total).is_integer() else total} 💵"
            else:
                old_str = int(old_total) if float(old_total).is_integer() else old_total
                tot_str = int(total) if float(total).is_integer() else total
                text += f"\n• {old_str}+{sell_str}= {tot_str} 💵"
        else:
            text += f"\n\n– {prod} (بدون سعر)"
                
    if prep_fee > 0:
        old_total = total
        total += prep_fee
        old_str = int(old_total) if float(old_total).is_integer() else old_total
        tot_str = int(total) if float(total).is_integer() else total
        text += f"\n\n– 📦 التجهيز: من {places_count} محلات بـ {prep_fee}"
        text += f"\n• {old_str}+{prep_fee}= {tot_str} 💵"
        
    old_total = total
    total += del_price
    old_str = int(old_total) if float(old_total).is_integer() else old_total
    tot_str = int(total) if float(total).is_integer() else total
    text += f"\n\n– 🚚 : بـ {int(del_price) if float(del_price).is_integer() else del_price}"
    text += f"\n• {old_str}+{int(del_price) if float(del_price).is_integer() else del_price}= {tot_str} 💵"
    
    text += f"\n-----------------------------------\n✨ المجموع الكلي: ✨\nبدون التوصيل = {int(old_total) if float(old_total).is_integer() else old_total} 💵\nمــــع التوصيل = {tot_str} 💵\nشكراً لاختياركم أبو الأكبر للتوصيل! ❤️"
    
    return jsonify({"invoice_text": text})

@app.route('/api/reset', methods=['POST'])
def reset_data():
    conn = get_db_connection()
    if conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM orders")
        conn.commit()
        conn.close()
    return jsonify({"status": "success"})

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if TOKEN:
        bot = ApplicationBuilder().token(TOKEN).build()
        bot.run_polling()
    else:
        while True: time.sleep(10)
