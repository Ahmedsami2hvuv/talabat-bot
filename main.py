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



# تفعيل الـ logging للحصول على تفاصيل الأخطاء والعمليات

logging.basicConfig(

    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO

)

logger = logging.getLogger(__name__)





# المسار الثابت لحفظ البيانات داخل وحدة التخزين (Volume)

DATA_DIR = "/mnt/data/"



# أسماء ملفات حفظ البيانات، الآن ستُحفظ داخل مجلد DATA_DIR

ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")

PRICING_FILE = os.path.join(DATA_DIR, "pricing.json")

INVOICE_NUMBERS_FILE = os.path.join(DATA_DIR, "invoice_numbers.json")

DAILY_PROFIT_FILE = os.path.join(DATA_DIR, "daily_profit.json")

COUNTER_FILE = os.path.join(DATA_DIR, "invoice_counter.txt")

# ملف لحفظ IDs رسائل الأزرار لكي لا يتم حذفها عند إعادة التشغيل

LAST_BUTTON_MESSAGE_FILE = os.path.join(DATA_DIR, "last_button_message.json")



# تهيئة المتغيرات العامة في النطاق العلوي لضمان وجودها

orders = {}

pricing = {}

invoice_numbers = {}

daily_profit = 0.0

last_button_message = {} 

current_product = {} 



# تحميل البيانات عند بدء تشغيل البوت

def load_data():

    global orders, pricing, invoice_numbers, daily_profit, last_button_message, current_product



    os.makedirs(DATA_DIR, exist_ok=True)



    if os.path.exists(ORDERS_FILE):

        with open(ORDERS_FILE, "r") as f:

            try:

                orders = json.load(f)

                orders = {str(k): v for k, v in orders.items()}

            except json.JSONDecodeError:

                orders = {}

                logger.warning("orders.json is corrupted or empty, reinitializing.")

            except Exception as e:

                logger.error(f"Error loading orders.json: {e}, reinitializing.")

                orders = {}



    if os.path.exists(PRICING_FILE):

        with open(PRICING_FILE, "r") as f:

            try:

                pricing = json.load(f)

                pricing = {str(k): v for k, v in pricing.items()}

                for oid in pricing:

                    if isinstance(pricing[oid], dict):

                        pricing[oid] = {str(pk): pv for pk, pv in pricing[oid].items()}

            except json.JSONDecodeError:

                pricing = {}

                logger.warning("pricing.json is corrupted or empty, reinitializing.")

            except Exception as e:

                logger.error(f"Error loading pricing.json: {e}, reinitializing.")

                pricing = {}



    if os.path.exists(INVOICE_NUMBERS_FILE):

        with open(INVOICE_NUMBERS_FILE, "r") as f:

            try:

                invoice_numbers = json.load(f)

                invoice_numbers = {str(k): v for k, v in invoice_numbers.items()}

            except json.JSONDecodeError:

                invoice_numbers = {}

                logger.warning("invoice_numbers.json is corrupted or empty, reinitializing.")

            except Exception as e:

                logger.error(f"Error loading invoice_numbers.json: {e}, reinitializing.")

                invoice_numbers = {}



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

    

    # تحميل آخر رسائل الأزرار

    if os.path.exists(LAST_BUTTON_MESSAGE_FILE):

        with open(LAST_BUTTON_MESSAGE_FILE, "r") as f:

            try:

                last_button_message = json.load(f)

                last_button_message = {str(k): v for k, v in last_button_message.items()}

            except json.JSONDecodeError:

                last_button_message = {}

                logger.warning("last_button_message.json is corrupted or empty, reinitializing.")

            except Exception as e:

                logger.error(f"Error loading last_button_message.json: {e}, reinitializing.")

                last_button_message = {}



# حفظ البيانات

def save_data():

    os.makedirs(DATA_DIR, exist_ok=True)

    with open(ORDERS_FILE, "w") as f:

        json.dump(orders, f)

    with open(PRICING_FILE, "w") as f:

        json.dump(pricing, f)

    with open(INVOICE_NUMBERS_FILE, "w") as f:

        json.dump(invoice_numbers, f)

    with open(DAILY_PROFIT_FILE, "w") as f:

        json.dump(daily_profit, f)

    # حفظ IDs رسائل الأزرار

    with open(LAST_BUTTON_MESSAGE_FILE, "w") as f:

        json.dump(last_button_message, f)



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



