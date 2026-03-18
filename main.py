import os
import re
import json
import uuid
import time
import asyncio
import logging
import threading
from collections import Counter
from datetime import datetime, timezone, time as dt_time
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, Defaults,
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters
)

# ✅ استيراد الدوال الخاصة بالمناطق من الملف الجديد
from features.delivery_zones import (
    list_zones, get_delivery_price, is_zone_known, get_matching_zone_name,
    get_closest_zone_name, get_closest_zone_names, get_all_close_zones_from_words,
    get_close_zones_with_words, match_text_to_suggested_zones
)
# ✅ تصنيف المنتجات (سمك، خضروات، لحم) لبناء فواتير منفصلة
from features.product_categories import is_fish, is_vegetable_fruit, is_meat

# ✅ تفعيل الـ logging للحصول على تفاصيل الأخطاء والعمليات
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ✅ مسارات التخزين داخل Railway أو Replit أو غيره
DATA_DIR = "/mnt/data/"

ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")
PRICING_FILE = os.path.join(DATA_DIR, "pricing.json")
INVOICE_NUMBERS_FILE = os.path.join(DATA_DIR, "invoice_numbers.json")
DAILY_PROFIT_FILE = os.path.join(DATA_DIR, "daily_profit.json")
COUNTER_FILE = os.path.join(DATA_DIR, "invoice_counter.txt")
LAST_BUTTON_MESSAGE_FILE = os.path.join(DATA_DIR, "last_button_message.json")

# ✅ قراءة التوكن من المتغيرات البيئية (يفترض أنك ضايفه بـ Railway)
TOKEN = os.getenv("TOKEN")

# ⏰ أوقات التقرير والتصفير التلقائي (بتوقيت UTC — السيرفر يستخدم UTC)
# الافتراضي: 4 الفجر تقارير، 6 الفجر تصفير. تقبل الساعة فقط "6" أو ساعة:دقيقة "18:2"
def _parse_hour_min(env_key: str, default_hour: str, default_min: str) -> tuple:
    raw = os.getenv(env_key, default_hour)
    s = str(raw).strip()
    if ":" in s:
        parts = s.split(":", 1)
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 and parts[1].strip() else int(default_min)
        return h, m
    h = int(s)
    m = int(os.getenv(env_key.replace("HOUR", "MINUTE"), default_min))
    return h, m

_report_h, _report_m = _parse_hour_min("REPORT_DAILY_HOUR", "10", "37")
_reset_h, _reset_m = _parse_hour_min("RESET_DAILY_HOUR", "10", "39")
REPORT_DAILY_HOUR = _report_h
REPORT_DAILY_MINUTE = _report_m
RESET_DAILY_HOUR = _reset_h
RESET_DAILY_MINUTE = _reset_m

# التوقيت المحلي للجدولة (العراق)
BOT_TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Baghdad"))

def _schedule_daily_with_catchup(app, callback, hour: int, minute: int, name: str, catchup_minutes: int = 5):
    """يشغّل run_daily، وإذا فات الوقت بفترة قصيرة (افتراضياً 5 دقايق) يشغّله مرة للتجربة."""
    if not app.job_queue:
        return
    app.job_queue.run_daily(callback, time=dt_time(hour=hour, minute=minute, tzinfo=BOT_TZ), name=name)
    now = datetime.now(BOT_TZ)
    scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta_sec = (now - scheduled_today).total_seconds()
    if 0 < delta_sec <= catchup_minutes * 60:
        # فاتت شوي: شغّلها مرة بعد ثواني
        app.job_queue.run_once(callback, when=3, name=f"{name}_catchup")
        logger.info(f"Catch-up triggered for {name} (missed by {int(delta_sec)}s).")

# ✅ متغيرات التخزين المؤقت في الذاكرة
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

# دالة تحميل JSON بشكل آمن (يمكن نقلها إلى ملف utils/data_manager لاحقاً)
def load_json_file(filepath, default_value, var_name):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            try:
                data = json.load(f)
                logger.info(f"Loaded {var_name} from {filepath} successfully.")
                return data
            except json.JSONDecodeError:
                logger.warning(f"{filepath} is corrupted or empty, reinitializing {var_name}.")
            except Exception as e:
                logger.error(f"Error loading {filepath}: {e}, reinitializing {var_name}.")
    logger.info(f"{var_name} file not found or corrupted, initializing to default.")
    return default_value

# دالة حفظ البيانات إلى القرص (يجب أن تكون عامة ويمكن الوصول إليها)
def _save_data_to_disk_global():
    # الوصول إلى المتغيرات العالمية مباشرةً
    global orders, pricing, invoice_numbers, daily_profit, last_button_message, supplier_report_timestamps # ✅ ضفنا هنا المتغير الجديد
    with save_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            with open(ORDERS_FILE + ".tmp", "w") as f:
                json.dump(orders, f, indent=4)
            os.replace(ORDERS_FILE + ".tmp", ORDERS_FILE)

            with open(PRICING_FILE + ".tmp", "w") as f:
                json.dump(pricing, f, indent=4)
            os.replace(PRICING_FILE + ".tmp", PRICING_FILE)

            with open(INVOICE_NUMBERS_FILE + ".tmp", "w") as f:
                json.dump(invoice_numbers, f, indent=4)
            os.replace(INVOICE_NUMBERS_FILE + ".tmp", INVOICE_NUMBERS_FILE)

            with open(DAILY_PROFIT_FILE + ".tmp", "w") as f:
                json.dump(daily_profit, f, indent=4)
            os.replace(DAILY_PROFIT_FILE + ".tmp", DAILY_PROFIT_FILE)

            with open(LAST_BUTTON_MESSAGE_FILE + ".tmp", "w") as f:
                json.dump(last_button_message, f, indent=4)
            os.replace(LAST_BUTTON_MESSAGE_FILE + ".tmp", LAST_BUTTON_MESSAGE_FILE)

            # ✅ هذا الكود الجديد لحفظ سجل أوقات التصفير
            with open(os.path.join(DATA_DIR, "supplier_report_timestamps.json") + ".tmp", "w") as f:
                json.dump(supplier_report_timestamps, f, indent=4)
            os.replace(os.path.join(DATA_DIR, "supplier_report_timestamps.json") + ".tmp", os.path.join(DATA_DIR, "supplier_report_timestamps.json"))

            logger.info("All data (global) saved to disk successfully.")
        except Exception as e:
            logger.error(f"Error saving global data to disk: {e}")

# دالة الحفظ المؤجل العامة
def schedule_save_global():
    global save_timer, save_pending
    if save_pending:
        logger.info("Save already pending, skipping new schedule.")
        return

    if save_timer is not None:
        save_timer.cancel()

    save_pending = True
    save_timer = threading.Timer(0.5, _save_data_to_disk_global)
    save_timer.start()
    logger.info("Global data save scheduled with 0.5 sec delay.")

# ✅ دالة تحميل البيانات عند بدء تشغيل البوت (تم تغيير موقعها)
def load_data():
    global orders, pricing, invoice_numbers, daily_profit, last_button_message, supplier_report_timestamps # ✅ ضفنا هنا المتغير الجديد

    os.makedirs(DATA_DIR, exist_ok=True)

    orders_temp = load_json_file(ORDERS_FILE, {}, "orders")
    orders.clear()
    orders.update({str(k): v for k, v in orders_temp.items()})

    pricing_temp = load_json_file(PRICING_FILE, {}, "pricing")
    pricing.clear()
    pricing.update({str(pk): pv for pk, pv in pricing_temp.items()})
    for oid in pricing:
        if isinstance(pricing[oid], dict):
            pricing[oid] = {str(pk): pv for pk, pv in pricing[oid].items()} # Ensure inner keys are strings too

    invoice_numbers_temp = load_json_file(INVOICE_NUMBERS_FILE, {}, "invoice_numbers")
    invoice_numbers.clear()
    invoice_numbers.update({str(k): v for k, v in invoice_numbers_temp.items()})

    daily_profit = load_json_file(DAILY_PROFIT_FILE, 0.0, "daily_profit")
    
    last_button_message_temp = load_json_file(LAST_BUTTON_MESSAGE_FILE, {}, "last_button_message")
    last_button_message.clear()
    last_button_message.update({str(k): v for k, v in last_button_message_temp.items()})

    # ✅ هذا السطر الجديد واللي بعده لتحميل سجل أوقات التصفير
    supplier_report_timestamps_temp = load_json_file(os.path.join(DATA_DIR, "supplier_report_timestamps.json"), {}, "supplier_report_timestamps")
    supplier_report_timestamps.clear()
    supplier_report_timestamps.update({str(k): v for k, v in supplier_report_timestamps_temp.items()})

    logger.info(f"Initial load complete. Orders: {len(orders)}, Pricing entries: {len(pricing)}, Daily Profit: {daily_profit}")

# تهيئة ملف عداد الفواتير
os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.exists(COUNTER_FILE):
    with open(COUNTER_FILE, "w") as f:
        f.write("1")

def get_invoice_number():
    with open(COUNTER_FILE, "r") as f:
        current = int(f.read().strip())
    with open(COUNTER_FILE, "w") as f:
        f.write(str(current + 1))
    return current

# ✅ استدعاء دالة load_data() هنا، بعد تعريفها
load_data()

# حالات المحادثة
ASK_BUY, ASK_PLACES_COUNT, ASK_PRODUCT_NAME, ASK_PRODUCT_TO_DELETE, ASK_CUSTOMER_PHONE_NUMBER_FOR_DELETION, ASK_FOR_DELETION_CONFIRMATION = range(6)

# جلب التوكن ومعرف المالك/المديرين من متغيرات البيئة
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
_owner_raw = (os.getenv("OWNER_TELEGRAM_ID") or "").strip()
OWNER_PHONE_NUMBER = os.getenv("OWNER_TELEGRAM_PHONE_NUMBER", "+9647733921468")

if TOKEN is None:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set.")

# دعم أكثر من مدير: أرقام مفصولة بفاصلة (مثال: 7032076289,937732530)
OWNER_IDS = set()
for x in _owner_raw.split(","):
    part = x.strip()
    if part.isdigit():
        OWNER_IDS.add(int(part))
if not OWNER_IDS:
    raise ValueError("OWNER_TELEGRAM_ID must be set (one or more numbers, comma-separated).")

# أول مدير في القائمة (للتوافق مع الأماكن التي ترسل لمدير واحد فقط إن لزم)
OWNER_ID = next(iter(OWNER_IDS))


def is_owner(user_id):
    """التحقق إذا كان المستخدم من المديرين."""
    try:
        return int(user_id) in OWNER_IDS
    except (ValueError, TypeError):
        return False

# دالة لتنسيق الأرقام العشرية
def format_float(value):
    formatted = f"{value:g}"
    if formatted.endswith(".0"):
        return formatted[:-2]
    return formatted

# دالة لحساب مبلغ الأجرة الإضافي بناءً على عدد المحلات
def calculate_extra(places_count):
    if places_count <= 2:
        return 0
    elif places_count == 3:
        return 1
    elif places_count == 4:
        return 2
    elif places_count == 5:
        return 3
    elif places_count == 6:
        return 4
    elif places_count == 7:
        return 5
    elif places_count == 8:
        return 6
    elif places_count == 9:
        return 7
    elif places_count >= 10:
        return 8
    return 0

