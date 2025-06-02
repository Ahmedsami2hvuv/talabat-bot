from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters
)
import uuid
import os
from collections import Counter
import json
import logging
import asyncio
import threading

# تفعيل الـ logging للحصول على تفاصيل الأخطاء والعمليات
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# المسار الثابت لحفظ البيانات داخل وحدة التخزين (Volume)
DATA_DIR = "/mnt/data/"

# أسماء ملفات حفظ البيانات
ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")
PRICING_FILE = os.path.join(DATA_DIR, "pricing.json")
INVOICE_NUMBERS_FILE = os.path.join(DATA_DIR, "invoice_numbers.json")
DAILY_PROFIT_FILE = os.path.join(DATA_DIR, "daily_profit.json")
COUNTER_FILE = os.path.join(DATA_DIR, "invoice_counter.txt")
LAST_BUTTON_MESSAGE_FILE = os.path.join(DATA_DIR, "last_button_message.json")

# تهيئة المتغيرات العامة
orders = {}
pricing = {}
invoice_numbers = {}
daily_profit = 0.0
last_button_message = {}

# متغيرات الحفظ المؤجل
save_timer = None
save_pending = False
save_lock = threading.Lock()

# تحميل البيانات عند بدء تشغيل البوت
def load_data():
    global orders, pricing, invoice_numbers, daily_profit, last_button_message

    os.makedirs(DATA_DIR, exist_ok=True)

    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r") as f:
            try:
                temp_data = json.load(f)
                orders.clear() 
                orders.update(temp_data) 
                orders = {str(k): v for k, v in orders.items()}
            except json.JSONDecodeError:
                orders.clear() 
                logger.warning("orders.json is corrupted or empty, reinitializing.")
            except Exception as e:
                logger.error(f"Error loading orders.json: {e}, reinitializing.")
                orders.clear()

    if os.path.exists(PRICING_FILE):
        with open(PRICING_FILE, "r") as f:
            try:
                temp_data = json.load(f)
                pricing.clear()
                pricing.update(temp_data)
                pricing = {str(pk): pv for pk, pv in pricing.items()}
                for oid in pricing:
                    if isinstance(pricing[oid], dict):
                        pricing[oid] = {str(pk): pv for pk, pv in pricing[oid].items()}
            except json.JSONDecodeError:
                pricing.clear()
                logger.warning("pricing.json is corrupted or empty, reinitializing.")
            except Exception as e:
                logger.error(f"Error loading pricing.json: {e}, reinitializing.")
                pricing.clear()

    if os.path.exists(INVOICE_NUMBERS_FILE):
        with open(INVOICE_NUMBERS_FILE, "r") as f:
            try:
                temp_data = json.load(f)
                invoice_numbers.clear()
                invoice_numbers.update(temp_data)
                invoice_numbers = {str(k): v for k, v in invoice_numbers.items()}
            except json.JSONDecodeError:
                invoice_numbers.clear()
                logger.warning("invoice_numbers.json is corrupted or empty, reinitializing.")
            except Exception as e:
                logger.error(f"Error loading invoice_numbers.json: {e}, reinitializing.")
                invoice_numbers.clear()

    if os.path.exists(DAILY_PROFIT_FILE):
        with open(DAILY_PROFIT_FILE, "r") as f:
            try:
                daily_profit = json.load(f)
            except json.JSONDecodeError:
                daily_profit = 0.0
                logger.warning("daily_profit.json is corrupted or empty, reinitializing.")
            except Exception as e:
                logger.error(f"Error loading daily_profit.json: {e}, reinitializing.")
                daily_profit = 0.0
    
    if os.path.exists(LAST_BUTTON_MESSAGE_FILE):
        with open(LAST_BUTTON_MESSAGE_FILE, "r") as f:
            try:
                temp_data = json.load(f)
                last_button_message.clear()
                last_button_message.update(temp_data)
                last_button_message = {str(k): v for k, v in last_button_message.items()}
            except json.JSONDecodeError:
                last_button_message.clear()
                logger.warning("last_button_message.json is corrupted or empty, reinitializing.")
            except Exception as e:
                logger.error(f"Error loading last_button_message.json: {e}, reinitializing.")
                last_button_message.clear()

# حفظ البيانات
def _save_data_to_disk():
    global save_pending
    with save_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            with open(ORDERS_FILE, "w") as f:
                json.dump(orders, f)
            with open(PRICING_FILE, "w") as f:
                json.dump(pricing, f)
            with open(INVOICE_NUMBERS_FILE, "w") as f:
                json.dump(invoice_numbers, f)
            with open(DAILY_PROFIT_FILE, "w") as f:
                json.dump(daily_profit, f)
            with open(LAST_BUTTON_MESSAGE_FILE, "w") as f:
                json.dump(last_button_message, f)
            logger.info("All data saved to disk successfully.")
        except Exception as e:
            logger.error(f"Error saving data to disk: {e}")
        finally:
            save_pending = False

# دالة الحفظ المؤجل
def schedule_save():
    global save_timer, save_pending
    if save_pending:
        logger.info("Save already pending, skipping new schedule.")
        return

    if save_timer is not None:
        save_timer.cancel()

    save_pending = True
    save_timer = threading.Timer(0.5, _save_data_to_disk)
    save_timer.start()
    logger.info("Data save scheduled with 0.5 sec delay.")


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

# تحميل البيانات عند بدء البوت
load_data()

# حالات المحادثة
ASK_BUY, ASK_SELL, ASK_PLACES = range(3) 

# جلب التوكن ومعرف المالك من متغيرات البيئة
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_TELEGRAM_ID")) 
OWNER_PHONE_NUMBER = "+9647733921468" 

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
        await asyncio.sleep(0.05) # تأخير خفيف قبل الحذف
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Successfully deleted message {message_id} from chat {chat_id} in background.")
    except Exception as e:
        logger.warning(f"Could not delete message {message_id} from chat {chat_id} in background: {e}.")

