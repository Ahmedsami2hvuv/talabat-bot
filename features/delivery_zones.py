import json
import os
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

# ✅ إعداد الـ logging لهذا الملف (للتتبع)
logger = logging.getLogger(__name__)

# ✅ تعريف مسار الملف المحلي للمناطق
# بما أن الملف هو data/delivery_zones.json، سنبني المسار إليه
# os.path.dirname(__file__) يعطي مسار الملف الحالي (features)
# os.path.join() يجمع المسارات بشكل صحيح
CURRENT_DIR = os.path.dirname(__file__)
PARENT_DIR = os.path.dirname(CURRENT_DIR) # هذا يرجع للمجلد الرئيسي (اللي بيه data folder)
DELIVERY_ZONES_FILE_PATH = os.path.join(PARENT_DIR, "data", "delivery_zones.json")


# دالة لتحميل بيانات المناطق من الملف المحلي.
def load_zones():
    logger.info(f"Attempting to load zones from local file: {DELIVERY_ZONES_FILE_PATH}")
    try:
        # ✅ التأكد من وجود الملف قبل محاولة فتحه
        if not os.path.exists(DELIVERY_ZONES_FILE_PATH):
            logger.error(f"Zones file not found at: {DELIVERY_ZONES_FILE_PATH}")
            return {} # ارجع قاموس فارغ إذا الملف ما موجود

        with open(DELIVERY_ZONES_FILE_PATH, "r", encoding="utf-8") as f:
            zones_data = json.load(f) # قراءة الملف JSON
        
        logger.info(f"Successfully loaded zones from local file. Found {len(zones_data)} zones.")
        return zones_data
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON from local zones file: {e}. File path: {DELIVERY_ZONES_FILE_PATH}")
        return {} # ارجع قاموس فارغ إذا كان الملف JSON فيه خطأ
    except Exception as e:
        logger.error(f"An unexpected error occurred while loading zones from local file: {e}", exc_info=True)
        return {} # ارجع قاموس فارغ لأي خطأ آخر

# دالة لعرض قائمة المناطق الحالية.
async def list_zones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    zones = load_zones() # تحميل المناطق الحالية من الملف المحلي

    if not zones:
        text = "لا توجد مناطق مسجلة حالياً."
    else:
        text = "📍 المناطق الحالية وسعر التوصيل:\n\n"
        for name, price in zones.items():
            text += f"▫️ {name} — {price} دينار\n"
            
    # لا توجد أزرار إدارة (إضافة/حذف) هنا، لأن التعديل سيكون يدوياً على GitHub.
    reply_markup = None 

    if update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"Failed to edit message in list_zones (callback query), sending new one. Error: {e}")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)

# دالة لاستخراج سعر التوصيل من سطر عنوان الطلب.
def get_delivery_price(order_title_line):
    zones = load_zones() # تحميل المناطق الحالية من الملف المحلي
    # هنا لازم نتأكد من مطابقة المنطقة، الأفضل نسويها بأكثر دقة
    # ممكن يكون اكو جزء من اسم منطقة موجود بمنطقة ثانية (مثلاً: "بغداد الجديدة" و "الجديدة")
    # لازم ندور على الأطول أول
    
    # تحويل مفاتيح القاموس إلى قائمة وترتيبها تنازلياً حسب الطول
    sorted_zone_names = sorted(zones.keys(), key=len, reverse=True)

    for zone_name in sorted_zone_names:
        # التأكد إن المنطقة موجودة ككلمة كاملة أو جزء من العنوان بشكل منطقي
        # يعني "الاسمدة" لازم تكون "الاسمدة" مو "الاسمدة والمستلزمات"
        # أبسط طريقة هي التأكد إن الكلمة موجودة
        if zone_name in order_title_line:
            logger.info(f"Found delivery zone '{zone_name}' in title '{order_title_line}' with price {zones[zone_name]}.")
            return zones[zone_name]
    
    logger.info(f"No matching delivery zone found in title '{order_title_line}'. Returning 0.")
    return 0

# الدوال الخاصة بإدارة المناطق من البوت (ask_zone_name, handle_zone_edit, add_zones_bulk)
# لا داعي لوجودها بما أن التعديل سيتم يدويا على GitHub.
# يفضل حذفها من main.py أيضاً.
