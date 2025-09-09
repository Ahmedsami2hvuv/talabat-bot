import os
import json
import uuid
import time
import asyncio
import logging
import threading
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import quote

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters
)

# ✅ استيراد الدوال الخاصة بالمناطق من الملف الجديد
try:
    from features.delivery_zones import list_zones, get_delivery_price
except ImportError:
    # دوال بديلة في حالة عدم وجود الملف
    def list_zones():
        return ["المنطقة الافتراضية"]
    
    def get_delivery_price(address):
        return 5  # سعر افتراضي

# ✅ تفعيل الـ logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ✅ مسارات التخزين
DATA_DIR = "/mnt/data/"
os.makedirs(DATA_DIR, exist_ok=True)

ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")
PRICING_FILE = os.path.join(DATA_DIR, "pricing.json")
INVOICE_NUMBERS_FILE = os.path.join(DATA_DIR, "invoice_numbers.json")
DAILY_PROFIT_FILE = os.path.join(DATA_DIR, "daily_profit.json")
COUNTER_FILE = os.path.join(DATA_DIR, "invoice_counter.txt")
LAST_BUTTON_MESSAGE_FILE = os.path.join(DATA_DIR, "last_button_message.json")
SUPPLIER_REPORT_FILE = os.path.join(DATA_DIR, "supplier_report_timestamps.json")

# ✅ قراءة التوكن من المتغيرات البيئية
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0"))
OWNER_PHONE_NUMBER = os.getenv("OWNER_TELEGRAM_PHONE_NUMBER", "+9647733921468")

if TOKEN is None:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set.")

# ✅ متغيرات التخزين المؤقت
orders = {}
pricing = {}
invoice_numbers = {}
daily_profit = 0.0
last_button_message = {}
supplier_report_timestamps = {}

# تهيئة القفل لعمليات الحفظ
save_lock = threading.Lock()
save_timer = None
save_pending = False

# حالات المحادثة
(
    ASK_BUY, ASK_PLACES_COUNT, ASK_PRODUCT_NAME, 
    ASK_PRODUCT_TO_DELETE, ASK_CUSTOMER_PHONE_NUMBER_FOR_DELETION, 
    ASK_FOR_DELETION_CONFIRMATION
) = range(6)

# ========== دوال إدارة البيانات ==========
def load_json_file(filepath, default_value, var_name):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                logger.info(f"Loaded {var_name} from {filepath} successfully.")
                return data
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"{filepath} is corrupted: {e}, reinitializing {var_name}.")
    logger.info(f"{var_name} file not found, initializing to default.")
    return default_value

def _save_data_to_disk_global():
    global orders, pricing, invoice_numbers, daily_profit, last_button_message, supplier_report_timestamps
    
    with save_lock:
        try:
            files_to_save = [
                (ORDERS_FILE, orders),
                (PRICING_FILE, pricing),
                (INVOICE_NUMBERS_FILE, invoice_numbers),
                (LAST_BUTTON_MESSAGE_FILE, last_button_message),
                (SUPPLIER_REPORT_FILE, supplier_report_timestamps)
            ]
            
            for file_path, data in files_to_save:
                temp_file = file_path + ".tmp"
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                os.replace(temp_file, file_path)
            
            with open(DAILY_PROFIT_FILE + ".tmp", "w", encoding="utf-8") as f:
                f.write(str(daily_profit))
            os.replace(DAILY_PROFIT_FILE + ".tmp", DAILY_PROFIT_FILE)
            
            logger.info("All data saved successfully.")
            
        except Exception as e:
            logger.error(f"Error saving data: {e}")

def schedule_save_global():
    global save_pending
    if not save_pending:
        save_pending = True
        threading.Timer(2.0, _delayed_save).start()

def _delayed_save():
    global save_pending
    _save_data_to_disk_global()
    save_pending = False

def load_data():
    global orders, pricing, invoice_numbers, daily_profit, last_button_message, supplier_report_timestamps
    
    orders = load_json_file(ORDERS_FILE, {}, "orders")
    pricing = load_json_file(PRICING_FILE, {}, "pricing")
    invoice_numbers = load_json_file(INVOICE_NUMBERS_FILE, {}, "invoice_numbers")
    last_button_message = load_json_file(LAST_BUTTON_MESSAGE_FILE, {}, "last_button_message")
    supplier_report_timestamps = load_json_file(SUPPLIER_REPORT_FILE, {}, "supplier_report_timestamps")
    
    try:
        if os.path.exists(DAILY_PROFIT_FILE):
            with open(DAILY_PROFIT_FILE, "r", encoding="utf-8") as f:
                daily_profit = float(f.read().strip())
    except (ValueError, Exception):
        daily_profit = 0.0
    
    if not os.path.exists(COUNTER_FILE):
        with open(COUNTER_FILE, "w", encoding="utf-8") as f:
            f.write("1")