# دالة مساعدة لحفظ البيانات في الخلفية
async def save_data_in_background(context: ContextTypes.DEFAULT_TYPE):
    schedule_save()
    logger.info("Data save scheduled in background.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    if user_id in context.user_data:
        del context.user_data[user_id]
        logger.info(f"Cleared user_data for user {user_id} on /start command.")
    
    await update.message.reply_text("أهلاً بك يا أبا الأكبر! لإعداد طلبية، دز الطلبية كلها برسالة واحدة.\n\n*السطر الأول:* عنوان الزبون.\n*الأسطر الباقية:* كل منتج بسطر واحد.", parse_mode="Markdown")
    return ConversationHandler.END

async def receive_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_order(update, context, update.message)

async def edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.edited_message:
        return
    await process_order(update, context, update.edited_message, edited=True)

async def process_order(update, context, message, edited=False):
    user_id = str(message.from_user.id)
    lines = message.text.strip().split('\n')
    if len(lines) < 2:
        if not edited:
            await message.reply_text("الرجاء التأكد من كتابة عنوان الزبون في السطر الأول والمنتجات في الأسطر التالية.")
        return

    title = lines[0]
    products = [p.strip() for p in lines[1:] if p.strip()]

    if not products:
        if not edited:
            await message.reply_text("الرجاء إضافة منتجات بعد العنوان.")
        return

    order_id = None
    # هنا ندور على الطلبية من خلال الـ last_button_message أو إذا موجودة بالـ user_data
    for oid, msg_info in last_button_message.items():
        # التأكد إنو الرسالة تابعة لنفس المستخدم ونفس الشات
        if msg_info and msg_info.get("message_id") == message.message_id and str(msg_info.get("chat_id")) == str(message.chat_id):
            if oid in orders and str(orders[oid].get("user_id")) == user_id:
                order_id = oid
                logger.info(f"Found existing order {order_id} for user {user_id} based on message ID.")
                break
            else:
                logger.warning(f"Message ID {message.message_id} found in last_button_message but not linked to user {user_id} or order {oid} is missing. Treating as new.")
                order_id = None # إعادة التعيين للتأكد من معاملتها كطلب جديد
                break
    
    # إذا ملكينا بالـ last_button_message، ممكن يكون جاي من زر "تعديل الأسعار"
    # هذا الشرط مهم لاستمرار تدفق المحادثة بعد تعديل الطلبية من رسالة الزر.
    if not order_id and user_id in context.user_data and "completed_order_id" in context.user_data[user_id]:
        temp_order_id = context.user_data[user_id]["completed_order_id"]
        if temp_order_id in orders and str(orders[temp_order_id].get("user_id")) == user_id:
            order_id = temp_order_id
            logger.info(f"Found existing order {order_id} for user {user_id} based on completed_order_id in user_data.")


    is_new_order = False
    if not order_id:
        is_new_order = True
        order_id = str(uuid.uuid4())[:8]
        invoice_no = get_invoice_number()
        # ضفنا "places_count": 0 لتهيئة الطلب الجديد
        orders[order_id] = {"user_id": user_id, "title": title, "products": products, "places_count": 0} 
        pricing[order_id] = {p: {} for p in products}
        invoice_numbers[order_id] = invoice_no
        logger.info(f"Created new order {order_id} for user {user_id}.")
    else:
        # إذا الطلبية موجودة، نعدل عليها
        old_products = set(orders[order_id].get("products", []))
        new_products = set(products)
        
        orders[order_id]["title"] = title
        orders[order_id]["products"] = products

        # إضافة منتجات جديدة إلى الـ pricing (بأسعار فارغة)
        for p in new_products:
            if p not in pricing.get(order_id, {}):
                pricing.setdefault(order_id, {})[p] = {}
        
        # حذف المنتجات اللي انحذفت من الطلبية من الـ pricing
        if order_id in pricing:
            for p in old_products - new_products: # المنتجات اللي كانت موجودة وانحذفت
                if p in pricing[order_id]:
                    del pricing[order_id][p]
                    logger.info(f"Removed pricing for product '{p}' from order {order_id}.")
        logger.info(f"Updated existing order {order_id} for user {user_id}.")

    context.application.create_task(save_data_in_background(context))
    
    if is_new_order:
        await message.reply_text(f"استلمت الطلب بعنوان: *{title}* (عدد المنتجات: {len(products)})", parse_mode="Markdown")
        await show_buttons(message.chat_id, context, user_id, order_id)
    else:
        await show_buttons(message.chat_id, context, user_id, order_id, confirmation_message="تم تحديث الطلب. الرجاء التأكد من تسعير أي منتجات جديدة.")

async def show_buttons(chat_id, context, user_id, order_id, confirmation_message=None):
    if order_id not in orders:
        logger.warning(f"Attempted to show buttons for non-existent order_id: {order_id}")
        await context.bot.send_message(chat_id=chat_id, text="عذراً، الطلب الذي تحاول الوصول إليه غير موجود أو تم حذفه. الرجاء بدء طلبية جديدة.")
        if user_id in context.user_data:
            del context.user_data[user_id]
        return

    order = orders[order_id]
    
    completed_products = []
    pending_products = []
    for p in order["products"]:
        if p in pricing.get(order_id, {}) and 'buy' in pricing[order_id].get(p, {}) and 'sell' in pricing[order_id].get(p, {}):
            completed_products.append(p)
        else:
            pending_products.append(p)
            
    completed_products.sort()
    pending_products.sort()

    buttons_list = []
    for p in completed_products:
        buttons_list.append([InlineKeyboardButton(f"✅ {p}", callback_data=f"{order_id}|{p}")])
    for p in pending_products:
        buttons_list.append([InlineKeyboardButton(p, callback_data=f"{order_id}|{p}")])
    
    markup = InlineKeyboardMarkup(buttons_list)
    
    message_text = ""
    if confirmation_message:
        message_text += f"{confirmation_message}\n\n"
    message_text += f"اضغط على منتج لتحديد سعره من *{order['title']}*:"

    # تحديث أو إرسال رسالة الأزرار
    msg_info = last_button_message.get(order_id)
    if msg_info and str(msg_info.get("chat_id")) == str(chat_id):
        try:
            msg = await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_info["message_id"],
                text=message_text,
                reply_markup=markup,
                parse_mode="Markdown"
            )
            logger.info(f"Edited existing button message {msg_info['message_id']} for order {order_id}.")
        except Exception as e:
            logger.warning(f"Could not edit message {msg_info['message_id']} for order {order_id}: {e}. Sending new one.")
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                reply_markup=markup,
                parse_mode="Markdown"
            )
            last_button_message[order_id] = {"chat_id": chat_id, "message_id": msg.message_id}
            context.application.create_task(save_data_in_background(context))
    else:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=message_text,
            reply_markup=markup,
            parse_mode="Markdown"
        )
        logger.info(f"Sent new button message {msg.message_id} for order {order_id}")
        last_button_message[order_id] = {"chat_id": chat_id, "message_id": msg.message_id}
        context.application.create_task(save_data_in_background(context))

