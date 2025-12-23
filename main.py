import os
import json
import uuid
import time
import asyncio
import logging
import threading
from collections import Counter
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters
)

# ✅ استيراد الدوال الخاصة بالمناطق من الملف الجديد
from features.delivery_zones import (
    list_zones, get_delivery_price
)

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

# جلب التوكن ومعرف المالك من متغيرات البيئة
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_TELEGRAM_ID")) 
OWNER_PHONE_NUMBER = os.getenv("OWNER_TELEGRAM_PHONE_NUMBER", "+9647733921468")

if TOKEN is None:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set.")
if OWNER_ID is None:
    raise ValueError("OWNER_TELEGRAM_ID environment variable not set.")

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

# تحميل ملف المناطق واسعارها 
def load_delivery_zones():
    try:
        with open("data/delivery_zones.json", "r") as f:
            zones = json.load(f)
            return zones
    except Exception as e:
        print(f"Error loading delivery zones: {e}")
        return {}
        # استخراج سعر التوصيل بناءً على العنوان
def get_delivery_price(address):
    delivery_zones = load_delivery_zones()
    for zone, price in delivery_zones.items():
        if zone in address:
            return price
    return 0  # إذا لم يتم العثور على العنوان في المناطق

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
        "أهلاً بك يا أبا الأكبر! لإعداد طلبية، دز الطلبية كلها برسالة واحدة.\n\n*السطر الأول:* عنوان الزبون.\n*السطر الثاني:* رقم هاتف الزبون.\n*الأسطر الباقية:* كل منتج بسطر واحد.", 
        parse_mode="Markdown",
        reply_markup=markup
    )
    return ConversationHandler.END

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

async def process_order(update, context, message, edited=False):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    invoice_numbers = context.application.bot_data['invoice_numbers']
    last_button_message = context.application.bot_data['last_button_message']
    
    user_id = str(message.from_user.id)
    lines = [line.strip() for line in message.text.strip().split('\n') if line.strip()]
    
    # ✅ تعديل التحقق من عدد الأسطر: الآن نتوقع 3 أسطر على الأقل (عنوان، رقم هاتف، منتجات)
    if len(lines) < 3:
        if not edited:
            await message.reply_text("باعلي تاكد انك تكتب الطلبية ك التالي اول سطر هو عنوان الزبون وثاني سطر هو رقم الزبون وراها المنتجات كل سطر بي منتج يالله فر ويلك وسوي الطلب.")
        return

    title = lines[0]
    
    # ✅ منطق جديد لمعالجة رقم الهاتف
    phone_number_raw = lines[1].strip().replace(" ", "") # إزالة المسافات
    if phone_number_raw.startswith("+964"):
        phone_number = "0" + phone_number_raw[4:] # استبدال +964 بـ 0
    else:
        phone_number = phone_number_raw.replace("+", "") # إذا ماكو +964، بس نضمن إزالة أي علامة +
    
    products = [p.strip() for p in lines[2:] if p.strip()] # ✅ المنتجات تبدأ من السطر الثالث

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
    
    # ✅ تعديل رسالة الاستلام لتضمين رقم الهاتف بالشكل الجديد
    if is_new_order:
        await message.reply_text(f"طلب : *{title}*\n(الرقم: `{phone_number}` )\n(عدد المنتجات: {len(products)})", parse_mode="Markdown")
        await show_buttons(message.chat_id, context, user_id, order_id)
    else:
        await show_buttons(message.chat_id, context, user_id, order_id, confirmation_message="دهاك حدثنه الطلب. عيني دخل الاسعار الاستاذ حدث الطلب.")
        