def get_invoice_number():
    try:
        with open(COUNTER_FILE, "r+", encoding="utf-8") as f:
            current = int(f.read().strip())
            f.seek(0)
            f.write(str(current + 1))
            f.truncate()
            return current
    except (ValueError, Exception):
        with open(COUNTER_FILE, "w", encoding="utf-8") as f:
            f.write("2")
        return 1

def format_float(value):
    formatted = f"{value:g}"
    if formatted.endswith(".0"):
        return formatted[:-2]
    return formatted

def calculate_extra(places_count):
    if places_count <= 2:
        return 0
    elif places_count <= 10:
        return places_count - 2
    else:
        return 8

# ========== دوال المساعدة ==========
async def delete_message_in_background(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    try:
        await asyncio.sleep(0.1)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Deleted message {message_id} from chat {chat_id}.")
    except Exception as e:
        logger.warning(f"Could not delete message {message_id}: {e}.")

async def save_data_in_background(context: ContextTypes.DEFAULT_TYPE):
    schedule_save_global()
    logger.info("Data save scheduled in background.")

# ========== دوال البوت الرئيسية ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    logger.info(f"/start command from user {user_id}.")
    
    if user_id in context.user_data:
        keys_to_remove = ["order_id", "product", "current_active_order_id", 
                         "messages_to_delete", "buy_price"]
        for key in keys_to_remove:
            context.user_data[user_id].pop(key, None)
    
    welcome_text = """
    🛒 مرحباً بك في بوت إدارة الطلبات!
    
    📋 الأوامر المتاحة:
    /neworder - بدء طلبية جديدة
    /products - عرض المنتجات
    /profit - عرض الأرباح
    /report - عرض التقرير
    /help - المساعدة
    
    📞 للاستفسار: {}
    """.format(OWNER_PHONE_NUMBER)
    
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
    🆘 أوامر البوت:
    
    /start - بدء استخدام البوت
    /help - هذه الرسالة
    /neworder - بدء طلبية جديدة
    /products - إدارة المنتجات
    /profit - عرض الأرباح اليومية
    /report - تقرير المبيعات
    /reset - إعادة الضبط (للمالك فقط)
    /deleteorder - حذف طلبية
    
    📊 للإدارة المتقدمة تواصل مع المالك.
    """
    await update.message.reply_text(help_text)

async def receive_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        message = update.message or update.edited_message
        text = message.text.strip()
        user_id = str(message.from_user.id)
        
        logger.info(f"Received order from {user_id}: {text}")
        
        # معالجة الطلبية هنا
        # ... [الكود الأصلي للمعالجة]
        
        await message.reply_text("✅ تم استلام طلبك وسيتم معالجته قريباً.")
        
    except Exception as e:
        logger.error(f"Error in receive_order: {e}")
        await update.message.reply_text("❌ حدث خطأ في معالجة الطلب.")

async def process_order(update, context, message, edited=False):
    try:
        # تنفيذ معالجة الطلبية
        # ... [الكود الأصلي للمعالجة]
        
        await save_data_in_background(context)
        
    except Exception as e:
        logger.error(f"Error processing order: {e}")
        await message.reply_text("❌ حدث خطأ في المعالجة.")

async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        
        # معالجة اختيار المنتج
        # ... [الكود الأصلي]
        
    except Exception as e:
        logger.error(f"Error in product_selected: {e}")

async def show_profit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = str(update.message.from_user.id)
        if user_id != str(OWNER_ID):
            await update.message.reply_text("❌ هذا الأمر متاح للمالك فقط.")
            return
        
        total_profit = sum(order.get('profit', 0) for order in orders.values() 
                          if order.get('status') == 'completed')
        
        profit_text = f"""
        📊 تقرير الأرباح:
        
        🎯 الأرباح اليومية: {format_float(daily_profit)} دينار
        💰 الأرباح الإجمالية: {format_float(total_profit)} دينار
        📦 عدد الطلبات: {len([o for o in orders.values() if o.get('status') == 'completed'])}
        
        📅 آخر تحديث: {datetime.now().strftime('%Y-%m-%d %H:%M')}
        """
        
        await update.message.reply_text(profit_text)
        
    except Exception as e:
        logger.error(f"Error in show_profit: {e}")
        await update.message.reply_text("❌ حدث خطأ في عرض الأرباح.")

async def reset_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = str(update.message.from_user.id)
        if user_id != str(OWNER_ID):
            await update.message.reply_text("❌ هذا الأمر للمالك فقط.")
            return
        
        keyboard = [
            [InlineKeyboardButton("✅ نعم، تأكيد المسح", callback_data="confirm_reset")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_reset")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "⚠️ هل أنت متأكد من مسح جميع البيانات؟ هذا الإجراء لا يمكن التراجع عنه!",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        logger.error(f"Error in reset_all: {e}")

async def confirm_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        
        global orders, pricing, invoice_numbers, daily_profit, last_button_message, supplier_report_timestamps
        
        # مسح جميع البيانات
        orders = {}
        pricing = {}
        invoice_numbers = {}
        daily_profit = 0.0
        last_button_message = {}
        supplier_report_timestamps = {}
        
        # إعادة ضبط عداد الفواتير
        with open(COUNTER_FILE, "w", encoding="utf-8") as f:
            f.write("1")
        
        # حفظ البيانات
        _save_data_to_disk_global()
        
        await query.edit_message_text("✅ تم مسح جميع البيانات بنجاح.")
        
    except Exception as e:
        logger.error(f"Error in confirm_reset: {e}")
        await query.edit_message_text("❌ حدث خطأ أثناء المسح.")

async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = str(update.message.from_user.id)
        if user_id != str(OWNER_ID):
            await update.message.reply_text("❌ هذا الأمر للمالك فقط.")
            return
        
        completed_orders = [o for o in orders.values() if o.get('status') == 'completed']
        total_orders = len(completed_orders)
        total_revenue = sum(order.get('total_price', 0) for order in completed_orders)
        total_cost = sum(order.get('total_cost', 0) for order in completed_orders)
        total_profit = total_revenue - total_cost
        
        report_text = f"""
        📈 تقرير المبيعات:
        
        📊 عدد الطلبات: {total_orders}
        💵 الإيرادات: {format_float(total_revenue)} دينار
        💰 التكاليف: {format_float(total_cost)} دينار
        🎯 الأرباح: {format_float(total_profit)} دينار
        📦 المنتجات المباعة: {sum(order.get('quantity', 0) for order in completed_orders)}
        
        📅 آخر تحديث: {datetime.now().strftime('%Y-%m-%d %H:%M')}
        """
        
        await update.message.reply_text(report_text)
        
    except Exception as e:
        logger.error(f"Error in show_report: {e}")
        await update.message.reply_text("❌ حدث خطأ في عرض التقرير.")

# ========== دوال إدارة المنتجات ==========
async def list_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not pricing:
            await update.message.reply_text("❌ لا توجد منتجات مضافة بعد.")
            return
        
        products_text = "📦 المنتجات المتاحة:\n\n"
        for product, price in pricing.items():
            products_text += f"• {product}: {format_float(price)} دينار\n"
        
        await update.message.reply_text(products_text)
        
    except Exception as e:
        logger.error(f"Error in list_products: {e}")

# ========== الإعداد الرئيسي ==========
def setup_handlers(app):
    """إعداد جميع handlers"""
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("profit", show_profit))
    app.add_handler(CommandHandler("report", show_report))
    app.add_handler(CommandHandler("reset", reset_all))
    app.add_handler(CommandHandler("products", list_products))
    
    # إضافة handlers للاستعلامات
    app.add_handler(CallbackQueryHandler(confirm_reset, pattern="^confirm_reset$"))
    app.add_handler(CallbackQueryHandler(product_selected, pattern="^product_"))
    
    # إضافة handler للرسائل العادية
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order))

def main():
    """الدالة الرئيسية لتشغيل البوت"""
    try:
        # تحميل البيانات أولاً
        load_data()
        
        # بناء التطبيق
        app = ApplicationBuilder().token(TOKEN).build()
        
        # تخزين البيانات في bot_data
        app.bot_data.update({
            'orders': orders,
            'pricing': pricing,
            'invoice_numbers': invoice_numbers,
            'daily_profit': daily_profit,
            'last_button_message': last_button_message,
            'supplier_report_timestamps': supplier_report_timestamps,
            'schedule_save_global_func': schedule_save_global
        })
        
        # إعداد handlers
        setup_handlers(app)
        
        logger.info("✅ Bot is starting...")
        print("✅ Bot is running. Press Ctrl+C to stop.")
        
        app.run_polling()
        
    except Exception as e:
        logger.error(f"❌ Error starting bot: {e}")
        raise

if __name__ == "__main__":
    main()