async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    logger.info(f"Callback query received: {query.data}")

    user_id = str(query.from_user.id)
    
    try:
        order_id, product = query.data.split("|", 1) 
    except ValueError as e:
        logger.error(f"Failed to parse callback_data for product selection: {query.data}. Error: {e}")
        await query.message.reply_text("عذراً، حدث خطأ في بيانات الزر. الرجاء بدء طلبية جديدة.")
        return ConversationHandler.END

    if order_id not in orders or product not in orders[order_id].get("products", []):
        logger.warning(f"Order ID '{order_id}' not found or Product '{product}' not in products for order '{order_id}'.")
        await query.message.reply_text("عذراً، الطلب أو المنتج غير موجود. الرجاء بدء طلبية جديدة أو التحقق من المنتجات.")
        if user_id in context.user_data:
            del context.user_data[user_id]
        return ConversationHandler.END
    
    context.user_data.setdefault(user_id, {})
    context.user_data[user_id].update({"order_id": order_id, "product": product})
    
    if 'messages_to_delete' not in context.user_data[user_id]:
        context.user_data[user_id]['messages_to_delete'] = [] 

    # لا نحذف رسالة الأزرار هنا، بل نعدلها لتبدو كأنها اختفت
    if query.message:
        try:
            # هنا نخلي الرسالة تبين فارغة (أو بس بيها نص تأكيد) مؤقتاً
            await context.bot.edit_message_reply_markup(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                reply_markup=InlineKeyboardMarkup([[]]) # نخليها بدون أزرار
            )
            logger.info(f"Cleared buttons from message {query.message.message_id} for order {order_id}.")
        except Exception as e:
            logger.warning(f"Could not clear buttons from message {query.message.message_id}: {e}. Proceeding.")

    msg = await query.message.reply_text(f"تمام، كم سعر شراء *'{product}'*؟", parse_mode="Markdown")
    context.user_data[user_id]['messages_to_delete'].append({'chat_id': msg.chat_id, 'message_id': msg.message_id})
    
    return ASK_BUY
    
async def receive_buy_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    
    context.user_data.setdefault(user_id, {})
    if 'messages_to_delete' not in context.user_data[user_id]:
        context.user_data[user_id]['messages_to_delete'] = []
    
    context.user_data[user_id]['messages_to_delete'].append({
        'chat_id': update.message.chat_id,
        'message_id': update.message.message_id # رسالة المستخدم (جوابه)
    })

    data = context.user_data.get(user_id, {})
    if not data or "order_id" not in data or "product" not in data:
        await update.message.reply_text("حدث خطأ، الرجاء البدء من جديد")
        return ConversationHandler.END
    
    order_id = data["order_id"]
    product = data["product"]
    
    try:
        price = float(update.message.text.strip())
        if price < 0:
            msg_error = await update.message.reply_text("السعر يجب أن يكون موجباً")
            context.user_data[user_id]['messages_to_delete'].append({
                'chat_id': msg_error.chat_id, 
                'message_id': msg_error.message_id
            })
            return ASK_BUY
    except ValueError:
        msg_error = await update.message.reply_text("الرجاء إدخال رقم صحيح")
        context.user_data[user_id]['messages_to_delete'].append({
                'chat_id': msg_error.chat_id, 
                'message_id': msg_error.message_id
            })
        return ASK_BUY
    
    msg = await update.message.reply_text(f"شكراً. وهسه، بيش راح تبيع *'{product}'*؟", parse_mode="Markdown")
    context.user_data[user_id]['messages_to_delete'].append({
        'chat_id': msg.chat_id,
        'message_id': msg.message_id
    })
    
    pricing.setdefault(order_id, {}).setdefault(product, {})["buy"] = price
    context.application.create_task(save_data_in_background(context))
    
    return ASK_SELL