async def show_buttons(chat_id, context, user_id, order_id, confirmation_message=None):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    last_button_message = context.application.bot_data['last_button_message']

    try:
        if order_id not in orders:
            await context.bot.send_message(chat_id=chat_id, text="الطلب مموجود.")
            return

        order = orders[order_id]
        final_buttons_list = []

        # ازرار الاضافة والمسح
        final_buttons_list.append([
            InlineKeyboardButton("➕ إضافة منتج", callback_data=f"add_product_to_order_{order_id}"),
            InlineKeyboardButton("🗑️ مسح منتج", callback_data=f"delete_specific_product_{order_id}")
        ])

        completed_products_buttons = []
        pending_products_buttons = []

        # جلب قائمة المنتجات المعدلة حالياً
        edited_list = context.user_data.get(user_id, {}).get("edited_products_list", [])

        for i, p_name in enumerate(order["products"]):
            callback_data_for_product = f"{order_id}|{i}"
            
            # تحديد شكل الزر (صح، علامة تدوير، او اسم فقط)
            button_text = p_name
            is_priced = p_name in pricing.get(order_id, {}) and 'buy' in pricing[order_id].get(p_name, {})

            if is_priced:
                if p_name in edited_list:
                    button_text = f"✏️✅ {p_name}"  # ✅ العلامة الجديدة للمنتج المعدل
                else:
                    button_text = f"✅ {p_name}"
                completed_products_buttons.append([InlineKeyboardButton(button_text, callback_data=callback_data_for_product)])
            else:
                pending_products_buttons.append([InlineKeyboardButton(button_text, callback_data=callback_data_for_product)])

        final_buttons_list.extend(completed_products_buttons)
        final_buttons_list.extend(pending_products_buttons)

        # زر انتهاء التعديل يظهر فقط في وضع التعديل
        if context.user_data.get(user_id, {}).get("editing_mode", False):
            final_buttons_list.append([
                InlineKeyboardButton("تعديل المحلات🏪", callback_data=f"done_editing_{order_id}")
            ])
            final_buttons_list.append([
                InlineKeyboardButton("اكتمل التعديل💾", callback_data=f"cancel_edit_{order_id}")
            ])

        markup = InlineKeyboardMarkup(final_buttons_list)

        message_text = f"{confirmation_message}\n\n" if confirmation_message else ""
        message_text += f"دوس على منتج واكتب سعره ({order['title']}):"

        # حذف الرسالة القديمة وارسال جديدة
        msg_info = last_button_message.get(order_id)
        if msg_info:
            context.application.create_task(delete_message_in_background(context, chat_id=msg_info["chat_id"], message_id=msg_info["message_id"]))

        msg = await context.bot.send_message(chat_id=chat_id, text=message_text, reply_markup=markup, parse_mode="Markdown")
        last_button_message[order_id] = {"chat_id": chat_id, "message_id": msg.message_id}
        context.application.create_task(save_data_in_background(context)) 

        # تنظيف الرسائل القديمة
        if user_id in context.user_data and 'messages_to_delete' in context.user_data[user_id]:
            for msg_info in context.user_data[user_id]['messages_to_delete']:
                context.application.create_task(delete_message_in_background(context, chat_id=msg_info['chat_id'], message_id=msg_info['message_id']))
            context.user_data[user_id]['messages_to_delete'].clear()
            
    except Exception as e:
        logger.error(f"Error in show_buttons: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="خطأ في عرض الازرار.")
        
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
    user_id = str(update.message.from_user.id)
    chat_id = update.effective_chat.id
    
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    
    try:
        # ✅ الاحتفاظ بذكاء البوت: اذا المجهز دز نص طويل (طلب جديد) بالغلط وهو بوضع السعر
        lines = [line.strip() for line in update.message.text.split('\n') if line.strip()]
        if len(lines) >= 3:
            logger.info(f"[{chat_id}] User {user_id} sent a new order while in ASK_BUY mode. Switching to process_order.")
            if user_id in context.user_data:
                context.user_data[user_id].pop("order_id", None)
                context.user_data[user_id].pop("product", None)
            await process_order(update, context, update.message)
            return ConversationHandler.END

        try:
            await delete_previous_messages(context, user_id)
        except Exception:
            pass

        order_id = context.user_data[user_id].get("order_id")
        product = context.user_data[user_id].get("product")
        
        if not order_id or not product:
            await update.message.reply_text("❌ حدث خطأ، ابدأ من جديد.")
            return ConversationHandler.END

        # 🛑 هنا مسحنا كل القيود، أي واحد يكدر يعدل السعر 🛑

        context.user_data.setdefault(user_id, {}).setdefault('messages_to_delete', []).append({
            'chat_id': update.message.chat_id, 
            'message_id': update.message.message_id
        })
        
        # تحليل السعر
        buy_price_str, sell_price_str = None, None

        if len(lines) == 2:
            buy_price_str, sell_price_str = lines[0], lines[1]
        elif len(lines) == 1:
            parts = [p.strip() for p in lines[0].split() if p.strip()]
            if len(parts) == 2:
                buy_price_str, sell_price_str = parts[0], parts[1]
            elif len(parts) == 1:
                buy_price_str, sell_price_str = parts[0], parts[0]
        
        if not buy_price_str or not sell_price_str:
            msg = await update.message.reply_text("😒 اكتب الاسعار عدل (شراء وبيع).")
            context.user_data[user_id]['messages_to_delete'].append({'chat_id': msg.chat_id, 'message_id': msg.message_id})
            return ASK_BUY

        try:
            buy_price = float(buy_price_str)
            sell_price = float(sell_price_str)
            if buy_price < 0 or sell_price < 0: raise ValueError
        except ValueError:
            msg = await update.message.reply_text("😒 دخل ارقام صحيحة.")
            context.user_data[user_id]['messages_to_delete'].append({'chat_id': msg.chat_id, 'message_id': msg.message_id})
            return ASK_BUY

        # حفظ السعر وتحديث المجهز (مؤقتاً)
        pricing.setdefault(order_id, {}).setdefault(product, {})["buy"] = buy_price
        pricing[order_id][product]["sell"] = sell_price
        
        # نسجل ان هذا المجهز اشتغل ع الطلب (بس مو نهائي)
        orders[order_id]["supplier_id"] = user_id
        
        # منطق وضع التعديل (القلم)
        is_editing = context.user_data.get(user_id, {}).get("editing_mode", False)

        if is_editing:
            if "edited_products_list" not in context.user_data[user_id]:
                context.user_data[user_id]["edited_products_list"] = []
            if product not in context.user_data[user_id]["edited_products_list"]:
                context.user_data[user_id]["edited_products_list"].append(product)

        context.application.create_task(save_data_in_background(context))
        
        context.user_data[user_id].pop("order_id", None)
        context.user_data[user_id].pop("product", None)

        # اذا بوضع التعديل، ارجع اعرض الازرار
        if is_editing:
            logger.info(f"[{chat_id}] Price updated in Edit Mode. Returning to buttons.")
            await show_buttons(chat_id, context, user_id, order_id, confirmation_message=f"تم تعديل سعر '{product}' بنجاح ✅.")
            return ConversationHandler.END

        # اذا مو وضع تعديل (شغل عادي)، نشيك اذا الطلب كمل
        is_order_complete = True
        for p_name in orders[order_id].get("products", []):
            if p_name not in pricing.get(order_id, {}) or 'buy' not in pricing[order_id].get(p_name, {}):
                is_order_complete = False
                break
                
        if is_order_complete:
            # اذا كملت كل المنتجات، نحوله لعدد المحلات
            await request_places_count_standalone(chat_id, context, user_id, order_id)
            return ConversationHandler.END
        else:
            # اذا بعد اكو منتجات، نرجع نعرض الازرار
            await show_buttons(chat_id, context, user_id, order_id, confirmation_message="تم إدخال السعر.")
            return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error in receive_buy_price: {e}", exc_info=True)
        await update.message.reply_text("صار خطا.")
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
    daily_profit_current = context.application.bot_data['daily_profit']

    try:
        logger.info(f"[{chat_id}] Showing final options for order {order_id} to user {user_id}")
        if order_id not in orders:
            logger.warning(f"[{chat_id}] Attempted to show final options for non-existent order_id: {order_id}")
            await context.bot.send_message(chat_id=chat_id, text="😐كسهها الطلب مموجود تريد سوي طلب جديد .")
            return

        order = orders[order_id]
        invoice = invoice_numbers.get(order_id, "غير معروف")
        phone_number = order.get('phone_number', 'ماكو رقم')

        # ✅ جلب معلومات المجهز (الاسم واليوزر)
        supplier_real_id = order.get("supplier_id", user_id)
        supplier_name = "غير معروف"
        supplier_username = ""
        
        try:
            supplier_chat = await context.bot.get_chat(supplier_real_id)
            supplier_name = supplier_chat.full_name
            if supplier_chat.username:
                supplier_username = f"(@{supplier_chat.username})"
            else:
                supplier_username = ""
        except Exception as e:
            logger.warning(f"Could not fetch supplier info: {e}")

        # حساب الأسعار
        total_buy = 0.0
        total_sell = 0.0
        for p_name in order["products"]:
            if p_name in pricing.get(order_id, {}) and "buy" in pricing[order_id].get(p_name, {}) and "sell" in pricing[order_id].get(p_name, {}):
                total_buy += pricing[order_id][p_name]["buy"]
                total_sell += pricing[order_id][p_name]["sell"]

        net_profit_products = total_sell - total_buy
        current_places = order.get("places_count", 0)
        extra_cost_value = calculate_extra(current_places)
        delivery_fee = get_delivery_price(order.get('title', ''))
        original_delivery_fee = delivery_fee 

        # إجمالي الفاتورة
        final_total = total_sell + extra_cost_value + delivery_fee

        # تحديث الربح اليومي
        context.application.bot_data['daily_profit'] = daily_profit_current + net_profit_products + extra_cost_value
        context.application.create_task(save_data_in_background(context))

        # --- فاتورة الزبون ---
        customer_invoice_lines = [
            "📋 أبو الأكبر للتوصيل 🚀",
            "-----------------------------------",
            f"فاتورة رقم: #{invoice}",
            f"🏠 عنوان الزبون: {order['title']}",
            f"📞 رقم الزبون: `{phone_number}`",
            "🛍️ المنتجات:  ",
            ""
        ]

        current_display_total_sum = 0.0
        for i, product_name in enumerate(order["products"]):
            if product_name in pricing.get(order_id, {}) and "sell" in pricing[order_id].get(product_name, {}):
                sell_price = pricing[order_id][product_name]["sell"]
                if i == 0:
                    customer_invoice_lines.append(f"– {product_name} بـ{format_float(sell_price)}")
                    customer_invoice_lines.append(f"• {format_float(sell_price)} 💵")
                else:
                    prev_total_for_display = current_display_total_sum
                    customer_invoice_lines.append(f"– {product_name} بـ{format_float(sell_price)}")
                    customer_invoice_lines.append(f"• {format_float(prev_total_for_display)}+{format_float(sell_price)}= {format_float(prev_total_for_display + sell_price)} 💵")
                current_display_total_sum += sell_price
            else:
                customer_invoice_lines.append(f"– {product_name} (لم يتم تسعيره)")

        if extra_cost_value > 0:
            prev_total_for_display = current_display_total_sum
            customer_invoice_lines.append(f"– 📦 التجهيز: من {current_places} محلات بـ {format_float(extra_cost_value)}")
            customer_invoice_lines.append(f"• {format_float(prev_total_for_display)}+{format_float(extra_cost_value)}= {format_float(prev_total_for_display + extra_cost_value)} 💵")
            current_display_total_sum += extra_cost_value

        display_delivery_fee_customer = original_delivery_fee
        if current_places in [1, 2]:
            display_delivery_fee_customer = original_delivery_fee

        if display_delivery_fee_customer > 0:
            prev_total_for_display = current_display_total_sum
            customer_invoice_lines.append(f"– 🚚 التوصيل: بـ {format_float(display_delivery_fee_customer)}")
            customer_invoice_lines.append(f"• {format_float(prev_total_for_display)}+{format_float(display_delivery_fee_customer)}= {format_float(prev_total_for_display + display_delivery_fee_customer)} 💵")
            current_display_total_sum += display_delivery_fee_customer
        else:
            customer_invoice_lines.append(f"– 🚚 التوصيل: بـ 0")
            customer_invoice_lines.append(f"• {format_float(current_display_total_sum)}+0= {format_float(current_display_total_sum)} 💵")

        customer_invoice_lines.extend([
            "-----------------------------------",
            "✨ المجموع الكلي: ✨",
            f"بدون التوصيل = {format_float(total_sell + extra_cost_value)} 💵",
            f"مــــع التوصيل = {format_float(final_total)} 💵",
            "شكراً لاختياركم أبو الأكبر للتوصيل! ❤️"
        ])

        customer_final_text = "\n".join(customer_invoice_lines)

        try:
            await context.bot.send_message(chat_id=chat_id, text=customer_final_text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"[{chat_id}] Could not send customer invoice: {e}")

        # --- فاتورة المجهز (الشراء) ---
        supplier_invoice = [
            f"**فاتورة الشراء:🧾💸**",
            f"👤 **المجهز:** {supplier_name} {supplier_username}", 
            f"رقم الفاتورة🔢: {invoice}",
            f"عنوان الزبون🏠: {order['title']}",
            f"رقم الزبون📞: `{phone_number}`",
            "\n*تفاصيل الشراء:🗒️💸*"
        ]
        for p_name in order["products"]:
            if p_name in pricing.get(order_id, {}) and "buy" in pricing[order_id][p_name]:
                buy = pricing[order_id][p_name]["buy"]
                supplier_invoice.append(f"  - {p_name}: {format_float(buy)}")
            else:
                supplier_invoice.append(f"  - {p_name}: (ترا ماحددت بيش اشتريت)")
        supplier_invoice.append(f"\n*مجموع كلفة الشراء للطلبية:💸* {format_float(total_buy)}")

        # إرسال للمجهز
        try:
            await context.bot.send_message(chat_id=user_id, text="\n".join(supplier_invoice), parse_mode="Markdown")
        except Exception as e:
            logger.error(f"[{chat_id}] Could not send supplier invoice: {e}")

        # ⭐⭐ إرسال فاتورة الشراء للمدير (مع حل مشكلة عدم الوصول) ⭐⭐
        try:
            # المحاولة الأولى: إرسال مرتب مع تنسيق
            await context.bot.send_message(chat_id=OWNER_ID, text="\n".join(supplier_invoice), parse_mode="Markdown")
        except Exception as e:
            logger.error(f"[{chat_id}] Failed to send markdown invoice to owner: {e}")
            # المحاولة الثانية: إرسال نص عادي (بدون تنسيق) لضمان الوصول
            try:
                # نحذف الرموز اللي ممكن تسبب مشاكل
                clean_text = "\n".join(supplier_invoice).replace('*', '').replace('`', '').replace('_', '-')
                await context.bot.send_message(chat_id=OWNER_ID, text=clean_text)
            except Exception as e2:
                logger.error(f"[{chat_id}] Failed to send plain invoice to owner: {e2}")

        # --- فاتورة الإدارة (الربح) ---
        owner_invoice = [
            f"**فاتورة الإدارة:👨🏻‍💼**",
            f"👤 **المجهز:** {supplier_name}",
            f"رقم الفاتورة🔢: {invoice}",
            f"رقم الزبون📞: `{phone_number}`",
            f"عنوان الزبون🏠: {order['title']}",
            "\n*تفاصيل الطلبية:🗒*"
        ]
        for p_name in order["products"]:
            if p_name in pricing.get(order_id, {}) and "buy" in pricing[order_id][p_name] and "sell" in pricing[order_id][p_name]:
                buy = pricing[order_id][p_name]["buy"]
                sell = pricing[order_id][p_name]["sell"]
                profit = sell - buy
                owner_invoice.append(f"- {p_name}: شراء {format_float(buy)} | بيع {format_float(sell)} | ربح {format_float(profit)}")
            else:
                owner_invoice.append(f"- {p_name}: (غير مسعر)")
        owner_invoice.extend([
            f"\n*إجمالي الشراء:💸* {format_float(total_buy)}",
            f"*إجمالي البيع:💵 * {format_float(total_sell)}",
            f"*ربح المنتجات:💲* {format_float(net_profit_products)}",
            f"*ربح المحلات ({current_places} محل):🏪* {format_float(extra_cost_value)}",
            f"*أجرة التوصيل:🚚* {format_float(delivery_fee)}",
            f"*المجموع الكلي:💰* {format_float(final_total)}"
        ])

        try:
            await context.bot.send_message(chat_id=OWNER_ID, text="\n".join(owner_invoice), parse_mode="Markdown")
        except Exception as e:
            logger.error(f"[{chat_id}] Could not send owner invoice: {e}")

        # إرسال نسخة من فاتورة الزبون للمدير
        try:
            await context.bot.send_message(chat_id=OWNER_ID, text=customer_final_text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"[{chat_id}] Could not send customer invoice to owner: {e}")

        # أزرار التحكم
        from urllib.parse import quote
        encoded_customer_text = quote(customer_final_text, safe='')
        keyboard = [
            [InlineKeyboardButton("1️⃣ تعديل الاسعار", callback_data=f"edit_prices_{order_id}")],
            [InlineKeyboardButton("2️⃣ رفع الطلبية", url="https://d.ksebstor.site/client/96f743f604a4baf145939298")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        message_text = "صلوات كملت 😏!\nدختار من الخيارات ابو العريف :"
        if message_prefix:
            message_text = message_prefix + "\n" + message_text

        await context.bot.send_message(chat_id=chat_id, text=message_text, reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"[{chat_id}] Error in show_final_options: {str(e)}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="😏كسها باعلي ماكدرت ادزلك الفاتورة.")

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
async def show_profit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data['orders'] # نجيب كل الطلبيات
    pricing = context.application.bot_data['pricing'] # نحتاج الأسعار لحساب الربح

    try:
        if str(update.message.from_user.id) != str(OWNER_ID):
            await update.message.reply_text("😏لاتاكل خره ماتكدر تسوي هالشي.")
            return

        total_net_profit_products_all_orders = 0.0 # صافي ربح المنتجات الكلي
        total_extra_profit_all_orders = 0.0 # ربح المحلات الكلي

        for order_id, order_data in orders.items():
            order_net_profit_products = 0.0 # ربح منتجات الطلبية الواحدة
            order_extra_profit_single_order = 0.0 # ربح محلات الطلبية الواحدة

            # حساب ربح المنتجات للطلبية
            if isinstance(order_data.get("products"), list):
                for p_name in order_data["products"]:
                    if p_name in pricing.get(order_id, {}) and "buy" in pricing[order_id].get(p_name, {}) and "sell" in pricing[order_id].get(p_name, {}):
                        buy = pricing[order_id][p_name]["buy"]
                        sell = pricing[order_id][p_name]["sell"]
                        order_net_profit_products += (sell - buy)

            # حساب ربح المحلات للطلبية
            num_places = order_data.get("places_count", 0)
            order_extra_profit_single_order = calculate_extra(num_places) # نستخدم الدالة الموجودة

            total_net_profit_products_all_orders += order_net_profit_products
            total_extra_profit_all_orders += order_extra_profit_single_order

        # مجموع الربح الكلي (منتجات + محلات)
        overall_cumulative_profit = total_net_profit_products_all_orders + total_extra_profit_all_orders

        logger.info(f"Overall cumulative profit requested by user {update.message.from_user.id}: {overall_cumulative_profit}")
        await update.message.reply_text(f"ربح البيع والتجهيز💵: *{format_float(overall_cumulative_profit)}* دينار", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"[{update.effective_chat.id}] Error in show_profit: {e}", exc_info=True)
        await update.message.reply_text("😐كسها ماكدرت اطلعلك الارباح")

async def reset_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if str(update.message.from_user.id) != str(OWNER_ID):
            await update.message.reply_text("😏لاتاكل خره ماتكدر تسوي هالشي.")
            return
        
        keyboard = [
            [InlineKeyboardButton("اي صفر", callback_data="confirm_reset")],
            [InlineKeyboardButton("لا لاتصفر", callback_data="cancel_reset")]
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

        if str(query.from_user.id) != str(OWNER_ID):
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
        
async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data['orders']
    pricing = context.application.bot_data['pricing']
    invoice_numbers = context.application.bot_data['invoice_numbers']
    daily_profit = context.application.bot_data['daily_profit'] # هذا المتغير يمثل الربح التراكمي الكلي

    try:
        if str(update.message.from_user.id) != str(OWNER_ID):
            await update.message.reply_text("لاتاكل خره هذا الامر للمدير افتهمت لولا.")
            return

        total_orders = len(orders)
        total_products = 0
        total_buy_all_orders = 0.0 
        total_sell_all_orders = 0.0 
        total_net_profit_all_orders = 0.0 # ✅ هذا يمثل صافي الربح الكلي لكل الطلبيات
        total_extra_profit_all_orders = 0.0 # ✅ متغير جديد لربح المحلات الكلي
        product_counter = Counter()
        details = []

        for order_id, order in orders.items():
            invoice = invoice_numbers.get(order_id, "غير معروف")
            details.append(f"\n**فاتورة رقم:🔢** {invoice}")
            details.append(f"**عنوان الزبون:🏠** {order['title']}")

            order_buy = 0.0
            order_sell = 0.0
            order_net_profit = 0.0 # صافي ربح الطلبية الواحدة
            order_extra_profit = 0.0 # ربح المحلات للطلبية الواحدة

            if isinstance(order.get("products"), list):
                for p_name in order["products"]:
                    total_products += 1
                    product_counter[p_name] += 1

                    if p_name in pricing.get(order_id, {}) and "buy" in pricing[order_id].get(p_name, {}) and "sell" in pricing[order_id].get(p_name, {}):
                        buy = pricing[order_id][p_name]["buy"]
                        sell = pricing[order_id][p_name]["sell"]
                        profit_item = sell - buy
                        order_buy += buy
                        order_sell += sell
                        order_net_profit += profit_item # نجمع ربح كل منتج
                        details.append(f"  - {p_name} | 👊🏿: {format_float(buy)} | ✊🏻 : {format_float(sell)} | 💲: {format_float(profit_item)}")
                    else:
                        details.append(f"  - {p_name} | (لم يتم تسعيره)")
            else:
                details.append(f"  (لا توجد منتجات محددة لهذا الطلب)")

            # حساب ربح المحلات للطلبية الواحدة
            num_places = order.get("places_count", 0)
            order_extra_profit = calculate_extra(num_places) # نحسب الربح من عدد المحلات

            total_buy_all_orders += order_buy
            total_sell_all_orders += order_sell
            total_net_profit_all_orders += order_net_profit # نجمع صافي ربح الطلبية
            total_extra_profit_all_orders += order_extra_profit # نجمع ربح المحلات الكلي

            details.append(f"  *ربح المنتجات في هذه الطلبية:🛍️💵* {format_float(order_net_profit)}")
            details.append(f"  *ربح المحلات في هذه الطلبية ({num_places} محل):🏪💵* {format_float(order_extra_profit)}")
            details.append(f"  *إجمالي ربح هذه الطلبية:🏪🛍️💵* {format_float(order_net_profit + order_extra_profit)}")


        top_product_str = "لا يوجد"
        if product_counter:
            top_product_name, top_product_count = product_counter.most_common(1)[0]
            top_product_str = f"{top_product_name} ({top_product_count} مرة)"

        result = (
            f"**--- تقرير عام عن الطلبات🗒️ ---**\n"
            f"**إجمالي عدد الطلبات المعالجة:🛍️** {total_orders}\n"
            f"**إجمالي عدد المنتجات المباعة (في الطلبات المعالجة):🛒** {total_products}\n"
            f"**أكثر منتج تم طلبه:🛍️** {top_product_str}\n\n"
            f"**مجموع الشراء الكلي (للمنتجات):💸** {format_float(total_buy_all_orders)}\n"
            f"**مجموع البيع الكلي (للمنتجات):💵 ** {format_float(total_sell_all_orders)}\n" 
            f"**صافي ربح المنتجات الكلي:🛍️💵 ** {format_float(total_net_profit_all_orders)}\n" 
            f"**ربح المحلات الكلي:🏪💵** {format_float(total_extra_profit_all_orders)}\n"
            f"**ربح البيع والتجهيز:🏪🛍️💵** {format_float(total_net_profit_all_orders + total_extra_profit_all_orders)} دينار\n\n"
            f"**--- تفاصيل الطلبات🗒 ---**\n" + "\n".join(details)
        )
        await update.message.reply_text(result, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"[{update.effective_chat.id}] Error in show_report: {e}", exc_info=True)
        await update.message.reply_text("😐هذا الظراط ماكدرت ادزلك التقرير .")

async def show_all_purchase_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders = context.application.bot_data.get('orders', {})
    pricing = context.application.bot_data.get('pricing', {})
    
    # التأكد من هوية المدير
    if str(update.effective_user.id) != str(OWNER_ID):
        await update.message.reply_text("😏 لاتاكل خره، هذا الأمر للمدير بس.")
        return

    if not orders:
        await update.message.reply_text("ماكو أي طلبيات مسجلة حالياً.")
        return

    # تجميع الطلبات حسب المجهز
    supplier_groups = {}
    for order_id, order in orders.items():
        s_id = order.get("supplier_id") or order.get("user_id")
        if s_id:
            if s_id not in supplier_groups:
                supplier_groups[s_id] = []
            supplier_groups[s_id].append((order_id, order))

    if not supplier_groups:
        await update.message.reply_text("ماكو بيانات مجهزين كافية للتقرير.")
        return

    # إرسال رسالة لكل مجهز
    for s_id, supplier_orders in supplier_groups.items():
        supplier_username = "لا يوجد"
        supplier_name = f"مجهز ({s_id})"
        
        try:
            # محاولة جلب الاسم واليوزر من تليكرام
            supplier_chat = await context.bot.get_chat(int(s_id))
            supplier_name = supplier_chat.full_name
            if supplier_chat.username:
                supplier_username = f"@{supplier_chat.username}"
        except Exception:
            pass 

        # استخدمنا هنا تنسيق HTML (<b> و <code>) حتى ما يضرب بسبب علامة (_) باليوزر
        report_msg = f"📦 <b>تقرير فواتير المجهز</b>\n"
        report_msg += f"👤 <b>الاسم:</b> {supplier_name}\n"
        report_msg += f"🆔 <b>الايدي:</b> <code>{s_id}</code>\n"
        report_msg += f"🔗 <b>اليوزر:</b> {supplier_username}\n"
        report_msg += "-----------------------------------\n"
        
        total_supplier_buy = 0.0
        has_priced_items = False

        for oid, order_data in supplier_orders:
            order_buy_sum = 0.0
            invoice_no = context.application.bot_data.get('invoice_numbers', {}).get(oid, '??')
            items_list = ""
            
            for p_name in order_data.get('products', []):
                buy_price = pricing.get(oid, {}).get(p_name, {}).get('buy', 0)
                if buy_price > 0:
                    order_buy_sum += buy_price
                    items_list += f"   • {p_name}: {format_float(buy_price)}\n"
            
            if order_buy_sum > 0:
                report_msg += f"🧾 <b>فاتورة:</b> #{invoice_no} | 🏠 {order_data['title']}\n"
                report_msg += items_list
                report_msg += f"💰 مجموع الطلبية: {format_float(order_buy_sum)}\n"
                report_msg += "--- --- ---\n"
                total_supplier_buy += order_buy_sum
                has_priced_items = True

        if has_priced_items:
            report_msg += f"\n✅ <b>المجموع الكلي للمجهز:</b> {format_float(total_supplier_buy)} دينار 💸"
            # انتبه هنا: غيرنا الـ parse_mode إلى HTML
            await update.message.reply_text(report_msg, parse_mode="HTML")

async def clear_chat_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    
    # التأكد أن الشخص هو صاحب البوت فقط
    if user_id != str(OWNER_ID):
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
    app = ApplicationBuilder().token(TOKEN).build()

    # تهيئة البيانات في Bot Data
    app.bot_data['orders'] = orders
    app.bot_data['pricing'] = pricing
    app.bot_data['invoice_numbers'] = invoice_numbers
    app.bot_data['daily_profit'] = daily_profit
    app.bot_data['last_button_message'] = last_button_message
    app.bot_data['supplier_report_timestamps'] = supplier_report_timestamps
    app.bot_data['schedule_save_global_func'] = schedule_save_global
    app.bot_data['_save_data_to_disk_global_func'] = _save_data_to_disk_global

    # 1. أوامر التحكم الأساسية
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profit", show_profit))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(الارباح|ارباح)$"), show_profit))
    app.add_handler(CommandHandler("reset", reset_all))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^تصفير$"), reset_all))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^صفر$"), reset_supplier_report))
    app.add_handler(CallbackQueryHandler(confirm_reset, pattern="^(confirm_reset|cancel_reset)$"))

    # 2. أوامر التقارير (المدير والمجهز)
    app.add_handler(CommandHandler("report", show_report))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^(التقارير|تقرير|تقارير)$"), show_report))
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

    if user_id != str(OWNER_ID):
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

    # تأكد من أن المستخدم يمتلك صلاحيات المالك
    if user_id != str(OWNER_ID):
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

    if user_id != str(OWNER_ID):
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
    