# دالة مساعدة لحذف الرسائل في الخلفية
async def delete_message_in_background(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    try:
        await asyncio.sleep(0.1)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Successfully deleted message {message_id} from chat {chat_id} in background.")
    except Exception as e:
        logger.warning(f"Could not delete message {message_id} from chat {chat_id} in background: {e}.")

# دالة مساعدة لحفظ البيانات في الخلفية
async def save_data_in_background(context: ContextTypes.DEFAULT_TYPE):
    schedule_save_global()
    logger.info("Data save scheduled in background.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    logger.info(f"[{update.effective_chat.id}] /start command from user {user_id}. User data before clearing: {json.dumps(context.user_data.get(user_id, {}), indent=2)}")
    if user_id in context.user_data:
        context.user_data[user_id].pop("order_id", None)
        context.user_data[user_id].pop("product", None)
        context.user_data[user_id].pop("current_active_order_id", None)
        context.user_data[user_id].pop("messages_to_delete", None) 
        context.user_data[user_id].pop("buy_price", None)
        logger.info(f"Cleared order-specific user_data for user {user_id} on /start command. User data after clearing: {json.dumps(context.user_data.get(user_id, {}), indent=2)}")
    
    # ⭐⭐ زر دائم للطلبات غير المكتملة ⭐⭐
    from telegram import ReplyKeyboardMarkup
    reply_keyboard = [['الطلبات']]
    markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True, input_field_placeholder='اختر "الطلبات"')
    
    await update.message.reply_text(
        "أهلاً بك يا أبا الأكبر! لإعداد طلبية، دز الطلبية كلها برسالة واحدة.\n\n*السطر الأول:* عنوان الزبون.\n*السطر الثاني:* رقم هاتف الزبون.\n*الأسطر الباقية:* كل منتج بسطر واحد.\n\nاكتب *اوامر* أو *قائمة* لرؤية قائمة الأوامر والاختصارات.", 
        parse_mode="Markdown",
        reply_markup=markup
    )
    return ConversationHandler.END


async def show_commands_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة الأوامر والاختصارات للمدير والمجهز."""
    text = (
        "📋 *قائمة الأوامر والاختصارات*\n"
        "────────────────────\n\n"
        "👨🏻‍💼 *للمدير:*\n"
        "• *تقرير* (أو *تق*) — تقرير عام عن الطلبات والأرباح\n"
        "• *ارباح* (أو *ار*) — عرض ربح البيع والتجهيز\n"
        "• *تصفير* (أو *تص* / *صفر* / *صف*) — تصفير كل البيانات (يطلب تأكيد)\n"
        "• *تقرير الشراء* / *تقارير المجهزين* — تقارير فواتير كل المجهزين\n"
        "• *مناطق* — عرض مناطق التوصيل والأسعار\n"
        "• *مسح* — مسح طلبية معينة (برقم الزبون)\n"
        "• *حذف كل* / *حك* — تنظيف رسائل الكروب\n\n"
        "👷 *للمجهز:*\n"
        "• *تقريري* — تقرير الطلبيات اللي جهزتها أنا\n"
        "• *صفر* (للمجهز) — تصفير تقاريري فقط\n\n"
        "🔄 *للجميع:*\n"
        "• *الطلبات* — عرض الطلبات غير المكتملة"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def receive_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    invoice_numbers = context.application.bot_data['invoice_numbers']
    last_button_message = context.application.bot_data['last_button_message']

    print("📩 تم استقبال رسالة جديدة داخل receive_order")
    try:
        logger.info(f"[{update.effective_chat.id}] Processing order from: {update.effective_user.id} - Message ID: {update.message.message_id}. User data: {json.dumps(context.user_data.get(str(update.effective_user.id), {}), indent=2)}")
        await process_order(update, context, update.message)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"[{update.effective_chat.id}] Error in receive_order: {e}", exc_info=True)
        await update.message.reply_text("ماكدرت اعالج الطلب عاجبك لوتحاول مره ثانيه لو ادز طلب جديد ولا تصفن.")
        return ConversationHandler.END

async def edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    invoice_numbers = context.application.bot_data['invoice_numbers']
    last_button_message = context.application.bot_data['last_button_message']

    try:
        if not update.edited_message:
            return
        logger.info(f"[{update.effective_chat.id}] Processing edited order from: {update.effective_user.id} - Message ID: {update.edited_message.message_id}. User data: {json.dumps(context.user_data.get(str(update.effective_user.id), {}), indent=2)}")
        await process_order(update, context, update.edited_message, edited=True)
    except Exception as e:
        logger.error(f"[{update.effective_chat.id}] Error in edited_message: {e}", exc_info=True)
        await update.edited_message.reply_text("طك بطك ماكدر اعدل تريد سوي طلب جديد.")


async def handle_region_suggestion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة أزرار اختيار المنطقة (كل منطقة قريبة بزر، أو لا اكتب بنفسك)."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    data = query.data
    orders = context.application.bot_data.get("orders", {})
    ud = context.user_data.setdefault(user_id, {})

    # اختار منطقة من الأزرار (pick_zone_ORDERID_0، pick_zone_ORDERID_1، ...)
    if data.startswith("pick_zone_"):
        rest = data.replace("pick_zone_", "", 1)
        parts = rest.rsplit("_", 1)
        if len(parts) != 2:
            return
        order_id, idx_str = parts
        try:
            idx = int(idx_str)
        except ValueError:
            return
        if order_id not in orders:
            await query.edit_message_text("الطلبية ما عاد موجودة.")
            ud.pop("pending_region_order_id", None)
            ud.pop("pending_region_suggested_zones", None)
            ud.pop("pending_region_suggested_pairs", None)
            return
        zones_list = ud.get("pending_region_suggested_zones") or []
        suggested_pairs = ud.get("pending_region_suggested_pairs") or []
        if idx < 0 or idx >= len(zones_list):
            return
        chosen_zone = zones_list[idx]
        word_to_remove = suggested_pairs[idx][1] if idx < len(suggested_pairs) else None
        orders[order_id]["title"] = chosen_zone
        if word_to_remove and "products" in orders[order_id]:
            products = orders[order_id]["products"]
            orders[order_id]["products"] = [p for p in products if p != word_to_remove]
        ud.pop("pending_region_order_id", None)
        ud.pop("pending_region_suggested_zones", None)
        ud.pop("pending_region_suggested_pairs", None)
        context.application.create_task(save_data_in_background(context))
        await query.edit_message_text(f"تم اختيار المنطقة: *{chosen_zone}*", parse_mode="Markdown")
        if orders[order_id].get("phone_number") == "مطلوب":
            ud["pending_phone_order_id"] = order_id
        await show_buttons(query.message.chat_id, context, user_id, order_id)
        return

    # زر "لا — اكتب اسم المنطقه"
    if data.startswith("reject_region_"):
        order_id = data.replace("reject_region_", "")
        ud.pop("pending_region_suggested_zones", None)
        ud.pop("pending_region_suggested_pairs", None)
        await query.edit_message_text("طيب اكتبلي اسم المنطقه.")
        # pending_region_order_id يبقى عشان الرسالة الجاية ناخذها كاسم منطقة


def _extract_phone_from_text(text):
    """
    يدور بين أسطر/كلمات الرسالة ويطلع أول رقم زبون (حتى لو +964 776 403 1859)
    ويعدّله إلى صيغة 07764031859.
    """
    if not text or not text.strip():
        return "مطلوب"
    # إزالة مسافات وشرطات عشان نلقط الرقم من أي شكل (+964 776 403 1859 → 07764031859)
    raw = re.sub(r"[\s\-]", "", text)
    # نمط: +964 7xxxxxxxx أو 07xxxxxxxx أو 7xxxxxxxx (رقم عراقي 10 خانات بعد 7)
    m = re.search(r"(?:\+?964)?0?7\d{9}", raw)
    if m:
        digits = re.sub(r"\D", "", m.group(0))
        if digits.startswith("964"):
            digits = digits[3:]
        if digits.startswith("7") and len(digits) >= 10:
            return "0" + digits[:10]
        if digits.startswith("0") and len(digits) >= 11:
            return digits[:11]
    m2 = re.search(r"7\d{9}", raw)
    if m2:
        return "0" + m2.group(0)
    m3 = re.search(r"07\d{9}", raw)
    if m3:
        return m3.group(0)
    return "مطلوب"


def _parse_site_order_format(raw_text):
    """
    يحوّل طلب بصيغة الموقع (اسم الزبون، العنوان، معلومات الطلب، الاسم/الكمية/السعر)
    إلى: title, phone_number, products.
    يرجع None إذا الرسالة مو بهذه الصيغة.
    """
    if not raw_text or "معلومات الطلب" not in raw_text and "الاسم:" not in raw_text:
        return None
    text = raw_text.strip()
    if "العنوان:" not in text and "اسم الزبون:" not in text:
        return None

    customer_name = ""
    address = ""
    products = []

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    for line in lines:
        if line.startswith("اسم الزبون:"):
            customer_name = line.split(":", 1)[1].strip()
        elif line.startswith("العنوان:"):
            address = line.split(":", 1)[1].strip()
        elif line.startswith("الاسم:"):
            name_part = line.split(":", 1)[1].strip()
            if name_part and name_part not in ("الكمية", "السعر", "السعر الكلي") and len(name_part) >= 2:
                products.append(name_part)

    # تجاهل أسطر مثل "******" و "السعر الكلي" و "الكمية:" و "السعر:" (ما نعدها منتجات)
    if not address and not customer_name:
        return None

    # اسم المنطقة: ناخذه من مقابيل "العنوان" — نطابق أول كلمة أو كلمتين فقط (المنطقة غالباً بالبداية) عشان كوت صحي→كوت الصلحي، بي عسكري→حي العسكري
    if address:
        try:
            tokens = address.strip().split()
            address_phrase = " ".join(tokens[:2]) if len(tokens) >= 2 else (tokens[0] if tokens else address)
            if address_phrase and len(address_phrase) >= 2:
                canonical_zone = get_closest_zone_name(address_phrase, cutoff=0.5)
                title = canonical_zone if canonical_zone else address
            else:
                title = address
        except Exception:
            title = address
    else:
        zone = get_matching_zone_name(text)
        title = zone if zone else (customer_name or "عنوان غير معروف")

    # رقم الزبون: ندوّر في كل النص ونطلع أول رقم ييشبه رقم زبون
    phone_number = _extract_phone_from_text(text)

    if not products:
        return None

    return {"title": title, "phone_number": phone_number, "products": products, "address": address}


async def process_order(update, context, message, edited=False):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    invoice_numbers = context.application.bot_data['invoice_numbers']
    last_button_message = context.application.bot_data['last_button_message']
    
    user_id = str(message.from_user.id)
    raw_text = message.text.strip()
    lines = [line.strip() for line in raw_text.split('\n') if line.strip()]

    # ✅ إذا كان البوت ينتظر اسم المنطقة — المستخدم يقدر يضغط زر أو يكتب اسم المنطقة مباشرة (بدون ما يضغط "لا")
    ud = context.user_data.setdefault(user_id, {})
    pending_region_oid = ud.get("pending_region_order_id")
    if not edited and pending_region_oid and pending_region_oid in orders:
        new_region = raw_text.strip()
        if new_region and len(new_region) < 200:  # رسالة معقولة كاسم منطقة
            suggested_pairs = ud.get("pending_region_suggested_pairs") or []
            suggested_zone_names = [z for z, _ in suggested_pairs]
            # إذا الكتابة مطابقة جداً لأحد المناطق المقترحة → نعاملها كانه اختارها ونسحب الكلمة من المنتجات
            match = match_text_to_suggested_zones(new_region, suggested_zone_names, cutoff=0.8)
            if match is not None:
                idx, chosen_zone = match
                word_to_remove = suggested_pairs[idx][1] if idx < len(suggested_pairs) else None
                orders[pending_region_oid]["title"] = chosen_zone
                if word_to_remove and "products" in orders[pending_region_oid]:
                    products = orders[pending_region_oid]["products"]
                    orders[pending_region_oid]["products"] = [p for p in products if p != word_to_remove]
                ud.pop("pending_region_order_id", None)
                ud.pop("pending_region_suggested_zones", None)
                ud.pop("pending_region_suggested_pairs", None)
                ud.pop("pending_region_suggested_zone", None)
                context.application.create_task(save_data_in_background(context))
                await message.reply_text(f"تم اختيار المنطقة: *{chosen_zone}*", parse_mode="Markdown")
                if orders[pending_region_oid].get("phone_number") == "مطلوب":
                    ud["pending_phone_order_id"] = pending_region_oid
                await show_buttons(message.chat_id, context, user_id, pending_region_oid)
                return
            # إذا المنطقة معروفة (من القاعدة) لكن ما طابقت المقترحات — نحدّث العنوان فقط
            if is_zone_known(new_region):
                orders[pending_region_oid]["title"] = new_region
                ud.pop("pending_region_order_id", None)
                ud.pop("pending_region_suggested_zones", None)
                ud.pop("pending_region_suggested_pairs", None)
                ud.pop("pending_region_suggested_zone", None)
                context.application.create_task(save_data_in_background(context))
                await message.reply_text(f"تم تحديث المنطقة إلى: *{new_region}*", parse_mode="Markdown")
                if orders[pending_region_oid].get("phone_number") == "مطلوب":
                    ud["pending_phone_order_id"] = pending_region_oid
                await show_buttons(message.chat_id, context, user_id, pending_region_oid)
                return
            # منطقة غير مسجلة
            await message.reply_text(f"المنطقة *{new_region}* غير مسجلة أيضاً. أرسل اسم المنطقة الصحيح (اكتب *مناطق* لرؤية القائمة).", parse_mode="Markdown")
            ud["pending_region_order_id"] = pending_region_oid
        return

    # ✅ إذا الطلبية السابقة كانت من الموقع ورقم الزبون "مطلوب"، والرسالة الحالية رقم فقط → نحدّث الرقم
    if not edited and len(lines) == 1:
        one_line = lines[0].replace(" ", "").replace("+", "").replace("-", "")
        if one_line.isdigit() and len(one_line) >= 9:
            pending_oid = context.user_data.get(user_id, {}).get("pending_phone_order_id")
            if pending_oid and pending_oid in orders and orders[pending_oid].get("phone_number") == "مطلوب":
                phone_number_raw = lines[0].strip().replace(" ", "")
                if phone_number_raw.startswith("+964"):
                    phone_number = "0" + phone_number_raw[4:]
                else:
                    phone_number = phone_number_raw.replace("+", "")
                orders[pending_oid]["phone_number"] = phone_number
                context.user_data[user_id].pop("pending_phone_order_id", None)
                context.application.create_task(save_data_in_background(context))
                await message.reply_text(f"تم تحديث رقم الزبون إلى `{phone_number}`.", parse_mode="Markdown")
                await show_buttons(message.chat_id, context, user_id, pending_oid)
                return

    # ✅ محاولة قراءة صيغة طلب الموقع (اسم الزبون، العنوان، معلومات الطلب، الاسم/الكمية/السعر)
    site_parsed = _parse_site_order_format(message.text)
    zone_search_text = raw_text  # للنص العادي نبحث في كل الرسالة
    if site_parsed:
        title = site_parsed["title"]
        phone_number = site_parsed["phone_number"]
        products = site_parsed["products"]
        # للطلب من الموقع نبحث عن المناطق القريبة فقط في سطر العنوان
        if site_parsed.get("address"):
            zone_search_text = site_parsed["address"]
    else:
        # الصيغة العادية: الرقم من أي سطر، المنطقة من أي سطر (مطابقة مع قاعدة المناطق)، الباقي منتجات
        if len(lines) < 1:
            if not edited:
                await message.reply_text("باعلي تاكد انك تكتب الطلبية: عنوان أو منطقة، رقم الزبون، وكل سطر منتج. يالله فر ويلك.")
            return

        phone_number = _extract_phone_from_text(raw_text)
        try:
            zone = get_matching_zone_name(raw_text)
        except Exception as e:
            logger.warning(f"get_matching_zone_name failed: {e}")
            zone = None
        if zone:
            title = zone
        else:
            region_candidate = None
            try:
                for line in lines:
                    line_clean = line.strip()
                    if not line_clean:
                        continue
                    if _extract_phone_from_text(line_clean) != "مطلوب":
                        continue
                    if re.sub(r"[\d\s\.]", "", line_clean) == "" and len(line_clean) > 4:
                        continue
                    if get_closest_zone_names(line_clean, n=1, cutoff=0.4):
                        region_candidate = line_clean
                        break
            except Exception as e:
                logger.warning(f"region_candidate loop failed: {e}")
            title = region_candidate if region_candidate else (lines[0] if lines else "عنوان غير معروف")

        # المنتجات: نستبعد سطر الرقم وأي سطر فيه كلمة قريبة من اسم منطقة
        products = []
        for line in lines:
            if not line.strip():
                continue
            if phone_number != "مطلوب" and _extract_phone_from_text(line) == phone_number:
                continue
            if zone and zone in line:
                continue
            if re.sub(r"[\d\s\.]", "", line) == "" and len(line.strip()) > 5:
                continue
            products.append(line.strip())

        try:
            if not zone and lines and not get_closest_zone_names(title or "", n=1, cutoff=0.4):
                title = lines[0]
        except Exception:
            if not zone and lines:
                title = lines[0]

    if not products:
        if not edited:
            await message.reply_text("يابه لازم المنتجات ورا رقم الهاتف .")
        return

    order_id = None
    is_new_order = True 

    if edited:
        for oid, msg_info in last_button_message.items():
            if msg_info and msg_info.get("message_id") == message.message_id and str(msg_info.get("chat_id")) == str(message.chat_id):
                if oid in orders: 
                    order_id = oid
                    is_new_order = False
                    logger.info(f"Found existing order {order_id} based on message ID (edited message).")
                    break
                else:
                    logger.warning(f"Message ID {message.message_id} found in last_button_message but order {oid} is missing. Treating as new.")
                    order_id = None 
                    
    if not order_id: 
        order_id = str(uuid.uuid4())[:8]
        invoice_no = get_invoice_number() 
        # ✅ إضافة phone_number و created_at إلى قاموس الطلبية
        orders[order_id] = {
            "user_id": user_id, 
            "title": title, 
            "phone_number": phone_number, 
            "products": products, 
            "places_count": 0,
            "created_at": datetime.now(timezone.utc).isoformat() # ✅ هذا السطر الجديد
        } 
        pricing[order_id] = {p: {} for p in products}
        invoice_numbers[order_id] = invoice_no
        if phone_number == "مطلوب":
            context.user_data.setdefault(user_id, {})["pending_phone_order_id"] = order_id
        logger.info(f"Created new order {order_id} for user {user_id}.")
    else: 
        old_products = set(orders[order_id].get("products", []))
        new_products = set(products)
        
        orders[order_id]["title"] = title
        orders[order_id]["phone_number"] = phone_number # ✅ تحديث رقم الهاتف في الطلبية الموجودة
        orders[order_id]["products"] = products
        # اذا تم تعديل الطلبية، ما نغير تاريخ الانشاء
        
        for p in new_products:
            if p not in pricing.get(order_id, {}):
                pricing.setdefault(order_id, {})[p] = {}
        
        if order_id in pricing:
            for p in old_products - new_products:
                if p in pricing[order_id]:
                    del pricing[order_id][p]
                    logger.info(f"Removed pricing for product '{p}' from order {order_id}.")
        logger.info(f"Updated existing order {order_id}. Initiator: {user_id}.")
        
    context.application.create_task(save_data_in_background(context))
    
    # ✅ البوت يقرا كل كلمات الرسالة ويطابقها بقاعدة المناطق ويطلع: منطقة قريبة لـ كلمة
    if is_new_order and not is_zone_known(title):
        ud = context.user_data.setdefault(user_id, {})
        ud["pending_region_order_id"] = order_id
        try:
            # طلب الموقع: نبحث فقط في سطر العنوان؛ غيره: نبحث في كل الرسالة. cutoff 0.5 عشان كوت صحي→كوت الصلحي، بي عسكري→حي العسكري
            suggested_pairs = get_close_zones_with_words(zone_search_text, per_word_n=4, cutoff=0.5)
        except Exception as e:
            logger.warning(f"get_close_zones_with_words failed: {e}", exc_info=True)
            suggested_pairs = []
        if suggested_pairs:
            suggested_pairs = suggested_pairs[:15]
            ud["pending_region_suggested_zones"] = [zone for zone, _ in suggested_pairs]
            ud["pending_region_suggested_pairs"] = suggested_pairs
            lines = [
                "ما عيّنت المنطقة، عندك مناطق قريبة بقاعدة البيانات — اختار الصح أو دوس لا واكتب اسم المنطقة",
                "",
            ]
            for zone, word in suggested_pairs:
                lines.append(f"• {zone} قريبة لـ {word}")
            kb_rows = []
            for i, (zone_name, _) in enumerate(suggested_pairs):
                kb_rows.append([InlineKeyboardButton(zone_name, callback_data=f"pick_zone_{order_id}_{i}")])
            kb_rows.append([InlineKeyboardButton("لا — اكتب اسم المنطقه", callback_data=f"reject_region_{order_id}")])
            kb = InlineKeyboardMarkup(kb_rows)
            await message.reply_text(
                "\n".join(lines),
                reply_markup=kb
            )
        else:
            await message.reply_text(
                "ما طابقت أي كلمة من رسالتك مع قاعدة المناطق.\n\nأرسل اسم المنطقة الصحيح (اكتب *مناطق* لرؤية القائمة) — بعدها راح تطلع أزرار التسعير.",
                parse_mode="Markdown"
            )
        return

    # ✅ تعديل رسالة الاستلام لتضمين رقم الهاتف بالشكل الجديد
    if is_new_order:
        reply_msg = f"طلب : *{title}*\n(الرقم: `{phone_number}`)\n(عدد المنتجات: {len(products)})"
        if phone_number == "مطلوب":
            reply_msg += "\n\n📱 رقم الزبون ما كان بالرسالة — دز رقم الزبون فقط وراح نحدث الطلبية، أو عدّل الطلبية من الزر."
        await message.reply_text(reply_msg, parse_mode="Markdown")
        await show_buttons(message.chat_id, context, user_id, order_id)
    else:
        await show_buttons(message.chat_id, context, user_id, order_id, confirmation_message="دهاك حدثنه الطلب. عيني دخل الاسعار الاستاذ حدث الطلب.")

async def show_buttons(chat_id, context, user_id, order_id, confirmation_message=None):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    last_button_message = context.application.bot_data['last_button_message']

    try:
        if order_id not in orders:
            await context.bot.send_message(chat_id=chat_id, text="❌ الطلب غير موجود.")
            return

        order = orders[order_id]
        final_buttons_list = []

        # أزرار الإضافة والمسح (السطر الأول)
        final_buttons_list.append([
            InlineKeyboardButton("➕ إضافة منتج", callback_data=f"add_product_to_order_{order_id}"),
            InlineKeyboardButton("🗑️ مسح منتج", callback_data=f"delete_specific_product_{order_id}")
        ])

        completed_products_buttons = []
        pending_products_buttons = []

        # جلب قائمة المنتجات المعدلة حالياً من بيانات المستخدم
        user_data = context.user_data.get(user_id, {})
        edited_list = user_data.get("edited_products_list", [])
        editing_mode = user_data.get("editing_mode", False)

        for i, p_name in enumerate(order["products"]):
            callback_data_for_product = f"{order_id}|{i}"
            
            # تحديد شكل الزر (تم التسعير، معدل، أو جديد)
            is_priced = p_name in pricing.get(order_id, {}) and 'buy' in pricing[order_id].get(p_name, {})

            if is_priced:
                if p_name in edited_list:
                    button_text = f"✏️✅ {p_name}"  # علامة القلم للمعدل
                else:
                    button_text = f"✅ {p_name}"    # علامة صح للمسعر مسبقاً
                completed_products_buttons.append([InlineKeyboardButton(button_text, callback_data=callback_data_for_product)])
            else:
                button_text = p_name
                pending_products_buttons.append([InlineKeyboardButton(button_text, callback_data=callback_data_for_product)])

        # دمج القوائم
        final_buttons_list.extend(completed_products_buttons)
        final_buttons_list.extend(pending_products_buttons)

        # ✅ أزرار التحكم في وضع التعديل (تظهر فقط عند النقر على "تعديل الطلبية")
        if editing_mode:
            final_buttons_list.append([
                InlineKeyboardButton("🏪 تعديل المحلات", callback_data=f"done_editing_{order_id}")
            ])
            final_buttons_list.append([
                InlineKeyboardButton("💾 حفظ واكتمل التعديل", callback_data=f"cancel_edit_{order_id}")
            ])

        markup = InlineKeyboardMarkup(final_buttons_list)

        # تجهيز نص الرسالة
        message_text = f"{confirmation_message}\n\n" if confirmation_message else ""
        status_text = "🔧 وضع التعديل حالياً" if editing_mode else "📝 تسعير الطلب"
        message_text += f"*{status_text}* ({order['title']}):\nاختر منتجاً لتعديل سعره:"

        # حذف الرسالة السابقة لتجنب تراكم الرسائل
        msg_info = last_button_message.get(order_id)
        if msg_info:
            context.application.create_task(delete_message_in_background(context, chat_id=msg_info["chat_id"], message_id=msg_info["message_id"]))

        # إرسال الرسالة الجديدة
        msg = await context.bot.send_message(
            chat_id=chat_id, 
            text=message_text, 
            reply_markup=markup, 
            parse_mode="Markdown"
        )
        
        # حفظ معلومات الرسالة الأخيرة
        last_button_message[order_id] = {"chat_id": chat_id, "message_id": msg.message_id}
        context.application.create_task(save_data_in_background(context)) 

        # تنظيف أي رسائل مؤقتة أخرى
        if 'messages_to_delete' in user_data:
            for m_info in user_data['messages_to_delete']:
                context.application.create_task(delete_message_in_background(context, chat_id=m_info['chat_id'], message_id=m_info['message_id']))
            user_data['messages_to_delete'].clear()
            
    except Exception as e:
        logger.error(f"Error in show_buttons: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="⚠️ حدث خطأ في عرض قائمة المنتجات.")

        
async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    last_button_message = context.application.bot_data['last_button_message']

    try: 
        query = update.callback_query
        await query.answer()

        user_id = str(query.from_user.id)
        
        # اضافة الرسالة لقائمة الحذف
        context.user_data.setdefault(user_id, {}).setdefault('messages_to_delete', []).append({
            'chat_id': query.message.chat_id,
            'message_id': query.message.message_id
        })

        order_id, product_index_str = query.data.split('|', 1)
        
        if order_id not in orders:
            await query.edit_message_text("زربت الطلبية مموجوده.")
            return ConversationHandler.END

        try:
            product_index = int(product_index_str)
            product = orders[order_id]["products"][product_index]
        except (ValueError, IndexError, KeyError):
            await query.edit_message_text("خطأ في تحديد المنتج.")
            return ConversationHandler.END

        context.user_data[user_id]["order_id"] = order_id
        context.user_data[user_id]["product"] = product 
        context.user_data[user_id].pop("buy_price", None) 

        current_buy = pricing.get(order_id, {}).get(product, {}).get("buy")
        current_sell = pricing.get(order_id, {}).get(product, {}).get("sell")

        message_prompt = ""
        if current_buy is not None and current_sell is not None:
            message_prompt = f"سعر *'{product}'* حالياً: {format_float(current_buy)} / {format_float(current_sell)}.\nدز السعر الجديد (شراء وبيع):"
        else:
            message_prompt = (
                f"تمام، بيش اشتريت *'{product}'*؟ (بالسطر الأول)\n"
                f"وبييش راح تبيعه؟ (بالسطر الثاني)\n\n"
                f"💡 **إذا السعر نفسه،** اكتب الرقم مرة واحدة."
            )

        # ✅✅ هنا ضفنا زر الإلغاء ✅✅
        cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء واختيار غير منتج", callback_data="cancel_price_entry")]])

        msg = await query.message.reply_text(message_prompt, parse_mode="Markdown", reply_markup=cancel_markup)
        
        context.user_data[user_id]['messages_to_delete'].append({
            'chat_id': msg.chat_id, 
            'message_id': msg.message_id
        })
        return ASK_BUY 

    except Exception as e: 
        logger.error(f"Error in product_selected: {e}", exc_info=True)
        await update.callback_query.message.reply_text("صار خطأ.")
        return ConversationHandler.END
        
async def cancel_price_entry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    chat_id = query.message.chat_id
    
    logger.info(f"[{chat_id}] User {user_id} cancelled price entry.")
    
    # تنظيف الذاكرة المؤقتة الخاصة بتسعير المنتج
    if user_id in context.user_data:
        context.user_data[user_id].pop("order_id", None)
        context.user_data[user_id].pop("product", None)
    
    # حذف رسالة "ادخل السعر"
    try:
        await query.message.delete()
    except Exception:
        pass
        
    await context.bot.send_message(chat_id=chat_id, text="تم الإلغاء. تكدر تختار منتج ثاني أو تسوي طلب جديد.")
    return ConversationHandler.END

async def add_new_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() 

    user_id = str(query.from_user.id)
    chat_id = query.message.chat_id
    order_id = query.data.replace("add_product_to_order_", "") 

    logger.info(f"[{chat_id}] Add new product button clicked for order {order_id} by user {user_id}.")

    context.user_data.setdefault(user_id, {}) 

    # حفظ الـ order_id في user_data للحالة القادمة
    context.user_data[user_id]["current_active_order_id"] = order_id
    context.user_data[user_id]["adding_new_product"] = True # علامة لتدل على أننا في عملية إضافة منتج

    # حذف رسالة الأزرار القديمة (إذا كانت موجودة)
    if query.message:
        context.application.create_task(delete_message_in_background(context, chat_id=query.message.chat_id, message_id=query.message.message_id))

    # ✅ إضافة زر الإلغاء هنا
    cancel_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ إلغاء الإضافة", callback_data=f"cancel_add_product_{order_id}")]
    ])
    await context.bot.send_message(chat_id=chat_id, text="تمام، شنو اسم المنتج الجديد اللي تريد تضيفه؟", reply_markup=cancel_keyboard)
    return ASK_PRODUCT_NAME # حالة محادثة جديدة لطلب اسم المنتج

async def delete_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    chat_id = query.message.chat_id

    order_id = query.data.replace("delete_specific_product_", "") 

    logger.info(f"[{chat_id}] General delete product button clicked for order {order_id} by user {user_id}.")

    if order_id not in orders:
        logger.warning(f"[{chat_id}] No active order found or order_id invalid for user {user_id} when trying to display delete products.")
        await context.bot.send_message(chat_id=chat_id, text="ترا ماكو طلب فعال حتى أظهرلك منتجات للمسح. سوي طلب جديد أول.")
        return ConversationHandler.END

    order = orders[order_id]

    if not order["products"]: # إذا الطلبية ما بيها منتجات أصلاً
        await context.bot.send_message(chat_id=chat_id, text="ترا الطلبية ما بيها أي منتجات حتى تمسح منها.")
        return ConversationHandler.END

    products_to_delete_buttons = []
    
    # ✅ التغيير هنا: نستخدم index (i) للمنتج
    for i, p_name in enumerate(order["products"]):
        # ✅ التغيير هنا: callback_data صار يستخدم الـ index بدل الاسم
        # وهذا يحل مشكلة الاسم الطويل
        products_to_delete_buttons.append([InlineKeyboardButton(p_name, callback_data=f"confirm_delete_idx_{order_id}_{i}")])

    # ✅ إضافة زر الإلغاء هنا
    products_to_delete_buttons.append([InlineKeyboardButton("❌ إلغاء المسح", callback_data=f"cancel_delete_product_{order_id}")])

    markup = InlineKeyboardMarkup(products_to_delete_buttons)

    # حذف رسالة الأزرار القديمة (إذا كانت موجودة)
    if query.message:
        context.application.create_task(delete_message_in_background(context, chat_id=query.message.chat_id, message_id=query.message.message_id))

    await context.bot.send_message(chat_id=chat_id, text="تمام، دوس على المنتج اللي تريد تمسحه من الطلبية:", reply_markup=markup)
    return ConversationHandler.END
    
async def confirm_delete_product_by_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    chat_id = query.message.chat_id

    # استخراج الـ order_id والـ product_index من الـ callback_data
    # مثلاً: "confirm_delete_idx_12345678_0"
    try:
        # ✅ التغيير هنا: نقسم البيانات الجديدة اللي بيها الـ index
        parts = query.data.split('_')
        order_id = parts[3]
        product_index_to_delete = int(parts[4]) # نحول الـ index إلى رقم
    except (ValueError, IndexError):
        logger.error(f"[{chat_id}] Error parsing delete callback data: {query.data}")
        await context.bot.send_message(chat_id=chat_id, text="خطأ في بيانات الزر. حاول مرة أخرى.")
        return ConversationHandler.END

    logger.info(f"[{chat_id}] Index {product_index_to_delete} confirmed for deletion from order {order_id} by user {user_id}.")

    if order_id not in orders:
        logger.warning(f"[{chat_id}] Order {order_id} not found when trying to delete product index.")
        await context.bot.send_message(chat_id=chat_id, text="ترا الطلب مموجود حتى امسح منه منتج. سوي طلب جديد.")
        return ConversationHandler.END

    order = orders[order_id]

    # ✅ التغيير هنا: نتأكد إن الـ index موجود باللستة
    if 0 <= product_index_to_delete < len(order["products"]):
        
        # ✅ التغيير هنا: نمسح المنتج من اللستة باستخدام الـ index
        # هذا يضمن مسح المنتج الصحيح
        product_name_to_delete = order["products"].pop(product_index_to_delete) 

        logger.info(f"[{chat_id}] Product '{product_name_to_delete}' deleted from order {order_id}.")
        await context.bot.send_message(chat_id=chat_id, text=f"تم حذف المنتج '{product_name_to_delete}' من الطلبية بنجاح.")

        # ✅ هذا المنطق يحمي من مسح السعر إذا كان المنتج مكرر
        # ما راح نمسح السعر إلا إذا كان هذا آخر منتج بنفس الاسم
        if product_name_to_delete not in order["products"]:
            if order_id in pricing and product_name_to_delete in pricing[order_id]:
                del pricing[order_id][product_name_to_delete]
                logger.info(f"[{chat_id}] Deleted pricing for product '{product_name_to_delete}' as it was the last one.")
        else:
            logger.info(f"[{chat_id}] Kept pricing for '{product_name_to_delete}' as other instances exist.")

        context.application.create_task(save_data_in_background(context)) # حفظ البيانات بعد حذف المنتج
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"ترا المنتج مو موجود بالطلبية أصلاً (يمكن انمسح). تأكد من الاسم.")

    # نرجع نعرض الأزرار المحدثة
    await show_buttons(chat_id, context, user_id, order_id) 
    return ConversationHandler.END

async def cancel_add_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    chat_id = query.message.chat_id
    order_id = query.data.replace("cancel_add_product_", "")

    logger.info(f"[{chat_id}] Cancel add product button clicked for order {order_id} by user {user_id}.")

    # حذف رسالة الأزرار القديمة (إذا كانت موجودة)
    if query.message:
        context.application.create_task(delete_message_in_background(context, chat_id=query.message.chat_id, message_id=query.message.message_id))

    await context.bot.send_message(chat_id=chat_id, text="تم إلغاء عملية إضافة منتج جديد.")
    # نرجع نعرض الأزرار الأصلية
    await show_buttons(chat_id, context, user_id, order_id)
    return ConversationHandler.END

async def cancel_delete_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    chat_id = query.message.chat_id
    order_id = query.data.replace("cancel_delete_product_", "")

    logger.info(f"[{chat_id}] Cancel delete product button clicked for order {order_id} by user {user_id}.")

    # حذف رسالة الأزرار القديمة (إذا كانت موجودة)
    if query.message:
        context.application.create_task(delete_message_in_background(context, chat_id=query.message.chat_id, message_id=query.message.message_id))

    await context.bot.send_message(chat_id=chat_id, text="تم إلغاء عملية مسح المنتج.")
    # نرجع نعرض الأزرار الأصلية
    await show_buttons(chat_id, context, user_id, order_id)
    return ConversationHandler.END

async def receive_buy_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    user_display_name = update.effective_user.first_name

    try:
        # 1. مسح رسالة المستخدم (المجهز) فوراً للحفاظ على نظافة الجات
        context.application.create_task(delete_message_in_background(context, chat_id=chat_id, message_id=update.message.message_id))

        if user_id not in context.user_data or "order_id" not in context.user_data[user_id]:
            await update.message.reply_text("❌ انتهت الجلسة، يرجى اختيار المنتج مرة أخرى.")
            return ConversationHandler.END

        order_id = context.user_data[user_id]["order_id"]
        product = context.user_data[user_id]["product"]
        
        # 2. تحليل النص المدخل (سواء كان بسطرين أو بمسافة بين الرقمين)
        input_text = update.message.text.strip().split()
        
        try:
            if len(input_text) == 1:
                # إذا دز رقم واحد، نعتبر الشراء والبيع نفس الشيء
                buy_price = float(input_text[0])
                sell_price = float(input_text[0])
            elif len(input_text) >= 2:
                # إذا دز رقمين، الأول شراء والثاني بيع
                buy_price = float(input_text[0])
                sell_price = float(input_text[1])
            else:
                raise ValueError
        except ValueError:
            msg = await update.message.reply_text("⚠️ يرجى إرسال أرقام صحيحة (مثلاً: 10 12 أو بس رقم واحد إذا السعر نفسه).")
            # إضافة رسالة الخطأ لقائمة الحذف حتى لا تبقى مشوهة للجات
            context.user_data.setdefault(user_id, {}).setdefault('messages_to_delete', []).append({'chat_id': msg.chat_id, 'message_id': msg.message_id})
            return ASK_BUY

        # 3. المحافظة على صاحب التسعير الأول (المنطق الذي طلبته سابقاً)
        original_pricing_data = pricing.get(order_id, {}).get(product, {})
        original_worker_name = original_pricing_data.get("prepared_by_name")
        original_worker_id = original_pricing_data.get("prepared_by_id")
        
        final_worker_name = original_worker_name if original_worker_name else user_display_name
        final_worker_id = original_worker_id if original_worker_id else user_id

        # 4. حفظ التسعير في قاعدة البيانات
        if order_id not in pricing:
            pricing[order_id] = {}
        
        pricing[order_id][product] = {
            "buy": buy_price,
            "sell": sell_price,
            "prepared_by_name": final_worker_name,
            "prepared_by_id": final_worker_id
        }

        # 5. التعامل مع "وضع التعديل" وعلامة القلم ✏️
        is_editing = context.user_data.get(user_id, {}).get("editing_mode", False)
        if is_editing:
            if "edited_products_list" not in context.user_data[user_id]:
                context.user_data[user_id]["edited_products_list"] = []
            if product not in context.user_data[user_id]["edited_products_list"]:
                context.user_data[user_id]["edited_products_list"].append(product)

        # حفظ البيانات في الخلفية
        context.application.create_task(save_data_in_background(context))

        # تنظيف بيانات الجلسة المؤقتة
        context.user_data[user_id].pop("order_id", None)
        context.user_data[user_id].pop("product", None)

        # 6. التحقق هل اكتمل الطلب؟
        current_order_products = orders[order_id].get("products", [])
        priced_products = pricing.get(order_id, {})
        is_order_complete = all(p in priced_products and "buy" in priced_products[p] for p in current_order_products)

        # 7. التوجيه النهائي: إذا تعديل نبقى، إذا طلب جديد واكتمل نروح للمحلات
        if is_order_complete and not is_editing:
            await request_places_count_standalone(chat_id, context, user_id, order_id)
        else:
            await show_buttons(chat_id, context, user_id, order_id, 
                             confirmation_message=f"✅ تم حفظ {product}: شراء {format_float(buy_price)} / بيع {format_float(sell_price)}")
        
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error in receive_buy_price: {e}", exc_info=True)
        await update.message.reply_text("❌ حدث خطأ غير متوقع.")
        return ConversationHandler.END

        

async def receive_new_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    
    # تحويل الرسالة إلى قائمة أسطر وتنظيفها
    incoming_text = update.message.text.strip()
    new_products_list = [line.strip() for line in incoming_text.split('\n') if line.strip()]

    logger.info(f"[{chat_id}] Received products to add: {new_products_list} from user {user_id}.")

    order_id = context.user_data[user_id].get("current_active_order_id")

    if not order_id or order_id not in context.application.bot_data['orders']:
        logger.warning(f"[{chat_id}] No active order found or order_id invalid for user {user_id}.")
        await update.message.reply_text("ترا ماكو طلب فعال حتى أضيفله منتج. سوي طلب جديد أول.")
        context.user_data[user_id].pop("adding_new_product", None)
        return ConversationHandler.END

    order = context.application.bot_data['orders'][order_id]
    added_count = 0
    skipped_products = []

    for p_name in new_products_list:
        if p_name in order["products"]:
            skipped_products.append(p_name)
        else:
            order["products"].append(p_name)
            added_count += 1

    # توثيق الحفظ
    if added_count > 0:
        logger.info(f"[{chat_id}] Added {added_count} new products to order {order_id}.")
        context.application.create_task(save_data_in_background(context))
        
        msg_text = f"✅ تمت إضافة {added_count} منتج للطلبية بنجاح."
        if skipped_products:
            msg_text += f"\n⚠️ (تجاهلت {len(skipped_products)} منتج لأنهم موجودين أصلاً)."
        await update.message.reply_text(msg_text)
    else:
        await update.message.reply_text("ترا كل المنتجات اللي دزيتهن موجودات بالطلبية أصلاً! 😅")

    # تنظيف الذاكرة المؤقتة
    context.user_data[user_id].pop("adding_new_product", None)
    context.user_data[user_id].pop("current_active_order_id", None)

    # عرض الأزرار المحدثة (التي تعتمد على الـ index لمنع أخطاء طول البيانات)
    await show_buttons(chat_id, context, user_id, order_id)
    return ConversationHandler.END

async def request_places_count_standalone(chat_id, context: ContextTypes.DEFAULT_TYPE, user_id: str, order_id: str):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']

    try:
        logger.info(f"[{chat_id}] request_places_count_standalone called for order {order_id} from user {user_id}. User data: {json.dumps(context.user_data.get(user_id), indent=2)}")
        context.user_data.setdefault(user_id, {})["current_active_order_id"] = order_id

        buttons = []
        emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']
        for i in range(1, 11):
            buttons.append(InlineKeyboardButton(emojis[i-1], callback_data=f"places_data_{order_id}_{i}"))
        
        keyboard = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
        reply_markup = InlineKeyboardMarkup(keyboard)

        msg_places = await context.bot.send_message(
            chat_id=chat_id,
            text="صلوات كللوش كل المنتجات تسعرت ديالله اختار عدد المحلات وفضني؟ (باوع ممنوع تكتب رقم لازم تختار من ذني الارقام )", 
            reply_markup=reply_markup
        )
        
        context.user_data[user_id]['places_count_message'] = {
            'chat_id': msg_places.chat_id,
            'message_id': msg_places.message_id
        }

        if user_id in context.user_data and 'messages_to_delete' in context.user_data[user_id]:
            logger.info(f"[{chat_id}] Scheduling deletion of {len(context.user_data[user_id].get('messages_to_delete', []))} old messages after showing places buttons for user {user_id}.")
            for msg_info in context.user_data[user_id]['messages_to_delete']:
                context.application.create_task(delete_message_in_background(context, chat_id=msg_info['chat_id'], message_id=msg_info['message_id']))
            context.user_data[user_id]['messages_to_delete'].clear()
        
    except Exception as e:
        logger.error(f"[{chat_id}] Error in request_places_count_standalone: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="😐ترا صار عطل من جاي اطلب عدد المحلات. تريد سوي طلب جديد.")
        
async def handle_places_count_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    daily_profit = context.application.bot_data['daily_profit']
    
    try:
        places = None
        chat_id = update.effective_chat.id
        user_id = str(update.effective_user.id) 
        logger.info(f"[{chat_id}] handle_places_count_data triggered by user {user_id}.")

        context.user_data.setdefault(user_id, {})
        if 'messages_to_delete' not in context.user_data[user_id]:
            context.user_data[user_id]['messages_to_delete'] = []

        order_id_to_process = None 

        if update.callback_query:
            query = update.callback_query
            logger.info(f"[{chat_id}] Places count callback query received: {query.data}")
            await query.answer()
            
            try:
                parts = query.data.split('_')
                if len(parts) == 4 and parts[0] == "places" and parts[1] == "data":
                    order_id_to_process = parts[2] 
                    
                    if order_id_to_process not in orders:
                        await context.bot.send_message(chat_id=chat_id, text="باعلي هيو الطلبية مموجودة.")
                        if user_id in context.user_data and "current_active_order_id" in context.user_data[user_id]:
                            del context.user_data[user_id]["current_active_order_id"]
                        return ConversationHandler.END 

                    places = int(parts[3])
                    if query.message:
                        try:
                            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
                        except Exception:
                            pass
                else:
                    raise ValueError(f"Unexpected data: {query.data}")
            except Exception as e:
                logger.error(f"[{chat_id}] Failed to parse places count: {e}", exc_info=True)
                await context.bot.send_message(chat_id=chat_id, text="😐الدكمة زربت.")
                return ConversationHandler.END 
        
        elif update.message: 
            context.user_data[user_id]['messages_to_delete'].append({'chat_id': update.message.chat_id, 'message_id': update.message.message_id})
            order_id_to_process = context.user_data[user_id].get("current_active_order_id")

            if not order_id_to_process or order_id_to_process not in orders:
                 msg_error = await context.bot.send_message(chat_id=chat_id, text="ماكو طلبية فعالة حالياً.")
                 context.user_data[user_id]['messages_to_delete'].append({'chat_id': msg_error.chat_id, 'message_id': msg_error.message_id})
                 return ConversationHandler.END 

            if not update.message.text.strip().isdigit(): 
                msg_error = await context.bot.send_message(chat_id=chat_id, text="😐يابه دوس رقم صحيح.")
                context.user_data[user_id]['messages_to_delete'].append({'chat_id': msg_error.chat_id, 'message_id': msg_error.message_id})
                return ASK_PLACES_COUNT 
            
            try:
                places = int(update.message.text.strip())
                if places < 0: raise ValueError
            except ValueError: 
                msg_error = await context.bot.send_message(chat_id=chat_id, text="😐يابه ددوس عدل.")
                context.user_data[user_id]['messages_to_delete'].append({'chat_id': msg_error.chat_id, 'message_id': msg_error.message_id})
                return ASK_PLACES_COUNT 
        
        if places is None or order_id_to_process is None:
            await context.bot.send_message(chat_id=chat_id, text="عذراً، صار خطأ.")
            return ConversationHandler.END 

        if 'places_count_message' in context.user_data[user_id]:
            msg_info = context.user_data[user_id]['places_count_message']
            try:
                await context.bot.delete_message(chat_id=msg_info['chat_id'], message_id=msg_info['message_id'])
            except Exception:
                pass
            del context.user_data[user_id]['places_count_message']

        # ✅✅ هنا التعديل الجوهري ✅✅
        # نسجل عدد المحلات + نسجل انو هذا المستخدم هو صاحب الطلب النهائي
        orders[order_id_to_process]["places_count"] = places
        orders[order_id_to_process]["supplier_id"] = user_id  # <--- هذا السطر يخلي الملكية للشخص اللي داس الدكمة

        # حفظ البيانات
        context.application.bot_data['daily_profit'] = daily_profit 
        context.application.create_task(save_data_in_background(context))

        logger.info(f"[{chat_id}] Order {order_id_to_process} finalized by {user_id}. Places: {places}.")

        if user_id in context.user_data and 'messages_to_delete' in context.user_data[user_id]:
            for msg_info in context.user_data[user_id]['messages_to_delete']:
                context.application.create_task(delete_message_in_background(context, chat_id=msg_info['chat_id'], message_id=msg_info['message_id']))
            context.user_data[user_id]['messages_to_delete'].clear()
        
        await show_final_options(chat_id, context, user_id, order_id_to_process, message_prefix="هلهل كللوش.")
        
        if user_id in context.user_data and "current_active_order_id" in context.user_data[user_id]:
            del context.user_data[user_id]["current_active_order_id"]

        return ConversationHandler.END 
    except Exception as e:
        logger.error(f"[{chat_id}] Error in handle_places_count_data: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="عذراً، صار خطأ.", parse_mode="Markdown")
        return ConversationHandler.END
        

async def show_final_options(chat_id, context, user_id, order_id, message_prefix=None):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    invoice_numbers = context.application.bot_data['invoice_numbers']

    try:
        order = orders.get(order_id)
        if not order: return
        
        invoice = invoice_numbers.get(order_id, "000")
        phone_number = order.get('phone_number', 'ماكو رقم')
        
        # معلومات المجهز
        current_chat = await context.bot.get_chat(user_id)
        current_name = current_chat.first_name
        username = f"(@{current_chat.username})" if current_chat.username else ""
        
        total_buy = 0.0
        total_sell = 0.0
        
        admin_details = []    # لتفاصيل فاتورة الإدارة (الأرباح)
        admin_detail_lines = []  # لرسالة الإدارة التفصيلية: كل صنف مع من جهزه
        all_supplier_ids = set() # كل المجهزين الذين شاركوا بهذه الطلبية
        
        # 1. حساب المنتجات وتفاصيلها + تجميع المجهزين
        for p_name in order["products"]:
            data = pricing.get(order_id, {}).get(p_name, {})
            buy = float(data.get("buy", 0.0))
            sell = float(data.get("sell", 0.0))
            profit = sell - buy
            p_worker_id = str(data.get("prepared_by_id", ""))
            p_worker_name = data.get("prepared_by_name", "شخص آخر")
            
            total_buy += buy
            total_sell += sell
            
            if p_worker_id:
                all_supplier_ids.add(p_worker_id)
            admin_detail_lines.append(f"  • {p_name}: {format_float(buy)} - {p_worker_name}")
            admin_details.append(f"- {p_name}: شراء {format_float(buy)} | بيع {format_float(sell)} | ربح {format_float(profit)}")

        # 2. جلب كلفة التوصيل والمحلات
        delivery = float(get_delivery_price(order.get('title', '')))
        places_count = order.get("places_count", 0)
        extra_cost = float(calculate_extra(places_count))
        
        grand_total = total_sell + extra_cost + delivery

        # --- أ. بناء وإرسال رسالة تفصيلية لكل مجهز شارك بالطلبية ---
        supplier_totals = {}  # اسم المجهز -> المبلغ الكلي الذي دفعه (لرسالة الإدارة)
        for s_id in all_supplier_ids:
            try:
                sup_chat = await context.bot.get_chat(int(s_id))
                sup_name = sup_chat.first_name
                sup_username = f"(@{sup_chat.username})" if sup_chat.username else ""
            except Exception:
                sup_name = "مجهز"
                sup_username = ""
            supplier_details = []
            others_details = {}
            for p_name in order["products"]:
                data = pricing.get(order_id, {}).get(p_name, {})
                buy = float(data.get("buy", 0.0))
                p_worker_id = str(data.get("prepared_by_id", ""))
                p_worker_name = data.get("prepared_by_name", "شخص آخر")
                if p_worker_id == str(s_id):
                    supplier_details.append(f"  • {p_name}: {format_float(buy)}  انت")
                else:
                    supplier_details.append(f"  • {p_name}: {format_float(buy)} جهزه ({p_worker_name})")
                    if buy > 0:
                        others_details[p_worker_name] = others_details.get(p_worker_name, 0.0) + buy
            deduction_total = sum(others_details.values())
            my_amount = total_buy - deduction_total
            supplier_totals[sup_name] = my_amount
            supplier_msg = [
                f"فاتورة الشراء:🧾💸",
                f"👤 المجهز: {sup_name} {sup_username}",
                f"رقم الفاتورة🔢: {invoice}",
                f"عنوان الزبون🏠: {order['title']}",
                f"رقم الزبون📞: {phone_number}",
                f"\n\nتفاصيل الشراء:🗒️💸",
                *supplier_details,
                f"\n💰 مجموع الطلبية: {format_float(total_buy)}"
            ]
            for other_name, amt in others_details.items():
                supplier_msg.append(f"ناقص تجهيز ({other_name}) {format_float(amt)}")
            supplier_msg.append(f"المبلغ الذي دفعته انت = {format_float(my_amount)}")
            supplier_invoice_text = "\n".join(supplier_msg)
            await context.bot.send_message(chat_id=int(s_id), text=supplier_invoice_text)

        # --- أ٢. بناء رسالة الإدارة التفصيلية (كل صنف من جهزه + كل مجهز شكد دفع) ---
        admin_detailed_lines = [
            "فاتورة الشراء (تفاصيل المجهزين):🧾💸",
            f"رقم الفاتورة🔢: {invoice}",
            f"عنوان الزبون🏠: {order['title']}",
            f"رقم الزبون📞: {phone_number}",
            f"\n\nتفاصيل الشراء:🗒️💸",
            *admin_detail_lines,
            f"\n💰 مجموع الطلبية: {format_float(total_buy)}",
            "-----------------------------------",
            "المبلغ الكلي لكل مجهز:"
        ]
        for sup_name, amt in supplier_totals.items():
            admin_detailed_lines.append(f"  • {sup_name}: {format_float(amt)} دينار 💸")
        admin_detailed_text = "\n".join(admin_detailed_lines)

        # --- فاتورة السمك لوحد (تُرسل للخاص): كل منتج سمك + من جهزه ---
        fish_lines = []
        for p_name in order["products"]:
            if not is_fish(p_name):
                continue
            data = pricing.get(order_id, {}).get(p_name, {})
            buy = float(data.get("buy", 0.0))
            p_worker_name = data.get("prepared_by_name", "شخص آخر")
            fish_lines.append(f"  • {p_name}: {format_float(buy)} — جهزه ({p_worker_name})")
        if fish_lines:
            fish_invoice_text = (
                "🐟 فاتورة السمك (تفصيل):🧾\n"
                f"رقم الفاتورة🔢: {invoice}\n"
                f"عنوان الزبون🏠: {order['title']}\n"
                f"رقم الزبون📞: {phone_number}\n\n"
                "تفاصيل السمك:\n"
                + "\n".join(fish_lines) +
                f"\n\n💰 مجموع السمك: {format_float(sum(float(pricing.get(order_id, {}).get(p, {}).get('buy', 0)) for p in order['products'] if is_fish(p)))}"
            )
        else:
            fish_invoice_text = None

        # --- فاتورة الخضروات والفواكه لوحد (تُرسل للخاص) ---
        veg_lines = []
        for p_name in order["products"]:
            if not is_vegetable_fruit(p_name):
                continue
            data = pricing.get(order_id, {}).get(p_name, {})
            buy = float(data.get("buy", 0.0))
            p_worker_name = data.get("prepared_by_name", "شخص آخر")
            veg_lines.append(f"  • {p_name}: {format_float(buy)} — جهزه ({p_worker_name})")
        if veg_lines:
            veg_invoice_text = (
                "🥬 فاتورة الخضروات والفواكه:🧾\n"
                f"رقم الفاتورة🔢: {invoice}\n"
                f"عنوان الزبون🏠: {order['title']}\n"
                f"رقم الزبون📞: {phone_number}\n\n"
                "تفاصيل الخضروات/الفواكه:\n"
                + "\n".join(veg_lines) +
                f"\n\n💰 مجموع الخضروات/الفواكه: {format_float(sum(float(pricing.get(order_id, {}).get(p, {}).get('buy', 0)) for p in order['products'] if is_vegetable_fruit(p)))}"
            )
        else:
            veg_invoice_text = None

        # --- فاتورة اللحم لوحد (تُرسل للخاص) ---
        meat_lines = []
        for p_name in order["products"]:
            if not is_meat(p_name):
                continue
            data = pricing.get(order_id, {}).get(p_name, {})
            buy = float(data.get("buy", 0.0))
            p_worker_name = data.get("prepared_by_name", "شخص آخر")
            meat_lines.append(f"  • {p_name}: {format_float(buy)} — جهزه ({p_worker_name})")
        if meat_lines:
            meat_invoice_text = (
                "🥩 فاتورة اللحم (تفصيل):🧾\n"
                f"رقم الفاتورة🔢: {invoice}\n"
                f"عنوان الزبون🏠: {order['title']}\n"
                f"رقم الزبون📞: {phone_number}\n\n"
                "تفاصيل اللحم:\n"
                + "\n".join(meat_lines) +
                f"\n\n💰 مجموع اللحم: {format_float(sum(float(pricing.get(order_id, {}).get(p, {}).get('buy', 0)) for p in order['products'] if is_meat(p)))}"
            )
        else:
            meat_invoice_text = None

        # --- ب. بناء فاتورة الإدارة (للمدير فقط) ---
        admin_msg = [
            f"فاتورة الإدارة:👨🏻‍💼",
            f"👤 المجهز: {current_name}",
            f"رقم الفاتورة🔢: {invoice}",
            f"رقم الزبون📞: {phone_number}",
            f"\nعنوان الزبون🏠: {order['title']}",
            f"\nتفاصيل الطلبية:🗒",
            *admin_details,
            f"\nإجمالي الشراء:💸 {format_float(total_buy)}",
            f"إجمالي البيع:💵  {format_float(total_sell)}",
            f"ربح المنتجات:💲 {format_float(total_sell - total_buy)}",
            f"ربح المحلات ({places_count} محل):🏪 {format_float(extra_cost)}",
            f"أجرة التوصيل:🚚 {format_float(delivery)}",
            f"المجموع الكلي:💰 {format_float(grand_total)}"
        ]
        admin_text = "\n".join(admin_msg)

        # --- ج. بناء فاتورة الزبون للجروب (مع الحساب المتسلسل) ---
        # رقم الزبون بصيغة `كود` عشان ينسخ باللمس بدل ما يفتح معلومات الرقم
        safe_phone = (phone_number or "").replace("`", "'")
        customer_lines = [
            "📋 أبو الأكبر للتوصيل 🚀",
            "-----------------------------------",
            f"فاتورة رقم: #{invoice}",
            f"🏠 عنوان الزبون: {order['title']}",
            f"📞 رقم الزبون: `{safe_phone}`",
            "\n🛍️ المنتجات:  \n"
        ]
        
        running_sum = 0.0
        for i, p_name in enumerate(order["products"]):
            p_sell = float(pricing.get(order_id, {}).get(p_name, {}).get("sell", 0.0))
            customer_lines.append(f"– {p_name} بـ{format_float(p_sell)}")
            if i == 0:
                customer_lines.append(f"• {format_float(p_sell)} 💵")
            else:
                customer_lines.append(f"• {format_float(running_sum)}+{format_float(p_sell)}= {format_float(running_sum + p_sell)} 💵")
            running_sum += p_sell

        if extra_cost > 0:
            customer_lines.append(f"– 📦 التجهيز: من {places_count} محلات بـ {format_float(extra_cost)}")
            customer_lines.append(f"• {format_float(running_sum)}+{format_float(extra_cost)}= {format_float(running_sum + extra_cost)} 💵")
            running_sum += extra_cost

        customer_lines.append(f"– 🚚 التوصيل: بـ {format_float(delivery)}")
        customer_lines.append(f"• {format_float(running_sum)}+{format_float(delivery)}= {format_float(running_sum + delivery)} 💵")
        
        customer_lines.extend([
            "-----------------------------------",
            "✨ المجموع الكلي: ✨",
            f"بدون التوصيل = {format_float(total_sell + extra_cost)} 💵",
            f"مــــع التوصيل = {format_float(grand_total)} 💵",
            "شكراً لاختياركم أبو الأكبر للتوصيل! ❤️"
        ])
        customer_text = "\n".join(customer_lines)

        # --- 3. إرسال الرسائل ---
        # (إرسال لكل مجهز تم أعلاه داخل الحلقة)

        # 2. إرسال للجروب (فاتورة الزبون) — Markdown عشان رقم الزبون يظهر كـ code فينسخ باللمس
        await context.bot.send_message(chat_id=chat_id, text=customer_text, parse_mode="Markdown")

        # 3. إرسال لكل المديرين بالخاص: التفصيلية، الأرباح، فاتورة السمك، الخضروات، اللحم، ثم نسخة الجروب
        for owner_id in OWNER_IDS:
            await context.bot.send_message(chat_id=owner_id, text=admin_detailed_text)  # تفاصيل المجهزين + كل مجهز شكد دفع
            await context.bot.send_message(chat_id=owner_id, text=admin_text)           # فاتورة الإدارة (الأرباح) - كما هي
            if fish_invoice_text:
                await context.bot.send_message(chat_id=owner_id, text=fish_invoice_text)  # فاتورة السمك لوحد
            if veg_invoice_text:
                await context.bot.send_message(chat_id=owner_id, text=veg_invoice_text)  # فاتورة الخضروات والفواكه لوحد
            if meat_invoice_text:
                await context.bot.send_message(chat_id=owner_id, text=meat_invoice_text)  # فاتورة اللحم لوحد
            await context.bot.send_message(chat_id=owner_id, text=f"📋 نسخة الجروب:\n\n{customer_text}", parse_mode="Markdown")  # نسخة الجروب

        # تنظيف رسائل المجهز
        if user_id in context.user_data and 'messages_to_delete' in context.user_data[user_id]:
            for msg_info in context.user_data[user_id]['messages_to_delete']:
                context.application.create_task(delete_message_in_background(context, chat_id=msg_info['chat_id'], message_id=msg_info['message_id']))
            context.user_data[user_id]['messages_to_delete'].clear()

        # أزرار التعديل والرفع
        kb = [[InlineKeyboardButton("1️⃣ تعديل الاسعار", callback_data=f"edit_prices_{order_id}")],
              [InlineKeyboardButton("2️⃣ رفع الطلبية", url="https://d.ksebstor.site/client/96f743f604a4baf145939298")]]
        await context.bot.send_message(chat_id=chat_id, text="تمت العملية بنجاح ✅", reply_markup=InlineKeyboardMarkup(kb))

    except Exception as e:
        logger.error(f"Error in show_final_options: {e}")

async def edit_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    
    try:
        query = update.callback_query
        await query.answer()
        
        user_id = str(query.from_user.id)
        logger.info(f"[{query.message.chat_id}] Edit prices callback from user {user_id}: {query.data}")
        
        if query.data.startswith("edit_prices_"):
            order_id = query.data.replace("edit_prices_", "")
        else:
            await query.message.reply_text("زربة الدكمة عطبت.")
            return ConversationHandler.END

        if order_id not in orders:
            await query.message.reply_text("الطلب مموجود.")
            return ConversationHandler.END

        # ✅ تفعيل وضع التعديل وتصفير قائمة المنتجات المعدلة حديثاً
        context.user_data.setdefault(user_id, {})["editing_mode"] = True
        context.user_data[user_id]["edited_products_list"] = []  # قائمة جديدة لتتبع التعديلات

        if query.message:
            context.user_data.setdefault(user_id, {}).setdefault('messages_to_delete', []).append({
                'chat_id': query.message.chat_id,
                'message_id': query.message.message_id
            })
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=query.message.chat_id,
                    message_id=query.message.message_id,
                    reply_markup=None 
                )
            except Exception:
                pass
        
        await show_buttons(query.message.chat_id, context, user_id, order_id, confirmation_message="وضع التعديل: اضغط على المنتج لتغيير سعره، ثم اضغط 'اكتمل التعديل'.")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in edit_prices: {e}", exc_info=True)
        await update.callback_query.message.reply_text("صار خطا بالتعديل.")
        return ConversationHandler.END
        
async def finish_editing_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    chat_id = query.message.chat_id
    order_id = query.data.replace("done_editing_", "")

    logger.info(f"[{chat_id}] Finished editing for order {order_id}. Proceeding to places count.")

    # نلغي وضع التعديل لان خلصنا
    if user_id in context.user_data:
        context.user_data[user_id].pop("editing_mode", None)

    # حذف رسالة الأزرار الحالية حتى لا تبقى معلقة
    try:
        await query.message.delete()
    except Exception as e:
        logger.warning(f"Could not delete message in finish_editing_callback: {e}")

    # الانتقال لطلب عدد المحلات
    await request_places_count_standalone(chat_id, context, user_id, order_id)
    return ConversationHandler.END

async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    order_id = query.data.replace("cancel_edit_", "")
    
    # إزالة وضع التعديل
    if user_id in context.user_data:
        context.user_data[user_id].pop("editing_mode", None)
    
    # حذف رسالة الأزرار القديمة
    try:
        await query.message.delete()
    except Exception as e:
        logger.warning(f"Could not delete edit message: {e}")
    
    # العودة لعرض الفاتورة النهائية
    await show_final_options(query.message.chat_id, context, user_id, order_id, message_prefix="ترا سطرتني عدل الغي عدل الغي لغيتها.")
    return ConversationHandler.END
    

async def start_new_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.from_user.id)
    try:
        query = update.callback_query
        await query.answer()
        
        logger.info(f"[{query.message.chat_id}] Start new order callback from user {user_id}. User data: {json.dumps(context.user_data.get(user_id, {}), indent=2)}")
        if user_id in context.user_data:
            context.user_data[user_id].pop("order_id", None)
            context.user_data[user_id].pop("product", None)
            context.user_data[user_id].pop("current_active_order_id", None)
            context.user_data[user_id].pop("messages_to_delete", None) 
            context.user_data[user_id].pop("buy_price", None) # Clear buy_price too
            logger.info(f"[{query.message.chat_id}] Cleared order-specific user_data for user {user_id} after starting a new order from button. User data after clean: {json.dumps(context.user_data.get(user_id, {}), indent=2)}")

        if query.message:
            context.application.create_task(delete_message_in_background(context, chat_id=query.message.chat_id, message_id=query.message.message_id))

        await query.message.reply_text("تمام، دز الطلبية الجديدة كلها برسالة واحدة.\n\n*السطر الأول:* عنوان الزبون.\n*السطر الثاني:* رقم هاتف الزبون.\n*الأسطر الباقية:* كل منتج بسطر واحد.", parse_mode="Markdown")
        
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"[{update.effective_chat.id}] Error in start_new_order_callback: {e}", exc_info=True)
        await update.callback_query.message.reply_text("😐زربة ماكدرت اسوي طلبية جديده اشو بالله دسوي مره ثانيه علكولتهم حاول من جديد.")
        return ConversationHandler.END

# الدوال الخاصة بالتقارير والأرباح (ستُجزأ لاحقاً إلى features/reports.py)
def _compute_overall_profit(orders: dict, pricing: dict) -> float:
    total_net_profit_products_all_orders = 0.0
    total_extra_profit_all_orders = 0.0

    for order_id, order_data in orders.items():
        order_net_profit_products = 0.0
        if isinstance(order_data.get("products"), list):
            for p_name in order_data["products"]:
                p = pricing.get(order_id, {}).get(p_name, {})
                if "buy" in p and "sell" in p:
                    order_net_profit_products += (p["sell"] - p["buy"])

        num_places = order_data.get("places_count", 0)
        order_extra_profit_single_order = calculate_extra(num_places)

        total_net_profit_products_all_orders += order_net_profit_products
        total_extra_profit_all_orders += order_extra_profit_single_order

    return float(total_net_profit_products_all_orders + total_extra_profit_all_orders)


async def show_profit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data['orders'] # نجيب كل الطلبيات
    pricing = context.application.bot_data['pricing'] # نحتاج الأسعار لحساب الربح

    try:
        if not is_owner(update.message.from_user.id):
            await update.message.reply_text("😏لاتاكل خره ماتكدر تسوي هالشي.")
            return

        overall_cumulative_profit = _compute_overall_profit(orders, pricing)

        logger.info(f"Overall cumulative profit requested by user {update.message.from_user.id}: {overall_cumulative_profit}")
        await update.message.reply_text(f"ربح البيع والتجهيز💵: *{format_float(overall_cumulative_profit)}* دينار", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"[{update.effective_chat.id}] Error in show_profit: {e}", exc_info=True)
        await update.message.reply_text("😐اهووو ماكدرت اطلعلك الارباح")

async def reset_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not is_owner(update.message.from_user.id):
            await update.message.reply_text("😏لاتاكل خره ماتكدر تسوي هالشي.")
            return
        
        keyboard = [
            [InlineKeyboardButton("اي صفر", callback_data="confirm_reset")],
            [InlineKeyboardButton("لاتصفر", callback_data="cancel_reset")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("😏يابه انته متاكد تريد تصفر راجع روحك اخذ خيره مو بعدين دكول لا حرامات ", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"[{update.effective_chat.id}] Error in reset_all: {e}", exc_info=True)
        await update.message.reply_text("😐، هذا الضراط ماكدرت اصفر.")

async def confirm_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    invoice_numbers = context.application.bot_data['invoice_numbers']
    last_button_message = context.application.bot_data['last_button_message']
    daily_profit = context.application.bot_data['daily_profit'] 
    supplier_report_timestamps = context.application.bot_data['supplier_report_timestamps'] # ✅ جبنا هذا المتغير

    try:
        query = update.callback_query
        await query.answer() # ✅ هذا السطر مهم جداً حتى يختفي التحميل من الزر

        if not is_owner(query.from_user.id):
            await query.edit_message_text("😏لاتاكل خره ماتكدر تسوي هالشي.")
            return

        if query.data == "confirm_reset":
            logger.info(f"Daily profit before reset: {daily_profit}")
            
            # تصفير القيم في الذاكرة
            orders.clear()
            pricing.clear()
            invoice_numbers.clear()
            last_button_message.clear()
            supplier_report_timestamps.clear() # ✅ تصفير سجلات المجهزين
            
            daily_profit_value = 0.0 # القيمة الجديدة للربح اليومي

            try:
                # إعادة تعيين عداد الفواتير
                with open(COUNTER_FILE, "w") as f:
                    f.write("1")
            except Exception as e:
                logger.error(f"Could not reset invoice counter file: {e}", exc_info=True)
            
            # تحديث القيم في bot_data بعد التصفير (هذا الجزء مهم)
            context.application.bot_data['orders'] = orders
            context.application.bot_data['pricing'] = pricing
            context.application.bot_data['invoice_numbers'] = invoice_numbers
            context.application.bot_data['last_button_message'] = last_button_message
            context.application.bot_data['daily_profit'] = daily_profit_value
            context.application.bot_data['supplier_report_timestamps'] = supplier_report_timestamps # ✅ تحديث سجل المجهزين في bot_data

            # استدعاء دالة الحفظ العامة لحفظ التغييرات على القرص
            _save_data_to_disk_global_func = context.application.bot_data.get('_save_data_to_disk_global_func')
            if _save_data_to_disk_global_func:
                _save_data_to_disk_global_func()
            else:
                logger.error("Could not find _save_data_to_disk_global_func in bot_data.")
            
            logger.info(f"Daily profit after reset: {context.application.bot_data['daily_profit']}")
            await query.edit_message_text("😒صفرنه ومسحنه عندك شي ثاني.")
        elif query.data == "cancel_reset":
            await query.edit_message_text("😏لغيناها ارتاحيت.")
    except Exception as e:
        logger.error(f"[{update.effective_chat.id}] Error in confirm_reset: {e}", exc_info=True)
        await update.callback_query.message.reply_text("😐، هذا الضراط ماكدرت اصفر.")


def _build_full_report_parts(orders, pricing, invoice_numbers):
    """يُرجع (نص التقرير العام، نص سمك، خضروات، لحم). إذا ماكو طلبات يُرجع تقرير فارغ."""
    total_orders = len(orders)
    if total_orders == 0:
        empty_main = (
            "**--- تقرير عام عن الطلبات 🗒️ ---**\n"
            "📋 *تقرير فارغ* — لا توجد طلبات اليوم.\n"
            "**إجمالي الطلبات:** 0\n"
            "**الربح الكلي الصافي: 0 دينار**"
        )
        return empty_main, "🐟 **فواتير السمك:** لا توجد.", "🥬 **فواتير الخضروات:** لا توجد.", "🥩 **فواتير اللحم:** لا توجد."

    total_products = 0
    total_buy_all_orders = 0.0
    total_sell_all_orders = 0.0
    total_net_profit_all_orders = 0.0
    total_extra_profit_all_orders = 0.0
    details = []

    for order_id, order in orders.items():
        invoice = invoice_numbers.get(order_id, "غير معروف")
        title = order.get("title", "")
        phone = order.get("phone_number", "بدون رقم")
        details.append(f"\n**رقم الطلب:🔢** {invoice}")
        details.append(f"**عنوان الطلب:🏠** {title}")
        details.append(f"**رقم الزبون:📞** `{phone}`")
        details.append("**السلع:**")
        order_buy = 0.0
        order_sell = 0.0
        order_net_profit = 0.0
        if isinstance(order.get("products"), list):
            for p_name in order["products"]:
                total_products += 1
                p_data = pricing.get(order_id, {}).get(p_name, {})
                if "buy" in p_data and "sell" in p_data:
                    buy, sell = p_data["buy"], p_data["sell"]
                    p_worker = p_data.get("prepared_by_name", "غير معروف")
                    profit_item = sell - buy
                    order_buy += buy
                    order_sell += sell
                    order_net_profit += profit_item
                    details.append(
                        f"   - {p_name}\n"
                        f"     *شراء:* {format_float(buy)} | *بيع:* {format_float(sell)} | *ربح:* {format_float(profit_item)}\n"
                        f"     *المجهز:* {p_worker}"
                    )
                else:
                    details.append(f"   - {p_name} | (لم يتم تسعيره: لا يوجد سعر شراء/بيع)")
        else:
            details.append("   - (لا توجد سلع)")
        num_places = order.get("places_count", 0)
        order_extra_profit = calculate_extra(num_places)
        total_buy_all_orders += order_buy
        total_sell_all_orders += order_sell
        total_net_profit_all_orders += order_net_profit
        total_extra_profit_all_orders += order_extra_profit
        details.append(
            f"**ملخص الطلب:** شراء {format_float(order_buy)} | بيع {format_float(order_sell)} | "
            f"ربح منتجات {format_float(order_net_profit)} | ربح محلات {format_float(order_extra_profit)} | "
            f"*ربح كلي* {format_float(order_net_profit + order_extra_profit)}"
        )

    result = (
        f"**--- تقرير عام عن الطلبات🗒️ ---**\n"
        f"**إجمالي الطلبات:** {total_orders}\n"
        f"**صافي ربح المنتجات:** {format_float(total_net_profit_all_orders)}\n"
        f"**ربح المحلات الكلي:** {format_float(total_extra_profit_all_orders)}\n"
        f"**الربح الكلي الصافي: {format_float(total_net_profit_all_orders + total_extra_profit_all_orders)} دينار**\n\n"
        f"**--- تفاصيل الطلبات🗒 ---**\n" + "\n".join(details)
    )
    report_fish = _build_report_fish_text(orders, pricing, invoice_numbers)
    report_veg = _build_report_veg_text(orders, pricing, invoice_numbers)
    report_meat = _build_report_meat_text(orders, pricing, invoice_numbers)
    return result, report_fish, report_veg, report_meat


async def send_scheduled_report(context: ContextTypes.DEFAULT_TYPE):
    """إرسال التقرير التلقائي للخاص (كل مدير) في الوقت المضبوط — حتى لو التقرير فارغ."""
    try:
        orders = context.application.bot_data['orders']
        pricing = context.application.bot_data['pricing']
        invoice_numbers = context.application.bot_data['invoice_numbers']
        result, report_fish, report_veg, report_meat = _build_full_report_parts(orders, pricing, invoice_numbers)
        overall_cumulative_profit = _compute_overall_profit(orders, pricing)
        for owner_id in OWNER_IDS:
            try:
                for chunk_start in range(0, len(result), 4096):
                    await context.bot.send_message(chat_id=owner_id, text=result[chunk_start:chunk_start + 4096], parse_mode="Markdown")
                for chunk_start in range(0, len(report_fish), 4096):
                    await context.bot.send_message(chat_id=owner_id, text=report_fish[chunk_start:chunk_start + 4096], parse_mode="Markdown")
                for chunk_start in range(0, len(report_veg), 4096):
                    await context.bot.send_message(chat_id=owner_id, text=report_veg[chunk_start:chunk_start + 4096], parse_mode="Markdown")
                for chunk_start in range(0, len(report_meat), 4096):
                    await context.bot.send_message(chat_id=owner_id, text=report_meat[chunk_start:chunk_start + 4096], parse_mode="Markdown")
                # بعد التقارير، إرسال رسالة الأرباح مثل أمر "ارباح/ار"
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=f"ربح البيع والتجهيز💵: *{format_float(overall_cumulative_profit)}* دينار",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Error sending scheduled report to owner {owner_id}: {e}")
        logger.info("Scheduled report sent to owners.")
    except Exception as e:
        logger.error(f"Error in send_scheduled_report: {e}", exc_info=True)


async def do_scheduled_reset(context: ContextTypes.DEFAULT_TYPE):
    """تصفير تلقائي في الوقت المضبوط — حتى لو البيانات فارغة، يرسل رسالة للمديرين."""
    try:
        orders = context.application.bot_data['orders']
        pricing = context.application.bot_data['pricing']
        invoice_numbers = context.application.bot_data['invoice_numbers']
        last_button_message = context.application.bot_data['last_button_message']
        supplier_report_timestamps = context.application.bot_data['supplier_report_timestamps']
        _save_data_to_disk_global_func = context.application.bot_data.get('_save_data_to_disk_global_func')

        orders.clear()
        pricing.clear()
        invoice_numbers.clear()
        last_button_message.clear()
        supplier_report_timestamps.clear()
        context.application.bot_data['daily_profit'] = 0.0
        context.application.bot_data['orders'] = orders
        context.application.bot_data['pricing'] = pricing
        context.application.bot_data['invoice_numbers'] = invoice_numbers
        context.application.bot_data['last_button_message'] = last_button_message
        context.application.bot_data['supplier_report_timestamps'] = supplier_report_timestamps

        try:
            with open(COUNTER_FILE, "w") as f:
                f.write("1")
        except Exception as e:
            logger.error(f"Could not reset invoice counter file: {e}", exc_info=True)

        if _save_data_to_disk_global_func:
            _save_data_to_disk_global_func()

        msg = "🔄 تم التصفير التلقائي. (البيانات صُفّرت أو كانت فارغة.)"
        for owner_id in OWNER_IDS:
            try:
                await context.bot.send_message(chat_id=owner_id, text=msg)
            except Exception as e:
                logger.error(f"Error sending scheduled reset msg to owner {owner_id}: {e}")
        logger.info("Scheduled reset done.")
    except Exception as e:
        logger.error(f"Error in do_scheduled_reset: {e}", exc_info=True)


def _build_report_fish_text(orders, pricing, invoice_numbers):
    """بناء نص فواتير السمك (كل الطلبات، منتجات السمك فقط) للتقرير."""
    lines = ["🐟 **فواتير السمك (تقرير)**\n"]
    for order_id, order in orders.items():
        fish_items = [(p_name, pricing.get(order_id, {}).get(p_name, {})) for p_name in order.get("products", []) if is_fish(p_name)]
        if not fish_items:
            continue
        inv = invoice_numbers.get(order_id, "??")
        lines.append(f"فاتورة #{inv} | {order.get('title', '')} | {order.get('phone_number', '')}")
        for p_name, p_data in fish_items:
            buy = p_data.get("buy", 0)
            who = p_data.get("prepared_by_name", "غير معروف")
            lines.append(f"  • {p_name}: {format_float(buy)} — جهزه ({who})")
        lines.append("")
    return "\n".join(lines) if len(lines) > 1 else "ماكو فواتير سمك مسجلة."


def _build_report_veg_text(orders, pricing, invoice_numbers):
    """بناء نص فواتير الخضروات والفواكه للتقرير."""
    lines = ["🥬 **فواتير الخضروات والفواكه (تقرير)**\n"]
    for order_id, order in orders.items():
        veg_items = [(p_name, pricing.get(order_id, {}).get(p_name, {})) for p_name in order.get("products", []) if is_vegetable_fruit(p_name)]
        if not veg_items:
            continue
        inv = invoice_numbers.get(order_id, "??")
        lines.append(f"فاتورة #{inv} | {order.get('title', '')} | {order.get('phone_number', '')}")
        for p_name, p_data in veg_items:
            buy = p_data.get("buy", 0)
            who = p_data.get("prepared_by_name", "غير معروف")
            lines.append(f"  • {p_name}: {format_float(buy)} — جهزه ({who})")
        lines.append("")
    return "\n".join(lines) if len(lines) > 1 else "ماكو فواتير خضروات/فواكه مسجلة."


def _build_report_meat_text(orders, pricing, invoice_numbers):
    """بناء نص فواتير اللحم للتقرير."""
    lines = ["🥩 **فواتير اللحم (تقرير)**\n"]
    for order_id, order in orders.items():
        meat_items = [(p_name, pricing.get(order_id, {}).get(p_name, {})) for p_name in order.get("products", []) if is_meat(p_name)]
        if not meat_items:
            continue
        inv = invoice_numbers.get(order_id, "??")
        lines.append(f"فاتورة #{inv} | {order.get('title', '')} | {order.get('phone_number', '')}")
        for p_name, p_data in meat_items:
            buy = p_data.get("buy", 0)
            who = p_data.get("prepared_by_name", "غير معروف")
            lines.append(f"  • {p_name}: {format_float(buy)} — جهزه ({who})")
        lines.append("")
    return "\n".join(lines) if len(lines) > 1 else "ماكو فواتير لحم مسجلة."


async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    invoice_numbers = context.application.bot_data['invoice_numbers']

    try:
        if not is_owner(update.message.from_user.id):
            await update.message.reply_text("لاتاكل خره هذا الامر للمدير افتهمت لولا.")
            return

        # بناء التقرير (فارغ أو فيه بيانات) وإرساله للخاص
        result, report_fish, report_veg, report_meat = _build_full_report_parts(orders, pricing, invoice_numbers)

        for owner_id in OWNER_IDS:
            try:
                for chunk_start in range(0, len(result), 4096):
                    await context.bot.send_message(chat_id=owner_id, text=result[chunk_start:chunk_start + 4096], parse_mode="Markdown")
                for chunk_start in range(0, len(report_fish), 4096):
                    await context.bot.send_message(chat_id=owner_id, text=report_fish[chunk_start:chunk_start + 4096], parse_mode="Markdown")
                for chunk_start in range(0, len(report_veg), 4096):
                    await context.bot.send_message(chat_id=owner_id, text=report_veg[chunk_start:chunk_start + 4096], parse_mode="Markdown")
                for chunk_start in range(0, len(report_meat), 4096):
                    await context.bot.send_message(chat_id=owner_id, text=report_meat[chunk_start:chunk_start + 4096], parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Error sending report to owner {owner_id}: {e}")

        await update.message.reply_text("✅ تم إرسال التقرير وفواتير السمك والخضروات واللحم للخاص (الإدارة).")
    except Exception as e:
        logger.error(f"Error in show_report: {e}", exc_info=True)
        await update.message.reply_text("😐 صار خطأ بالتقرير.")

async def show_all_purchase_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data.get('orders', {})
    pricing = context.application.bot_data.get('pricing', {})
    
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("😏 لاتاكل خره، هذا الأمر للمدير بس.")
        return

    if not orders:
        await update.message.reply_text("ماكو أي طلبيات مسجلة حالياً.")
        return

    # تحديد كل المجهزين الذين شاركوا في العمل
    all_suppliers = set()
    for oid in pricing:
        for p_name in pricing[oid]:
            s_id = pricing[oid][p_name].get("prepared_by_id")
            if s_id:
                all_suppliers.add(s_id)

    if not all_suppliers:
        await update.message.reply_text("ماكو بيانات مجهزين مسجلة بالأسعار.")
        return

    for s_id in all_suppliers:
        supplier_name = "مجهز غير معروف"
        supplier_username = "لا يوجد"
        try:
            supplier_chat = await context.bot.get_chat(int(s_id))
            supplier_name = supplier_chat.first_name
            if supplier_chat.username:
                supplier_username = f"@{supplier_chat.username}"
        except: pass

        report_msg = f"📦 <b>تقرير فواتير المجهز</b>\n"
        report_msg += f"👤 <b>الاسم:</b> {supplier_name}\n"
        report_msg += f"🆔 <b>الايدي:</b> <code>{s_id}</code>\n"
        report_msg += f"🔗 <b>اليوزر:</b> {supplier_username}\n"
        report_msg += "-----------------------------------\n"
        
        grand_total_for_this_supplier = 0.0
        has_data = False

        for oid, order_data in orders.items():
            invoice_no = context.application.bot_data.get('invoice_numbers', {}).get(oid, '??')
            order_total_buy = 0.0
            order_others_deduction = 0.0
            others_details = {} # { "اسم الشخص": مبلغ }
            items_text = ""
            order_has_supplier_items = False

            # فحص كل منتج في الطلبية
            for p_name in order_data.get('products', []):
                p_info = pricing.get(oid, {}).get(p_name, {})
                buy_price = p_info.get('buy', 0.0)
                p_worker_id = str(p_info.get('prepared_by_id', ''))
                p_worker_name = p_info.get('prepared_by_name', 'شخص آخر')

                if buy_price > 0:
                    order_total_buy += buy_price
                    # إذا كان المنتج لهذا المجهز
                    if p_worker_id == str(s_id):
                        items_text += f"   • {p_name}: {format_float(buy_price)}\n"
                        order_has_supplier_items = True
                    else:
                        # إذا كان لغيره
                        items_text += f"   • {p_name}: {format_float(buy_price)} جهزه ({p_worker_name})\n"
                        order_others_deduction += buy_price
                        others_details[p_worker_name] = others_details.get(p_worker_name, 0.0) + buy_price

            if order_has_supplier_items:
                has_data = True
                report_msg += f"🧾 <b>فاتورة:</b> #{invoice_no} | 🏠 {order_data['title']}\n"
                report_msg += items_text
                report_msg += f"💰 مجموع الطلبية: {format_float(order_total_buy)}\n"
                
                final_net = order_total_buy
                if order_others_deduction > 0:
                    for name, amt in others_details.items():
                        report_msg += f"ناقص تجهيز ({name}) {format_float(amt)} = {format_float(order_total_buy - amt)}\n"
                        final_net -= amt
                
                report_msg += "--- --- ---\n"
                grand_total_for_this_supplier += final_net

        if has_data:
            report_msg += f"\n✅ <b>المجموع الكلي للمجهز:</b> {format_float(grand_total_for_this_supplier)} دينار 💸"
            await update.message.reply_text(report_msg, parse_mode="HTML")
            

async def clear_chat_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    
    # التأكد أن الشخص من المديرين فقط
    if not is_owner(user_id):
        await update.message.reply_text("😏 لاتاكل خره، بس المالك يكدر ينظف الجات.")
        return

    # رسالة تنبيه قبل البدء
    status_msg = await update.message.reply_text("جاري تنظيف الكروب... اصبرلي ثواني 🧹")
    current_msg_id = update.message.message_id

    # راح يحاول يمسح آخر 500 رسالة (تكدر تزيد الرقم إذا تريد)
    deleted_count = 0
    for i in range(current_msg_id, current_msg_id - 500, -1):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=i)
            deleted_count += 1
        except Exception:
            # إذا الرسالة قديمة أو ممسوحة أصلاً، يعبرها
            continue

    # إرسال تأكيد نهائي
    await context.bot.send_message(chat_id=chat_id, text=f"تم تنظيف الجات بنجاح! ✨\nتم مسح {deleted_count} رسالة.")
    
        
        
def main():
    app = ApplicationBuilder().token(TOKEN).defaults(Defaults(tzinfo=BOT_TZ)).build()

    # تهيئة البيانات في Bot Data
    app.bot_data['orders'] = orders
    app.bot_data['pricing'] = pricing
    app.bot_data['invoice_numbers'] = invoice_numbers
    app.bot_data['daily_profit'] = daily_profit
    app.bot_data['last_button_message'] = last_button_message
    app.bot_data['supplier_report_timestamps'] = supplier_report_timestamps
    app.bot_data['schedule_save_global_func'] = schedule_save_global
    app.bot_data['_save_data_to_disk_global_func'] = _save_data_to_disk_global

    # ⏰ جدولة التقرير والتصفير التلقائي (الوقت في main.py أعلى: REPORT_DAILY_*, RESET_DAILY_*)
    _schedule_daily_with_catchup(app, send_scheduled_report, REPORT_DAILY_HOUR, REPORT_DAILY_MINUTE, "daily_report")
    _schedule_daily_with_catchup(app, do_scheduled_reset, RESET_DAILY_HOUR, RESET_DAILY_MINUTE, "daily_reset")
    logger.info(
        f"Scheduled (tz={BOT_TZ.key}): report at {REPORT_DAILY_HOUR}:{REPORT_DAILY_MINUTE:02d}, "
        f"reset at {RESET_DAILY_HOUR}:{RESET_DAILY_MINUTE:02d}"
    )

    # 0. قائمة الأوامر (اوامر / قائمة / مساعدة)
    app.add_handler(CommandHandler("help", show_commands_list))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(اوامر|الاوامر|الأوامر|قائمة|القائمة|مساعدة|المساعدة)$"), show_commands_list))

    # 1. أوامر التحكم الأساسية
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profit", show_profit))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(الارباح|ارباح|ار)$"), show_profit))
    app.add_handler(CommandHandler("reset", reset_all))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(تصفير|صفر|تص|صف)$"), reset_all))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^صفر$"), reset_supplier_report))
    app.add_handler(CallbackQueryHandler(confirm_reset, pattern="^(confirm_reset|cancel_reset)$"))
    app.add_handler(CallbackQueryHandler(handle_region_suggestion_callback, pattern=r"^(pick_zone_|reject_region_)"))

    # 2. أوامر التقارير (المدير والمجهز)
    app.add_handler(CommandHandler("report", show_report))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(التقارير|تقرير|تقارير|تق)$"), show_report))
    app.add_handler(CommandHandler("myreport", show_supplier_report))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(تقاريري|تقريري)$"), show_supplier_report))
    
    # 3. تقارير الشراء (المجهزين) - دعم كل الكلمات
    app.add_handler(CommandHandler("purchase_reports", show_all_purchase_reports))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(تقرير الشراء|تقرير شراء|تقارير شراء|تقارير الشراء|تقارير المجهزين|تقرير المجهزين|تقرير مجهزين|تقارير مجهزين)$"), show_all_purchase_reports))

    # 4. أوامر التنظيف (مسح الكل)
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(ح ك|حك|حذف ك|حذف كل|حذف الكل|م ك|مك|م س|مسح كل|مسح الكل)$"), clear_chat_messages))

    # 5. أوامر المناطق والتعديل
    app.add_handler(CommandHandler("zones", list_zones))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(مناطق|المناطق)$"), list_zones))
    app.add_handler(CallbackQueryHandler(cancel_edit, pattern=r"^cancel_edit_.*$"))
    app.add_handler(CallbackQueryHandler(edit_prices, pattern=r"^edit_prices_"))
    app.add_handler(CallbackQueryHandler(finish_editing_callback, pattern=r"^done_editing_"))
    app.add_handler(CallbackQueryHandler(start_new_order_callback, pattern=r"^start_new_order$"))

    # 6. الطلبات غير المكتملة
    app.add_handler(CommandHandler("incomplete", show_incomplete_orders))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(طلبات|الطلبات|طلبات غير مكتملة|طلبات ناقصة)$"), show_incomplete_orders))
    app.add_handler(CallbackQueryHandler(handle_incomplete_order_selection, pattern=r"^(load_incomplete_|cancel_incomplete)"))

    # 7. معالجة الرسائل المعدلة
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, edited_message))

    # 8. ConversationHandler لعدد المحلات
    places_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_places_count_data, pattern=r"^places_data_[a-f0-9]{8}_\d+$")],
        states={
            ASK_PLACES_COUNT: [
                MessageHandler(filters.TEXT & filters.Regex(r"^\d+(\.\d+)?$") & ~filters.COMMAND, handle_places_count_data),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_places_count_data),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )
    app.add_handler(places_conv_handler)

    # 9. ConversationHandler لمسح طلبية معينة (أمر مسح)
    delete_order_conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & filters.Regex(r"^(مسح)$"), delete_order_command),
            CommandHandler("delete_order", delete_order_command),
        ],
        states={
            ASK_CUSTOMER_PHONE_NUMBER_FOR_DELETION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_customer_phone_for_deletion)],
            ASK_FOR_DELETION_CONFIRMATION: [
                CallbackQueryHandler(handle_order_selection_for_deletion, pattern=r"^(select_order_to_delete_.*|confirm_final_delete_.*|cancel_delete_order|cancel_delete_order_final_selection)$")
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )
    app.add_handler(delete_order_conv_handler)

    # 10. ConversationHandler للطلبات والتسعير (المدخل الرئيسي)
    order_creation_conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order),
            CallbackQueryHandler(product_selected, pattern=r"^[a-f0-9]{8}\|\d+$"),
            CallbackQueryHandler(add_new_product_callback, pattern=r"^add_product_to_order_.*$"),
            CallbackQueryHandler(delete_product_callback, pattern=r"^delete_specific_product_.*$"), 
            CallbackQueryHandler(confirm_delete_product_by_button_callback, pattern=r"^confirm_delete_idx_.*$"), 
            CallbackQueryHandler(cancel_delete_product_callback, pattern=r"^cancel_delete_product_.*$")
        ],
        states={
            ASK_BUY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_buy_price),
                CallbackQueryHandler(cancel_price_entry_callback, pattern="^cancel_price_entry$")
            ],
            ASK_PRODUCT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_product_name),
                CallbackQueryHandler(cancel_add_product_callback, pattern=r"^cancel_add_product_.*$")
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )
    app.add_handler(order_creation_conv_handler)

    # تشغيل البوت
    app.run_polling(allowed_updates=Update.ALL_TYPES)
   

async def show_supplier_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    supplier_report_timestamps = context.application.bot_data['supplier_report_timestamps']

    user_id = str(update.message.from_user.id)
    report_text = f"**تقرير الطلبيات اللي جهزتها يا بطل:**\n\n"
    has_orders = False
    total_purchases_all_orders = 0.0 # ✅ متغير جديد لمجموع المشتريات الكلي للمجهز

    # جلب آخر وقت تصفير لهذا المجهز (إذا موجود)
    last_reset_timestamp_str = supplier_report_timestamps.get(user_id)
    last_reset_datetime = None
    if last_reset_timestamp_str:
        try:
            # تحويل الـ timestamp من string الى datetime object
            last_reset_datetime = datetime.fromisoformat(last_reset_timestamp_str)
            logger.info(f"[{update.effective_chat.id}] Last report reset for supplier {user_id} was at: {last_reset_datetime}")
        except ValueError as e:
            logger.error(f"[{update.effective_chat.id}] Error parsing last_reset_timestamp_str '{last_reset_timestamp_str}': {e}")
            last_reset_datetime = None # إذا صار خطأ بالتحويل، نعتبر ماكو وقت تصفير

    for order_id, order in orders.items():
        if order.get("supplier_id") == user_id:
            order_created_at_str = order.get("created_at")
            if last_reset_datetime and order_created_at_str:
                try:
                    order_created_datetime = datetime.fromisoformat(order_created_at_str)
                    if order_created_datetime <= last_reset_datetime:
                        continue
                except ValueError as e:
                    logger.error(f"[{update.effective_chat.id}] Error parsing order_created_at_str '{order_created_at_str}' for order {order_id}: {e}")

            has_orders = True
            report_text += f"▪️ *عنوان الزبون:🏠 * {order['title']}\n"
            report_text += f"   *رقم الزبون:📞* `{order.get('phone_number', 'لا يوجد رقم')}`\n"

            order_buy_total = 0.0

            report_text += "   *المنتجات (سعر الشراء افتهمت لولا):💸*\n"
            for p_name in order["products"]:
                if p_name in pricing.get(order_id, {}) and "buy" in pricing[order_id].get(p_name, {}):
                    buy_price = pricing[order_id][p_name]["buy"]
                    order_buy_total += buy_price
                    report_text += f"     - {p_name}: {format_float(buy_price)}\n"
                else:
                    report_text += f"     - {p_name}: (لم يتم تسعيره)\n"

            report_text += f"   *مجموع الشراء لهذه الطلبية:💸* {format_float(order_buy_total)}\n\n"
            total_purchases_all_orders += order_buy_total # ✅ جمع مشتريات هاي الطلبية للمجموع الكلي

    if not has_orders:
        report_text = "🖕🏻ماكو أي طلبية جديدة مسجلة باسمك بعد آخر تصفير."
    else: # ✅ إذا جان اكو طلبيات، نضيف المجموع الكلي للمشتريات بنهاية التقرير
        report_text += f"**💰 مجموع مشترياتك الكلي: {format_float(total_purchases_all_orders)} دينار💸**"

    await update.message.reply_text(report_text, parse_mode="Markdown")

async def reset_supplier_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    supplier_report_timestamps = context.application.bot_data['supplier_report_timestamps']
    schedule_save_global = context.application.bot_data['schedule_save_global_func']

    user_id = str(update.message.from_user.id)
    
    # نسجل الوقت الحالي كـ آخر وقت تصفير لهذا المجهز
    now_iso = datetime.now(timezone.utc).isoformat()
    supplier_report_timestamps[user_id] = now_iso
    
    # نحفظ التغييرات
    schedule_save_global()
    logger.info(f"[{update.effective_chat.id}] Supplier report for user {user_id} reset to {now_iso}.")

    await update.message.reply_text("📬تم تصفير تقاريرك بنجاح. أي طلبية جديدة تجهزها من الآن راح تظهر بالتقرير القادم.")

async def delete_order_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    chat_id = update.effective_chat.id

    if not is_owner(user_id):
        await update.message.reply_text("😏لاتاكل خره ماتكدر تسوي هالشي. هذا الأمر متاح للمالك فقط.")
        return ConversationHandler.END

    await update.message.reply_text("تمام، دزلي رقم الزبون للطلبية اللي تريد تمسحها:")
    context.user_data[user_id] = {"deleting_order": True}  # إعادة تهيئة user_data
    return ASK_CUSTOMER_PHONE_NUMBER_FOR_DELETION

async def receive_customer_phone_for_deletion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    chat_id = update.effective_chat.id
    customer_phone_number = update.message.text.strip()

    logger.info(f"[{chat_id}] Received phone number '{customer_phone_number}' for order deletion from user {user_id}.")

    # تأكد من أن المستخدم من المديرين
    if not is_owner(user_id):
        await update.message.reply_text("😏لاتاكل خره ماتكدر تسوي هالشي. هذا الأمر متاح للمالك فقط.")
        context.user_data[user_id].pop("deleting_order", None)
        return ConversationHandler.END

    # البحث عن جميع الطلبات لهذا الرقم سواء مكتملة أو غير مكتملة
    found_orders = {oid: o for oid, o in orders.items() if o.get("phone_number") == customer_phone_number}

    if not found_orders:
        await update.message.reply_text("ما لكييت أي طلبية لهذا الرقم.")
        context.user_data[user_id].pop("deleting_order", None)
        return ConversationHandler.END

    orders_list_details = []
    keyboard_buttons = []

    # ترتيب الطلبات حسب تاريخ الإنشاء (الأحدث أولاً)
    sorted_orders_items = sorted(found_orders.items(), key=lambda item: item[1].get('created_at', ''), reverse=True)

    # حفظ الطلبيات المطابقة في user_data ليتعامل معها handle_order_selection_for_deletion
    context.user_data[user_id]["matching_order_ids"] = [oid for oid, _ in sorted_orders_items]

    for i, (oid, order_data) in enumerate(sorted_orders_items):
        invoice = invoice_numbers.get(oid, "غير معروف")
        is_priced = all(p in pricing.get(oid, {}) and 'buy' in pricing[oid].get(p, {}) and 'sell' in pricing[oid].get(p, {}) for p in order_data.get("products", []))
        status = "مكتملة التسعير" if is_priced else "غير مكتملة التسعير"

        orders_list_details.append(
            f"🔹 *الفاتورة رقم #{invoice}* ({status})\n"
            f"    العنوان: {order_data.get('title', 'غير متوفر')}\n"
            f"    المنتجات: {', '.join(order_data.get('products', []))}"
        )
        # هنا سنستخدم "select_order_to_delete_{order_id}" مباشرة
        # وستقوم دالة handle_order_selection_for_deletion بتأكيد الحذف
        keyboard_buttons.append(
            [InlineKeyboardButton(f"مسح الفاتورة #{invoice} ({status})", callback_data=f"select_order_to_delete_{oid}")]
        )

    keyboard_buttons.append([InlineKeyboardButton("❌ إلغاء العملية", callback_data="cancel_delete_order")])

    await update.message.reply_text(
        f"تم العثور على {len(found_orders)} طلبية لهذا الرقم:\n\n" +
        "\n\n".join(orders_list_details) +
        "\n\nاختر الفاتورة التي تريد مسحها:",
        reply_markup=InlineKeyboardMarkup(keyboard_buttons),
        parse_mode="Markdown"
    )
    return ASK_FOR_DELETION_CONFIRMATION

async def handle_order_selection_for_deletion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    chat_id = query.message.chat_id
    data = query.data

    if not is_owner(user_id):
        await query.edit_message_text("عذراً، لا تملك صلاحية لتنفيذ هذا الأمر.")
        context.user_data[user_id].pop("deleting_order", None)
        return ConversationHandler.END

    # إذا ضغط المستخدم على زر إلغاء العملية
    if data == "cancel_delete_order":
        await query.edit_message_text("تم إلغاء عملية مسح الطلبية.")
        context.user_data[user_id].pop("deleting_order", None)
        context.user_data[user_id].pop("matching_order_ids", None)
        return ConversationHandler.END

    # إذا ضغط المستخدم على زر اختيار طلبية من القائمة
    if data.startswith("select_order_to_delete_"):
        order_id_to_confirm = data.replace("select_order_to_delete_", "")
        
        if order_id_to_confirm not in orders:
            await query.edit_message_text("الطلبية غير موجودة أو تم حذفها مسبقاً.")
            context.user_data[user_id].pop("deleting_order", None)
            context.user_data[user_id].pop("matching_order_ids", None)
            return ConversationHandler.END

        # حفظ order_id للتأكيد النهائي
        context.user_data[user_id]["order_id_to_delete_final"] = order_id_to_confirm

        invoice_num = invoice_numbers.get(order_id_to_confirm, "غير معروف")
        confirm_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ نعم، امسحها", callback_data=f"confirm_final_delete_{order_id_to_confirm}")],
            [InlineKeyboardButton("❌ لا، بطلت", callback_data="cancel_delete_order_final_selection")] # زر إلغاء بعد الاختيار
        ])
        await query.edit_message_text(
            f"هل أنت متأكد من مسح الفاتورة رقم `{invoice_num}`؟ هذا الإجراء لا يمكن التراجع عنه.",
            reply_markup=confirm_keyboard,
            parse_mode="Markdown"
        )
        return ASK_FOR_DELETION_CONFIRMATION # البقاء في نفس الحالة لانتظار التأكيد النهائي

    # إذا ضغط المستخدم على زر التأكيد النهائي للحذف
    if data.startswith("confirm_final_delete_"):
        order_id_to_delete = data.replace("confirm_final_delete_", "")

        # تحقق مرة أخرى من order_id_to_delete_final لضمان أننا نمسح الطلب الصحيح
        if context.user_data[user_id].get("order_id_to_delete_final") != order_id_to_delete:
            logger.warning(f"[{chat_id}] Mismatch in order ID for final deletion confirmation. Expected {context.user_data[user_id].get('order_id_to_delete_final')}, got {order_id_to_delete}.")
            await query.edit_message_text("حدث خطأ، الطلبية المحددة للحذف غير مطابقة. الرجاء المحاولة مرة أخرى.")
            context.user_data[user_id].pop("deleting_order", None)
            context.user_data[user_id].pop("matching_order_ids", None)
            context.user_data[user_id].pop("order_id_to_delete_final", None)
            return ConversationHandler.END

        # تنفيذ الحذف
        try:
            invoice_number_to_display = invoice_numbers.get(order_id_to_delete, "غير معروف")
            if order_id_to_delete in orders:
                del orders[order_id_to_delete]
            if order_id_to_delete in pricing:
                del pricing[order_id_to_delete]
            if order_id_to_delete in invoice_numbers:
                del invoice_numbers[order_id_to_delete]
            if order_id_to_delete in last_button_message: # حذف رسالة الزر من السجل إذا كانت موجودة
                del last_button_message[order_id_to_delete]

            context.application.create_task(save_data_in_background(context))

            logger.info(f"[{chat_id}] Order {order_id_to_delete} deleted successfully by user {user_id}.")
            await query.edit_message_text(f"تم مسح الطلبية رقم `{invoice_number_to_display}` بنجاح!")
        except Exception as e:
            logger.error(f"[{chat_id}] Error deleting order {order_id_to_delete}: {e}", exc_info=True)
            await query.edit_message_text("عذراً، صار خطأ أثناء مسح الطلبية.")

        context.user_data[user_id].pop("deleting_order", None)
        context.user_data[user_id].pop("matching_order_ids", None)
        context.user_data[user_id].pop("order_id_to_delete_final", None)
        return ConversationHandler.END
    
    # التعامل مع إلغاء الاختيار النهائي بعد اختيار طلبية
    if data == "cancel_delete_order_final_selection":
        await query.edit_message_text("تم إلغاء عملية مسح الطلبية.")
        context.user_data[user_id].pop("deleting_order", None)
        context.user_data[user_id].pop("matching_order_ids", None)
        context.user_data[user_id].pop("order_id_to_delete_final", None)
        return ConversationHandler.END

    logger.warning(f"[{chat_id}] Unhandled callback_data in handle_order_selection_for_deletion: {data}")
    await query.edit_message_text("خطأ غير متوقع. الرجاء المحاولة مرة أخرى.")
    return ConversationHandler.END

async def show_incomplete_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض الطلبات غير المكتملة على شكل أزرار"""
    try:
        user_id = str(update.effective_user.id)
        chat_id = update.effective_chat.id
        
        # البحث عن الطلبات غير المكتملة
        incomplete_orders = {}
        for order_id, order in orders.items():
            # التحقق إذا كانت الطلبية غير مكتملة (أي منتج لم يتم تسعيره)
            is_complete = True
            for p_name in order.get("products", []):
                if p_name not in pricing.get(order_id, {}) or "buy" not in pricing[order_id].get(p_name, {}) or "sell" not in pricing[order_id].get(p_name, {}):
                    is_complete = False
                    break
            
            if not is_complete:
                incomplete_orders[order_id] = order
        
        if not incomplete_orders:
            await update.message.reply_text("🎉 لا توجد طلبات غير مكتملة حالياً!")
            return
        
        # إنشاء أزرار للطلبات غير المكتملة
        buttons = []
        for order_id, order in incomplete_orders.items():
            title = order.get("title", "بدون عنوان")[:20]  # تقليل طول النص
            phone = order.get("phone_number", "بدون رقم")[-4:]  # آخر 4 أرقام فقط
            buttons.append([InlineKeyboardButton(f"{title} (...{phone})", callback_data=f"load_incomplete_{order_id}")])
        
        # إضافة زر الإلغاء
        buttons.append([InlineKeyboardButton("❌ إلغاء", callback_data="cancel_incomplete")])
        
        markup = InlineKeyboardMarkup(buttons)
        
        await update.message.reply_text(
            f"الطلبات غير المكتملة ({len(incomplete_orders)}):\nاختر طلبية لتحميلها:",
            reply_markup=markup
        )
        
    except Exception as e:
        logger.error(f"Error in show_incomplete_orders: {e}")
        await update.message.reply_text("❌ حدث خطأ في عرض الطلبات غير المكتملة")

async def handle_incomplete_order_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة اختيار طلبية غير مكتملة"""
    # الوصول إلى 'orders' من الذاكرة المشتركة للبوت
    orders = context.application.bot_data.get('orders', {}) 
    
    try:
        query = update.callback_query
        # الإجابة على الكويري أولاً لمنع "انتظار" البوت
        await query.answer() 
        
        if query.data == "cancel_incomplete":
            await query.edit_message_text("تم إلغاء عملية تحميل الطلبات.")
            return
        
        if query.data.startswith("load_incomplete_"):
            order_id = query.data.replace("load_incomplete_", "")
            user_id = str(query.from_user.id)
            chat_id = query.message.chat_id
            
            if order_id not in orders:
                await context.bot.send_message(chat_id=chat_id, text="❌ هذه الطلبية لم تعد موجودة.")
                return
            
            order = orders[order_id]
            
            # حذف رسالة القائمة
            try:
                await query.message.delete()
            except:
                pass
            
            # ✅ التعديل الرئيسي: استخدام الحقول الموجودة فعلاً في الطلب:
            # 1. رقم الزبون موجود في حقل 'phone_number'.
            # 2. العنوان/المنطقة موجود في حقل 'title'.
            customer_number_display = order.get("phone_number", "غير متوفر")
            zone_name_display = order.get("title", "غير متوفرة") 
            
            # ✅ التعديل هنا: استخدام تنسيق `Inline Code` (علامة `) حول رقم الزبون
            confirmation_message = (
                f"تم تحميل الطلبية غير المكتملة:\n"
                f"📞 رقم الزبون: `{customer_number_display}`\n"
                f"📌 عنوان الطلب: *{zone_name_display}*"
            )

            # عرض الطلبية المحددة بأزرارها
            await show_buttons(chat_id, context, user_id, order_id, 
                             confirmation_message=confirmation_message)
            
    except Exception as e:
        logger.error(f"Error in handle_incomplete_order_selection: {e}", exc_info=True)
        # إرسال رسالة خطأ جديدة بدلاً من تعديل رسالة قديمة
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="❌ حدث خطأ في تحميل الطلبية. (تم إرسال الخطأ إلى السجل)."
        )
    
    
if __name__ == "__main__":
    main()