# دالة مساعدة لحذف الرسائل (تم إعادة إضافتها)

async def delete_message_safe(context, chat_id, message_id):

    try:

        if message_id:

            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)

            logger.info(f"Deleted message {message_id} in chat {chat_id}")

    except Exception as e:

        logger.warning(f"Could not delete message {message_id} in chat {chat_id}: {e}")



# دالة لتنسيق الأرقام العشرية

def format_float(value):

    formatted = f"{value:g}"

    if formatted.endswith(".0"):

        return formatted[:-2]

    return formatted



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text("أهلاً بك! لإعداد طلبية، دز الطلبية كلها برسالة واحدة.\n\n*السطر الأول:* عنوان الزبون.\n*الأسطر الباقية:* كل منتج بسطر واحد.", parse_mode="Markdown")



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



    existing_order_id = None

    # البحث عن طلبية موجودة لنفس المستخدم ونفس العنوان

    for oid, order in orders.items():

        if str(order.get("user_id")) == user_id and order.get("title") == title:

            existing_order_id = oid

            break



    if edited:

        # البحث عن طلبية موجودة مرتبطة بهذه الرسالة المعدلة

        for oid, msg_info in last_button_message.items():

            if msg_info.get("message_id") == message.message_id and str(msg_info.get("chat_id")) == str(message.chat_id):

                if oid in orders and str(orders[oid].get("user_id")) == user_id:

                    existing_order_id = oid

                break

        

        # إذا تم تعديل رسالة ليس لها طلب موجود أو ملك لمستخدم آخر، عاملها كطلب جديد

        if existing_order_id and existing_order_id not in orders:

            existing_order_id = None

        if existing_order_id and str(orders[existing_order_id].get("user_id")) != user_id:

            existing_order_id = None



    if existing_order_id:

        order_id = existing_order_id

        old_products = set(orders[order_id].get("products", []))

        new_products = set(products)

        added_products = list(new_products - old_products)

        

        orders[order_id]["title"] = title

        for p in added_products:

            if p not in orders[order_id]["products"]:

                orders[order_id]["products"].append(p)

        

        for p in added_products:

            if p not in pricing.get(order_id, {}):

                pricing.setdefault(order_id, {})[p] = {}

        

        save_data()

        await show_buttons(message.chat_id, context, user_id, order_id)

        return



    # إنشاء طلبية جديدة

    order_id = str(uuid.uuid4())[:8]

    invoice_no = get_invoice_number()

    orders[order_id] = {"user_id": user_id, "title": title, "products": products}

    pricing[order_id] = {p: {} for p in products}

    invoice_numbers[order_id] = invoice_no

    

    save_data()

    

    await message.reply_text(f"استلمت الطلب بعنوان: *{title}* (عدد المنتجات: {len(products)})", parse_mode="Markdown")

    await show_buttons(message.chat_id, context, user_id, order_id)