async def receive_sell_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    
    context.user_data.setdefault(user_id, {})
    if 'messages_to_delete' not in context.user_data[user_id]:
        context.user_data[user_id]['messages_to_delete'] = []
    context.user_data[user_id]['messages_to_delete'].append({'chat_id': update.message.chat_id, 'message_id': update.message.message_id}) # رسالة المستخدم (جوابه)

    data = context.user_data.get(user_id)
    if not data or "order_id" not in data or "product" not in data:
        await update.message.reply_text("عذراً، حدث خطأ. الرجاء المحاولة مرة أخرى أو بدء طلبية جديدة.")
        if user_id in context.user_data:
            del context.user_data[user_id]
        return ConversationHandler.END
    
    order_id, product = data["order_id"], data["product"]
    
    if order_id not in orders or product not in orders[order_id].get("products", []):
        await update.message.reply_text("عذراً، الطلب أو المنتج لم يعد موجوداً. الرجاء بدء طلبية جديدة.")
        if user_id in context.user_data:
            del context.user_data[user_id]
        return ConversationHandler.END

    try:
        price = float(update.message.text.strip())
        if price < 0:
            msg_error = await update.message.reply_text("سعر البيع يجب أن يكون رقماً إيجابياً. بيش راح تبيع بالضبط؟")
            context.user_data[user_id]['messages_to_delete'].append({'chat_id': msg_error.chat_id, 'message_id': msg_error.message_id})
            return ASK_SELL 
    except ValueError:
        msg_error = await update.message.reply_text("الرجاء إدخال رقم صحيح لسعر البيع. بيش حتبيع؟")
        context.user_data[user_id]['messages_to_delete'].append({'chat_id': msg_error.chat_id, 'message_id': msg_error.message_id})
        return ASK_SELL 
    
    pricing.setdefault(order_id, {}).setdefault(product, {})["sell"] = price
    context.application.create_task(save_data_in_background(context))

    # حذف رسائل البوت والمستخدم السابقة
    logger.info(f"Scheduling deletion of {len(context.user_data[user_id].get('messages_to_delete', []))} messages for user {user_id}.")
    for msg_info in context.user_data[user_id].get('messages_to_delete', []):
        context.application.create_task(delete_message_in_background(context, chat_id=msg_info['chat_id'], message_id=msg_info['message_id']))
    context.user_data[user_id]['messages_to_delete'].clear()

    # حذف رسالة الأزرار القديمة (التي أصبحت بدون أزرار في product_selected)
    msg_info_buttons = last_button_message.get(order_id)
    if msg_info_buttons and str(msg_info_buttons.get("chat_id")) == str(update.effective_chat.id):
        try:
            await context.bot.delete_message(chat_id=msg_info_buttons["chat_id"], message_id=msg_info_buttons["message_id"])
            logger.info(f"Successfully deleted previous button message {msg_info_buttons['message_id']} for order {order_id}.")
        except Exception as e:
            logger.warning(f"Could not delete previous button message {msg_info_buttons['message_id']} for order {order_id}: {e}. Attempting to edit.")
            try:
                await context.bot.edit_message_text(
                    chat_id=msg_info_buttons["chat_id"],
                    message_id=msg_info_buttons["message_id"],
                    text="." # يمكن وضع أي نص بسيط هنا
                )
                logger.info(f"Edited previous button message {msg_info_buttons['message_id']} to remove buttons.")
            except Exception as edit_e:
                logger.warning(f"Could not edit previous button message {msg_info_buttons['message_id']} for order {order_id}: {edit_e}. Skipping.")
        
        # بعد ما حذفنا أو عدلنا الرسالة، يجب إزالتها من القائمة
        if order_id in last_button_message:
            del last_button_message[order_id]
            context.application.create_task(save_data_in_background(context)) # نحفظ التغيير مال الحذف

    # التحقق إذا كل المنتجات تم تسعيرها أو لا
    order = orders[order_id]
    all_priced = True
    for p in order["products"]:
        if p not in pricing.get(order_id, {}) or "buy" not in pricing[order_id].get(p, {}) or "sell" not in pricing[order_id].get(p, {}):
            all_priced = False
            break
            
    if all_priced:
        context.user_data[user_id]["completed_order_id"] = order_id # نخليه حتى نقدر نستخدمه في تعديل المحلات
        await request_places_count(update.effective_chat.id, context, user_id, order_id)
        # هنا المهم: ننتقل للحالة الجديدة ASK_PLACES بدلاً من END
        return ASK_PLACES 
    else:
        confirmation_msg = f"تم حفظ السعر لـ *'{product}'*."
        logger.info(f"Price saved for '{product}' in order {order_id}. Showing updated buttons with confirmation.")
        await show_buttons(update.effective_chat.id, context, user_id, order_id, confirmation_message=confirmation_msg)
        return ConversationHandler.END


async def request_places_count(chat_id, context: ContextTypes.DEFAULT_TYPE, user_id: str, order_id: str):
    """
    تسأل المستخدم عن عدد المحلات وتوفر أزرار اختيار.
    """
    # هنا نحفظ الـ order_id في user_data["completed_order_id"]
    # حتى لمن المستخدم يكتب رقم يدوي، نعرف هذا الرقم لـ يا طلب.
    context.user_data.setdefault(user_id, {})["completed_order_id"] = order_id 
    
    buttons = []
    emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']
    for i in range(1, 11):
        buttons.append(InlineKeyboardButton(emojis[i-1], callback_data=f"places_{order_id}_{i}"))
    
    keyboard = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg_places = await context.bot.send_message(
        chat_id=chat_id,
        text="تمام، كل المنتجات تسعّرت. هسه، كم محل كلفتك الطلبية؟ (اختر من الأزرار أو اكتب الرقم)", 
        reply_markup=reply_markup
    )
    context.user_data[user_id]['messages_to_delete'].append({'chat_id': msg_places.chat_id, 'message_id': msg_places.message_id})

    # لا نرجع ConversationHandler.END هنا، لأنو هذه الدالة تستدعى كجزء من منطق البوت
    # والـ ConversationHandler راح ينتقل لـ ASK_PLACES تلقائياً بعد receive_sell_price
    return 