async def show_buttons(chat_id, context, user_id, order_id, is_final_buttons=False):

    if order_id not in orders:

        logger.warning(f"Attempted to show buttons for non-existent order_id: {order_id}")

        await context.bot.send_message(chat_id=chat_id, text="الطلبية الي تريدها يمكن محذوفة لانها غير موجوده توكل وسوي طلب جديد حالك حال الوادم.")

        return



    order = orders[order_id]

    

    # فصل المنتجات المكتملة عن غير المكتملة لغرض الترتيب

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

    

    # **** التعديل هنا: إرسال الرسالة الجديدة أولاً

    msg = await context.bot.send_message(

        chat_id=chat_id,

        text=f"دوس على المنتج حتى تكتب سعره *{order['title']}*:",

        reply_markup=markup,

        parse_mode="Markdown"

    )

    logger.info(f"Sent new button message {msg.message_id} for order {order_id}")



    msg_info = last_button_message.get(order_id)

    if msg_info and msg_info.get("chat_id") == chat_id:

        try:

            # **** ثم حذف الرسالة القديمة (باستخدام الدالة الجديدة)

            await delete_message_safe(context, msg_info["chat_id"], msg_info["message_id"])

            logger.info(f"Deleted old button message {msg_info.get('message_id', 'N/A')} for order {order_id}")

        except Exception as e:

            logger.warning(f"Could not delete old button message {msg_info.get('message_id', 'N/A')} for order {order_id}: {e}. It might have been deleted already or is inaccessible.")

            pass # تجاهل الخطأ إذا الرسالة لم تعد موجودة أو لا يمكن حذفها

        finally:

            # إزالة الإشارة للرسالة القديمة من الذاكرة والملف

            if order_id in last_button_message:

                del last_button_message[order_id]

                save_data() # حفظ التغيير لضمان عدم الرجوع للرسالة المحذوفة بعد إعادة تشغيل البوت

    

    last_button_message[order_id] = {"chat_id": chat_id, "message_id": msg.message_id}

    save_data() # حفظ الـ ID والـ chat_id للرسالة الجديدة





async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    logger.info(f"Callback query received: {query.data}")

    await query.answer()



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

        return ConversationHandler.END

    

    current_product[user_id] = {"order_id": order_id, "product": product}

    await query.message.reply_text(f"تمام، كم سعر شراء *'{product}'*؟", parse_mode="Markdown")

    return ASK_BUY



async def receive_buy_price(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = str(update.message.from_user.id)

    data = current_product.get(user_id)

    if not data:

        await update.message.reply_text("عذراً، حدث خطأ. الرجاء المحاولة مرة أخرى أو بدء طلبية جديدة.")

        return ConversationHandler.END

    

    order_id, product = data["order_id"], data["product"]

    

    if order_id not in orders or product not in orders[order_id].get("products", []):

        await update.message.reply_text("عذراً، الطلب أو المنتج لم يعد موجوداً. الرجاء بدء طلبية جديدة.")

        return ConversationHandler.END



    try:

        price = float(update.message.text.strip())

        if price < 0:

            await update.message.reply_text("سعر الشراء يجب أن يكون رقماً إيجابياً. بيش اشتريت بالضبط؟")

            return ASK_BUY

    except ValueError:

        await update.message.reply_text("الرجاء إدخال رقم صحيح لسعر الشراء. بيش اشتريت؟")

        return ASK_BUY

    

    pricing.setdefault(order_id, {}).setdefault(product, {})["buy"] = price

    save_data()



    await update.message.reply_text(f"شكراً. وهسه، بيش راح تبيع *'{product}'*؟", parse_mode="Markdown")

    return ASK_SELL



async def receive_sell_price(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = str(update.message.from_user.id)

    data = current_product.get(user_id)

    if not data:

        await update.message.reply_text("عذراً، حدث خطأ. الرجاء المحاولة مرة أخرى أو بدء طلبية جديدة.")

        return ConversationHandler.END

    

    order_id, product = data["order_id"], data["product"]

    

    if order_id not in orders or product not in orders[order_id].get("products", []):

        await update.message.reply_text("عذراً، الطلب أو المنتج لم يعد موجوداً. الرجاء بدء طلبية جديدة.")

        return ConversationHandler.END



    try:

        price = float(update.message.text.strip())

        if price < 0:

            await update.message.reply_text("سعر البيع يجب أن يكون رقماً إيجابياً. بيش راح تبيع بالضبط؟")

            return ASK_SELL

    except ValueError:

        await update.message.reply_text("الرجاء إدخال رقم صحيح لسعر البيع. بيش حتبيع؟")

        return ASK_SELL

    

    pricing.setdefault(order_id, {}).setdefault(product, {})["sell"] = price

    save_data()



    await update.message.reply_text(f"تم حفظ السعر لـ *'{product}'*.", parse_mode="Markdown")

    

    if order_id not in orders:

        await update.message.reply_text("عذراً، الطلب لم يعد موجوداً بعد حفظ السعر. الرجاء بدء طلبية جديدة.")

        return ConversationHandler.END



    order = orders[order_id]

    all_priced = True

    for p in order["products"]:

        if p not in pricing.get(order_id, {}) or "buy" not in pricing[order_id].get(p, {}) or "sell" not in pricing[order_id].get(p, {}):

            all_priced = False

            break

            

    if all_priced:

        context.user_data["completed_order_id"] = order_id

        

        buttons = []

        emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']

        for i in range(1, 11):

            buttons.append(InlineKeyboardButton(emojis[i-1], callback_data=f"places_{i}"))

        

        keyboard = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]

        reply_markup = InlineKeyboardMarkup(keyboard)



        await update.message.reply_text("كل المنتجات تم تسعيرها. كم محل كلفتك الطلبية؟ (اختر من الأزرار أو اكتب الرقم)", reply_markup=reply_markup)

        return ASK_PLACES

    else:

        await show_buttons(update.effective_chat.id, context, user_id, order_id)

        return ASK_BUY



def calculate_extra(places):

    extra_fees = {

        1: 0,

        2: 0,

        3: 1,

        4: 2,

        5: 3,

        6: 4

    }

    return extra_fees.get(places, places - 2)



async def receive_place_count(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global daily_profit

    

    places = None

    message_to_send_from = None



    if update.callback_query:

        query = update.callback_query

        logger.info(f"Places callback query received: {query.data}")

        await query.answer()

        if query.data.startswith("places_"):

            places = int(query.data.split("_")[1])

            message_to_send_from = query.message

            try:

                await context.bot.edit_message_reply_markup(

                    chat_id=query.message.chat_id,

                    message_id=query.message.message_id,

                    reply_markup=None

                )

            except Exception as e:

                logger.warning(f"Could not remove places buttons: {e}")

                pass

        else:

            logger.error(f"Unexpected callback_query in receive_place_count: {query.data}")

            await query.edit_message_text("عذراً، حدث خطأ غير متوقع. الرجاء المحاولة مرة أخرى أو بدء طلبية جديدة.")

            return ConversationHandler.END

    elif update.message:

        message_to_send_from = update.message

        try:

            places = int(message_to_send_from.text.strip())

            if places < 0:

                await message_to_send_from.reply_text("عدد المحلات يجب أن يكون رقماً موجباً. الرجاء إدخال عدد المحلات بشكل صحيح.")

                return ASK_PLACES

        except ValueError:

            await message_to_send_from.reply_text("الرجاء إدخال عدد صحيح لعدد المحلات.")

            return ASK_PLACES

    

    if places is None:

        logger.warning("No places count received.")

        return ConversationHandler.END



    order_id = context.user_data.get("completed_order_id")

    if not order_id or order_id not in orders:

        await message_to_send_from.reply_text("عذراً، لا توجد طلبية مكتملة لمعالجتها أو تم حذفها. الرجاء بدء طلبية جديدة.")

        return ConversationHandler.END



    order = orders[order_id]

    invoice = invoice_numbers.get(order_id, "غير معروف")

    total_buy = 0.0

    total_sell = 0.0

    

    invoice_text_for_owner = [

        f"رقم الفاتورة: {invoice}",

        f"عنوان الزبون: {order['title']}",

    ]



    for p in order["products"]:

        if p in pricing.get(order_id, {}) and "buy" in pricing[order_id].get(p, {}) and "sell" in pricing[order_id].get(p, {}):

            buy = pricing[order_id][p]["buy"]

            sell = pricing[order_id][p]["sell"]

            profit = sell - buy

            total_buy += buy

            total_sell += sell

            invoice_text_for_owner.append(f"{p} - شراء: {format_float(buy)}, بيع: {format_float(sell)}, ربح: {format_float(profit)}")

        else:

            invoice_text_for_owner.append(f"{p} - (لم يتم تسعيره بعد)")



    net_profit = total_sell - total_buy

    daily_profit += net_profit

    save_data()



    extra = calculate_extra(places)

    total_with_extra = total_sell + extra



    invoice_text_for_owner.append(f"\nالمجموع شراء: {format_float(total_buy)}")

    invoice_text_for_owner.append(f"المجموع بيع: {format_float(total_sell)}")

    invoice_text_for_owner.append(f"الربح الكلي: {format_float(net_profit)}")

    invoice_text_for_owner.append(f"عدد المحلات: {places} (+{format_float(extra)})")

    invoice_text_for_owner.append(f"السعر الكلي: {format_float(total_with_extra)}")

    

    final_owner_invoice_text = "\n".join(invoice_text_for_owner)



    # إرسال فاتورة الإدارة إلى الخاص بالمالك (فقط للمالك)

    try:

        await context.bot.send_message(

            chat_id=OWNER_ID, # <--- يتم الإرسال إلى ID المالك فقط

            text=f"**فاتورة طلبية (الإدارة):**\n{final_owner_invoice_text}",

            parse_mode="Markdown"

        )

        logger.info(f"Admin invoice sent to OWNER_ID: {OWNER_ID}")

    except Exception as e:

        logger.error(f"Could not send admin invoice to OWNER_ID {OWNER_ID}: {e}")

        # إذا لم يتمكن من إرسالها للمالك، يرسل رسالة خطأ للمستخدم

        await message_to_send_from.reply_text("عذراً، لم أتمكن من إرسال فاتورة الإدارة إلى خاصك. يرجى التأكد من أنني أستطيع مراسلتك في الخاص (قد تحتاج إلى بدء محادثة معي أولاً).")





    running_total = 0.0

    customer_lines = []

    for p in order["products"]:

        if p in pricing.get(order_id, {}) and "sell" in pricing[order_id].get(p, {}):

            sell = pricing[order_id][p]["sell"]

            running_total += sell

            customer_lines.append(f"{p} - {format_float(sell)} = {format_float(running_total)}")

        else:

            customer_lines.append(f"{p} - (لم يتم تسعيره)")

    

    customer_lines.append(f"كلفة تجهيز من - {places} محلات {format_float(extra)} = {format_float(total_with_extra)}")

    

    customer_text = (

        f"أبو الأكبر للتوصيل\n"

        f"رقم الفاتورة: {invoice}\n"

        f"عنوان الزبون: {order['title']}\n\n"

        f"المواد:\n" + "\n".join(customer_lines) +

        f"\nالمجموع الكلي: {format_float(total_with_extra)} (مع احتساب عدد المحلات)"

    )

    

    # هذه هي الرسالة التي تحتوي على نسخة الزبون. سنقوم بتعديل الأزرار هنا.

    await message_to_send_from.reply_text("نسخة الزبون (لإرسالها للعميل):\n" + customer_text, parse_mode="Markdown")



    # أزرار الواتساب (لنسخة الإدارة والزبون)

    encoded_owner_invoice = final_owner_invoice_text.replace(" ", "%20").replace("\n", "%0A").replace("*", "")

    encoded_customer_invoice = customer_text.replace(" ", "%20").replace("\n", "%0A").replace("*", "")



    # إنشاء الأزرار التي ستظهر في المحادثة العامة (فقط زر الزبون)

    whatsapp_buttons_for_group_chat = InlineKeyboardMarkup([

        [InlineKeyboardButton("إرسال فاتورة الزبون للواتساب", url=f"https://wa.me/{OWNER_PHONE_NUMBER}?text={encoded_customer_invoice}")]

    ])

    await message_to_send_from.reply_text("دوس على هذه الأزرار لإرسال الفواتير عبر الواتساب:", reply_markup=whatsapp_buttons_for_group_chat)

    

    # إرسال أزرار الواتساب لنسخة الإدارة إلى الخاص بالمالك أيضاً

    whatsapp_buttons_for_owner_private = InlineKeyboardMarkup([

        [InlineKeyboardButton("إرسال فاتورة الإدارة للواتساب", url=f"https://wa.me/{OWNER_PHONE_NUMBER}?text={encoded_owner_invoice}")],

        [InlineKeyboardButton("إرسال فاتورة الزبون للواتساب", url=f"https://wa.me/{OWNER_PHONE_NUMBER}?text={encoded_customer_invoice}")]

    ])

    try:

        await context.bot.send_message(

            chat_id=OWNER_ID,

            text="روابط واتساب سريعة:",

            reply_markup=whatsapp_buttons_for_owner_private

        )

    except Exception as e:

        logger.error(f"Could not send WhatsApp buttons to OWNER_ID {OWNER_ID}: {e}")





    final_actions_keyboard = InlineKeyboardMarkup([

        [InlineKeyboardButton("عدل الطلب", callback_data=f"edit_last_order_{order_id}")],

        [InlineKeyboardButton("سوي طلب جديد", callback_data="start_new_order")]

    ])

    await message_to_send_from.reply_text("اختار بكيفك", reply_markup=final_actions_keyboard)



    return ConversationHandler.END



async def edit_last_order(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    

    user_id = str(query.from_user.id)

    if query.data.startswith("edit_last_order_"):

        order_id = query.data.replace("edit_last_order_", "")

    else:

        await query.message.reply_text("اسف حبي صار خطا بيانات الدكمه ادوسها من جديد.")

        return ConversationHandler.END



    if order_id not in orders or str(orders[order_id].get("user_id")) != user_id:

        await query.message.reply_text(" اسف حبي الطلب او المنتج مو موجود.")

        return ConversationHandler.END



    await show_buttons(query.message.chat_id, context, user_id, order_id)

    

    return ASK_BUY



async def start_new_order(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    await query.message.reply_text("تمام، دز الطلبية الجديدة كلها برسالة واحدة.\n\n*السطر الأول:* عنوان الزبون.\n*الأسطر الباقية:* كل منتج بسطر واحد.", parse_mode="Markdown")

    return ConversationHandler.END





async def show_profit(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if str(update.message.from_user.id) != str(OWNER_ID):

        await update.message.reply_text("عوف لكلاوات.")

        return

    await update.message.reply_text(f"الربح التراكمي الإجمالي: *{format_float(daily_profit)}* دينار", parse_mode="Markdown")



async def reset_all(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if str(update.message.from_user.id) != str(OWNER_ID):

        await update.message.reply_text("عوف الكلاوات.")

        return

    

    keyboard = [

        [InlineKeyboardButton("نعم، متأكد", callback_data="confirm_reset")],

        [InlineKeyboardButton("لا، إلغاء", callback_data="cancel_reset")]

    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("يابه متاكد مو بعدين تضل تلطم.", reply_markup=reply_markup)



async def confirm_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()



    if str(query.from_user.id) != str(OWNER_ID):

        await query.edit_message_text("افتر وارجع.")

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



        save_data()

        await query.edit_message_text("دهاك صفرتلك كلشي.")

    elif query.data == "cancel_reset":

        await query.edit_message_text("ها شحجينه .لغيت عملية التصفير.")



async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if str(update.message.from_user.id) != str(OWNER_ID):

        await update.message.reply_text("ادور وراي.")

        return

    

    total_orders = len(orders)

    total_products = 0

    total_buy_all_orders = 0.0 # لتجميع الشراء من كل الطلبات

    total_sell_all_orders = 0.0 # لتجميع البيع من كل الطلبات

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

                    details.append(f"  - {p_name} | شراء: {format_float(buy)} | بيع: {format_float(sell)} | ربح: {format_float(profit)}")

                else:

                    details.append(f"  - {p_name} | (لم يتم تسعيره)")

        else:

            details.append(f"  (لا توجد منتجات محددة لهذا الطلب)")



        total_buy_all_orders += order_buy

        total_sell_all_orders += order_sell

        details.append(f"  *ربح هذه الطلبية:* {format_float(order_sell - order_buy)}")



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

    app.add_handler(CallbackQueryHandler(edit_last_order, pattern="^edit_last_order_"))

    app.add_handler(CallbackQueryHandler(start_new_order, pattern="^start_new_order$"))





    # محادثة تجهيز الطلبات

    conv_handler = ConversationHandler(

        entry_points=[

            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order),

            CallbackQueryHandler(product_selected)

        ],

        states={

            ASK_BUY: [

                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_buy_price),

                CallbackQueryHandler(product_selected) 

            ],

            ASK_SELL: [

                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sell_price),

                CallbackQueryHandler(product_selected) 

            ],

            ASK_PLACES: [

                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_place_count),

                CallbackQueryHandler(receive_place_count, pattern="^places_")

            ],

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