async def receive_place_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    هاندلر مخصص لعدد المحلات، يستقبل سواء كان زر أو إدخال نصي، ويستمر بالـ ConversationHandler.
    """
    global daily_profit
    
    places = None
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)
    
    context.user_data.setdefault(user_id, {})
    if 'messages_to_delete' not in context.user_data[user_id]:
        context.user_data[user_id]['messages_to_delete'] = []

    target_order_id = context.user_data[user_id].get("completed_order_id") # نجلب order_id من الـ user_data

    if not target_order_id or target_order_id not in orders or str(orders[target_order_id].get("user_id")) != user_id:
        await context.bot.send_message(chat_id=chat_id, text="عذراً، لا توجد طلبية مكتملة لمعالجتها أو تم حذفها. الرجاء بدء طلبية جديدة.")
        if user_id in context.user_data:
            del context.user_data[user_id]
        return ConversationHandler.END # هنا ننهي الـ conversation

    if update.callback_query:
        query = update.callback_query
        logger.info(f"Places callback query received: {query.data}")
        await query.answer()
        
        try:
            parts = query.data.split('_')
            # تأكد أن الكول باك يبدأ بـ "places_" ولديه 3 أجزاء بالضبط (places_orderid_عدد)
            if len(parts) == 3 and parts[0] == "places":
                # نتأكد أن الـ order_id من الكول باك يطابق الـ order_id اللي بالك user_data
                if parts[1] != target_order_id:
                    logger.error(f"Mismatch order_id from callback ({parts[1]}) and user_data ({target_order_id}).")
                    await context.bot.send_message(chat_id=chat_id, text="عذراً، حدث خطأ في ربط الطلب. الرجاء بدء طلبية جديدة.")
                    if user_id in context.user_data: del context.user_data[user_id]
                    return ConversationHandler.END

                places = int(parts[2])
                if query.message:
                    context.application.create_task(delete_message_in_background(context, chat_id=query.message.chat_id, message_id=query.message.message_id))
            else:
                raise ValueError(f"Unexpected callback_data format for places: {query.data}")
        except (ValueError, IndexError) as e:
            logger.error(f"Failed to parse places count from callback data '{query.data}': {e}")
            await context.bot.send_message(chat_id=chat_id, text="عذراً، حدث خطأ في بيانات الزر. الرجاء المحاولة مرة أخرى.")
            return ASK_PLACES # نطلب منه المحاولة مرة أخرى
            

    elif update.message: # إذا المستخدم كتب رقم يدوي
        context.user_data[user_id]['messages_to_delete'].append({'chat_id': update.message.chat_id, 'message_id': update.message.message_id})
        
        try:
            places = int(update.message.text.strip())
            if places < 0:
                msg_error = await context.bot.send_message(chat_id=chat_id, text="عدد المحلات يجب أن يكون رقماً موجباً. الرجاء إدخال عدد المحلات بشكل صحيح.")
                context.user_data[user_id]['messages_to_delete'].append({'chat_id': msg_error.chat_id, 'message_id': msg_error.message_id})
                return ASK_PLACES # نبقى بنفس الحالة
        except ValueError:
            msg_error = await context.bot.send_message(chat_id=chat_id, text="الرجاء إدخال عدد صحيح لعدد المحلات.")
            context.user_data[user_id]['messages_to_delete'].append({'chat_id': msg_error.chat_id, 'message_id': msg_error.message_id})
            return ASK_PLACES # نبقى بنفس الحالة
    
    if places is None:
        logger.warning("No places count received or invalid input.")
        await context.bot.send_message(chat_id=chat_id, text="عذراً، لم أتمكن من فهم عدد المحلات. الرجاء إدخال رقم صحيح.")
        return ASK_PLACES # نطلب منه المحاولة مرة أخرى

    # تحديث عدد المحلات في بيانات الطلب باستخدام الـ target_order_id
    orders[target_order_id]["places_count"] = places
    context.application.create_task(save_data_in_background(context))

    # حذف رسائل الحوار السابقة
    if user_id in context.user_data and 'messages_to_delete' in context.user_data[user_id]:
        for msg_info in context.user_data[user_id]['messages_to_delete']:
            context.application.create_task(delete_message_in_background(context, chat_id=msg_info['chat_id'], message_id=msg_info['message_id']))
        context.user_data[user_id]['messages_to_delete'].clear()
    
    # بعد ما تم إدخال عدد المحلات بنجاح، نشيل "completed_order_id"
    if "completed_order_id" in context.user_data[user_id]:
        del context.user_data[user_id]["completed_order_id"]

    # استدعاء show_final_options لعرض الأزرار النهائية
    await show_final_options(chat_id, context, user_id, target_order_id, message_prefix="تم تحديث عدد المحلات بنجاح.")
    
    return ConversationHandler.END # هنا ننهي الـ conversation بعد ما اكتمل كل شي


async def show_final_options(chat_id, context, user_id, order_id, message_prefix=None):
    """
    تعرض فاتورة الزبون ثم الأزرار النهائية بعد اكتمال تسعير الطلب وتحديد عدد المحلات.
    """
    if order_id not in orders:
        logger.warning(f"Attempted to show final options for non-existent order_id: {order_id}")
        await context.bot.send_message(chat_id=chat_id, text="عذراً، الطلب الذي تحاول الوصول إليه غير موجود أو تم حذفه. الرجاء بدء طلبية جديدة.")
        if user_id in context.user_data:
            del context.user_data[user_id]
        return

    order = orders[order_id]
    invoice = invoice_numbers.get(order_id, "غير معروف")
    
    total_buy = 0.0
    total_sell = 0.0
    for p in order["products"]:
        if p in pricing.get(order_id, {}) and "buy" in pricing[order_id].get(p, {}) and "sell" in pricing[order_id].get(p, {}):
            total_buy += pricing[order_id][p]["buy"]
            total_sell += pricing[order_id][p]["sell"]

    net_profit = total_sell - total_buy
    
    current_places = orders[order_id].get("places_count", 0) 
    extra_cost = calculate_extra(current_places)
    final_total = total_sell + extra_cost

    # --- بناء فاتورة الزبون ---
    customer_invoice_lines = []
    customer_invoice_lines.append(f"**أبو الأكبر للتوصيل**") 
    customer_invoice_lines.append(f"رقم الفاتورة: {invoice}")
    customer_invoice_lines.append(f"عنوان الزبون: {order['title']}")
    customer_invoice_lines.append(f"\n*المواد:*") 
    
    running_total_for_customer = 0.0
    for p in order["products"]:
        if p in pricing.get(order_id, {}) and "sell" in pricing[order_id].get(p, {}):
            sell = pricing[order_id][p]["sell"]
            running_total_for_customer += sell
            customer_invoice_lines.append(f"{p} - {format_float(sell)} = {format_float(running_total_for_customer)}")
        else:
            customer_invoice_lines.append(f"{p} - (لم يتم تسعيره)")
    
    customer_invoice_lines.append(f"كلفة تجهيز من - {current_places} محلات {format_float(extra_cost)} = {format_float(final_total)}")
    customer_invoice_lines.append(f"\n*المجموع الكلي:* {format_float(final_total)} (مع احتساب عدد المحلات)") 
    
    customer_final_text = "\n".join(customer_invoice_lines)

    # --- إرسال فاتورة الزبون برسالة منفصلة أولاً ---
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=customer_final_text,
            parse_mode="Markdown"
        )
        logger.info(f"Customer invoice sent as a separate message for order {order_id}.")
    except Exception as e:
        logger.error(f"Could not send customer invoice as separate message to chat {chat_id}: {e}")
        await context.bot.send_message(chat_id=chat_id, text="عذراً، لم أتمكن من إرسال فاتورة الزبون. الرجاء المحاولة مرة أخرى.")


    # --- إنشاء الأزرار النهائية ---
    keyboard = [
        [InlineKeyboardButton("1️⃣ تعديل الأسعار", callback_data=f"edit_prices_{order_id}")],
        # [InlineKeyboardButton("2️⃣ تعديل المحلات", callback_data=f"edit_places_{order_id}")], # تم إزالة هذا الزر بناءً على طلبك
        [InlineKeyboardButton("3️⃣ إرسال فاتورة الزبون (واتساب)", url=f"https://wa.me/{OWNER_PHONE_NUMBER}?text={customer_final_text.replace(' ', '%20').replace('\n', '%0A').replace('*', '')}")],
        [InlineKeyboardButton("4️⃣ إنشاء طلب جديد", callback_data="start_new_order")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_text = "افعل ما تريد من الأزرار:\n\n"
    if message_prefix:
        message_text = message_prefix + "\n" + message_text
    
    # رسالة الإدارة للواتساب
    owner_invoice_details = []
    owner_invoice_details.append(f"رقم الفاتورة: {invoice}")
    owner_invoice_details.append(f"عنوان الزبون: {order['title']}")
    for p in order["products"]:
        if p in pricing.get(order_id, {}) and "buy" in pricing[order_id].get(p, {}) and "sell" in pricing[order_id].get(p, {}):
            buy = pricing[order_id][p]["buy"]
            sell = pricing[order_id][p]["sell"] 
            profit_item = sell - buy
            owner_invoice_details.append(f"{p} - شراء: {format_float(buy)}, بيع: {format_float(sell)}, ربح: {format_float(profit_item)}")
        else:
            owner_invoice_details.append(f"{p} - (لم يتم تسعيره بعد)")
    owner_invoice_details.append(f"\nالمجموع شراء: {format_float(total_buy)}")
    owner_invoice_details.append(f"المجموع بيع: {format_float(total_sell)}")
    owner_invoice_details.append(f"الربح الكلي: {format_float(net_profit)}")
    owner_invoice_details.append(f"عدد المحلات: {current_places} (+{format_float(extra_cost)})")
    owner_invoice_details.append(f"السعر الكلي: {format_float(final_total)}")
    
    final_owner_invoice_text = "\n".join(owner_invoice_details)
    
    encoded_owner_invoice = final_owner_invoice_text.replace(" ", "%20").replace("\n", "%0A").replace("*", "")
    whatsapp_owner_button_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("إرسال فاتورة الإدارة للواتساب", url=f"https://wa.me/{OWNER_PHONE_NUMBER}?text={encoded_owner_invoice}")]
    ])

    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"**فاتورة طلبية (الإدارة):**\n{final_owner_invoice_text}",
            parse_mode="Markdown",
            reply_markup=whatsapp_owner_button_markup
        )
        logger.info(f"Admin invoice and WhatsApp button sent to OWNER_ID: {OWNER_ID}")
    except Exception as e:
        logger.error(f"Could not send admin invoice to OWNER_ID {OWNER_ID}: {e}")
        await context.bot.send_message(chat_id=chat_id, text="عذراً، لم أتمكن من إرسال فاتورة الإدارة إلى خاصك. يرجى التأكد من أنني أستطيع مراسلتك في الخاص (قد تحتاج إلى بدء محادثة معي أولاً).")

    # إرسال الرسالة النهائية مع الأزرار للزبون (هذه الرسالة تحتوي الأزرار فقط)
    await context.bot.send_message(chat_id=chat_id, text=message_text, reply_markup=reply_markup, parse_mode="Markdown")
    
    # حذف أي رسائل سابقة في user_data['messages_to_delete']
    if user_id in context.user_data: 
        if 'messages_to_delete' in context.user_data[user_id]:
            for msg_info in context.user_data[user_id]['messages_to_delete']:
                context.application.create_task(delete_message_in_background(context, chat_id=msg_info['chat_id'], message_id=msg_info['message_id']))
            context.user_data[user_id]['messages_to_delete'].clear()

    # بعد عرض الأزرار النهائية، نمسح بيانات المستخدم الخاصة بالطلب الحالي
    if user_id in context.user_data:
        if "order_id" in context.user_data[user_id]:
            del context.user_data[user_id]["order_id"]
        if "product" in context.user_data[user_id]:
            del context.user_data[user_id]["product"]
        # لا تحذف completed_order_id لأننا قد نحتاجه إذا المستخدم اختار تعديل مرة أخرى
        # if "completed_order_id" in context.user_data[user_id]:
        #     del context.user_data[user_id]["completed_order_id"]
        logger.info(f"Cleaned up order-specific user_data for user {user_id} after showing final options.")


async def edit_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    if query.data.startswith("edit_prices_"):
        order_id = query.data.replace("edit_prices_", "")
    else:
        await query.message.reply_text("عذراً، حدث خطأ في بيانات الزر. الرجاء المحاولة مرة أخرى.")
        return ConversationHandler.END

    if order_id not in orders or str(orders[order_id].get("user_id")) != user_id:
        await query.message.reply_text("عذراً، الطلب الذي تحاول تعديله غير موجود أو ليس لك.")
        return ConversationHandler.END

    # حذف رسالة الأزرار النهائية
    if query.message:
        context.application.create_task(delete_message_in_background(context, chat_id=query.message.chat_id, message_id=query.message.message_id))
    
    # تأكد من مسح الـ last_button_message لـ order_id هذا
    # هذا يضمن إنو show_buttons راح ترسل رسالة أزرار جديدة بالكامل
    if order_id in last_button_message:
        del last_button_message[order_id]
        context.application.create_task(save_data_in_background(context))

    await show_buttons(query.message.chat_id, context, user_id, order_id, confirmation_message="يمكنك الآن تعديل أسعار المنتجات أو إضافة/حذف منتجات بتعديل الرسالة الأصلية للطلبية.")
    
    return ConversationHandler.END # ننهي الـ ConversationHandler هنا

# دالة edit_places لم تعد تُستخدم بعد إزالة الزر
# لكن نتركها موجودة في الكود حتى لا يصير خطأ إذا كانت هناك أي مرجعيات لها
# ولكن فعلياً لن تُستدعى ما دام الزر محذوفاً.
async def edit_places(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    if query.data.startswith("edit_places_"):
        order_id = query.data.replace("edit_places_", "")
    else:
        await query.message.reply_text("عذراً، حدث خطأ في بيانات الزر. الرجاء المحاولة مرة أخرى.")
        return ConversationHandler.END

    if order_id not in orders or str(orders[order_id].get("user_id")) != user_id:
        await query.message.reply_text("عذراً، الطلب الذي تحاول تعديله غير موجود أو ليس لك.")
        return ConversationHandler.END

    # حذف رسالة الأزرار النهائية
    if query.message:
        context.application.create_task(delete_message_in_background(context, chat_id=query.message.chat_id, message_id=query.message.message_id))

    # هنا نحفظ الـ order_id في user_data["completed_order_id"]
    # حتى لمن المستخدم يكتب رقم يدوي، نعرف هذا الرقم لـ يا طلب.
    context.user_data.setdefault(user_id, {})["completed_order_id"] = order_id 
    
    buttons = []
    emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']
    for i in range(1, 11):
        # هنا الـ callback_data هي نفسها "places_{order_id}_{i}"
        buttons.append(InlineKeyboardButton(emojis[i-1], callback_data=f"places_{order_id}_{i}"))
    
    keyboard = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg_places = await query.message.reply_text(
        "تمام، كم محل كلفتك الطلبية؟ (اختر من الأزرار أو اكتب الرقم)", 
        reply_markup=reply_markup
    )
    context.user_data[user_id]['messages_to_delete'] = [{'chat_id': msg_places.chat_id, 'message_id': msg_places.message_id}]
    
    # هنا لا نرجع ASK_PLACES لأننا لا نريد أن ندخل في ConversationHandler state جديد.
    # نترك الـ receive_place_count تستقبل الأزرار والرسائل النصية كـ CallbackQueryHandler عادي.
    return ASK_PLACES # مهم جداً: ننتقل للحالة ASK_PLACES هنا

async def start_new_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    # مسح كل البيانات الخاصة بالطلب الحالي من user_data
    if user_id in context.user_data:
        context.user_data.pop(user_id, None) # استخدام pop لضمان عدم وجود KeyError إذا لم يكن المفتاح موجوداً
        logger.info(f"Cleared all user_data for user {user_id} after starting a new order from button.")

    if query.message:
        context.application.create_task(delete_message_in_background(context, chat_id=query.message.chat_id, message_id=query.message.message_id))

    await query.message.reply_text("تمام، دز الطلبية الجديدة كلها برسالة واحدة.\n\n*السطر الأول:* عنوان الزبون.\n*الأسطر الباقية:* كل منتج بسطر واحد.", parse_mode="Markdown")
    
    return ConversationHandler.END


async def show_profit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.from_user.id) != str(OWNER_ID):
        await update.message.reply_text("عذراً، هذا الأمر متاح للمالك فقط.")
        return
    await update.message.reply_text(f"الربح التراكمي الإجمالي: *{format_float(daily_profit)}* دينار", parse_mode="Markdown")

async def reset_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.from_user.id) != str(OWNER_ID):
        await update.message.reply_text("عذراً، هذا الأمر متاح للمالك فقط.")
        return
    
    keyboard = [
        [InlineKeyboardButton("نعم، متأكد", callback_data="confirm_reset")],
        [InlineKeyboardButton("لا، إلغاء", callback_data="cancel_reset")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("هل أنت متأكد من تصفير جميع الأرباح ومسح كل الطلبات؟ هذا الإجراء لا يمكن التراجع عنه.", reply_markup=reply_markup)

async def confirm_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != str(OWNER_ID):
        await query.edit_message_text("عذراً، لا تملك صلاحية لتنفيذ هذا الأمر.")
        return

    if query.data == "confirm_reset":
        global daily_profit, orders, pricing, invoice_numbers, last_button_message
        daily_profit = 0.0
        orders.clear()
        pricing.clear()
        invoice_numbers.clear()
        last_button_message.clear()
        
        try:
            with open(COUNTER_FILE, "w") as f:
                f.write("1")
        except Exception as e:
            logger.error(f"Could not reset invoice counter file: {e}")

        _save_data_to_disk()
        await query.edit_message_text("تم تصفير الأرباح ومسح كل الطلبات بنجاح.")
    elif query.data == "cancel_reset":
        await query.edit_message_text("تم إلغاء عملية التصفير.")

async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.from_user.id) != str(OWNER_ID):
        await update.message.reply_text("عذراً، هذا الأمر متاح للمالك فقط.")
        return
    
    total_orders = len(orders)
    total_products = 0
    total_buy_all_orders = 0.0 
    total_sell_all_orders = 0.0 
    product_counter = Counter()
    details = []

    for order_id, order in orders.items():
        invoice = invoice_numbers.get(order_id, "غير معروف")
        details.append(f"\n**فاتورة رقم:** {invoice}")
        details.append(f"**عنوان الزبون:** {order['title']}")
        
        order_buy = 0.0
        order_sell = 0.0
        
        if isinstance(order.get("products"), list):
            for p_name in order["products"]:
                total_products += 1
                product_counter[p_name] += 1
                
                if p_name in pricing.get(order_id, {}) and "buy" in pricing[order_id].get(p_name, {}) and "sell" in pricing[order_id].get(p_name, {}):
                    buy = pricing[order_id][p_name]["buy"]
                    sell = pricing[order_id][p_name]["sell"]
                    profit = sell - buy
                    order_buy += buy
                    order_sell += sell
                    details.append(f"  - {p_name} | شراء: {format_float(buy)} | بيع: {format_float(sell)} | ربح: {format_float(profit)}")
                else:
                    details.append(f"  - {p_name} | (لم يتم تسعيره)")
        else:
            details.append(f"  (لا توجد منتجات محددة لهذا الطلب)")

        total_buy_all_orders += order_buy
        total_sell_all_orders += order_sell
        details.append(f"  *ربح هذه الطلبية:* {format_float(order_sell - order_buy)}")

    top_product_str = "لا يوجد"
    if product_counter:
        top_product_name, top_product_count = product_counter.most_common(1)[0]
        top_product_str = f"{top_product_name} ({top_product_count} مرة)"

    result = (
        f"**--- تقرير عام عن الطلبات ---**\n"
        f"**إجمالي عدد الطلبات المعالجة:** {total_orders}\n"
        f"**إجمالي عدد المنتجات المباعة (في الطلبات المعالجة):** {total_products}\n"
        f"**أكثر منتج تم طلبه:** {top_product_str}\n\n"
        f"**مجموع الشراء الكلي (للطلبات المعالجة):** {format_float(total_buy_all_orders)}\n"
        f"**مجموع البيع الكلي (للطلبات المعالجة):** {format_float(total_sell_all_orders)}\n"
        f"**صافي الربح الكلي (للطلبات المعالجة):** {format_float(total_sell_all_orders - total_buy_all_orders)}\n" 
        f"**الربح التراكمي في البوت (منذ آخر تصفير):** {format_float(daily_profit)} دينار\n\n"
        f"**--- تفاصيل الطلبات ---**\n" + "\n".join(details)
    )
    await update.message.reply_text(result, parse_mode="Markdown")

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # إضافة الـ Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^الارباح$|^ارباح$"), show_profit))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^صفر$|^تصفير$"), reset_all))
    app.add_handler(CallbackQueryHandler(confirm_reset, pattern="^(confirm_reset|cancel_reset)$"))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^التقارير$|^تقرير$|^تقارير$"), show_report))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, edited_message))

    # إضافة الهاندلرات الجديدة لأزرار ما بعد اكتمال الطلب
    app.add_handler(CallbackQueryHandler(edit_prices, pattern="^edit_prices_"))
    # بما أن زر تعديل المحلات تم إزالته من الواجهة، هذا الهاندلر لن يُستدعى
    # app.add_handler(CallbackQueryHandler(edit_places, pattern="^edit_places_")) 
    app.add_handler(CallbackQueryHandler(start_new_order_callback, pattern="^start_new_order$"))

    # محادثة تجهيز الطلبات (الآن مع إضافة حالة ASK_PLACES)
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order),
            CallbackQueryHandler(product_selected, pattern=r"^[a-f0-9]{8}\|.+$") # نمط الزر لproduct_selected
        ],
        states={
            ASK_BUY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_buy_price),
            ],
            ASK_SELL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sell_price),
            ],
            ASK_PLACES: [
                # يستقبل الكول باك لعدد المحلات من الأزرار
                CallbackQueryHandler(receive_place_count, pattern=r"^places_[a-f0-9]{8}_\d+$"),
                # يستقبل الرسائل النصية اللي بيها أرقام لعدد المحلات
                MessageHandler(filters.TEXT & filters.Regex(r"^\d+$"), receive_place_count), 
            ]
        },
        fallbacks=[
            CommandHandler("cancel", lambda u, c: ConversationHandler.END)
        ]
    )
    app.add_handler(conv_handler)

    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
