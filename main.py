from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters
)
import uuid
import os
from collections import Counter
import json

# أسماء ملفات حفظ البيانات
ORDERS_FILE = "orders.json"
PRICING_FILE = "pricing.json"
INVOICE_NUMBERS_FILE = "invoice_numbers.json"
DAILY_PROFIT_FILE = "daily_profit.json"
COUNTER_FILE = "invoice_counter.txt"

# تحميل البيانات عند بدء تشغيل البوت
def load_data():
    global orders, pricing, invoice_numbers, daily_profit, last_button_message

    # تهيئة المتغيرات
    orders = {}
    pricing = {}
    invoice_numbers = {}
    daily_profit = 0.0
    last_button_message = {} # هذا ما راح ينحفظ، لأنه يتعلق بحالة الرسائل في الوقت الحالي

    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r") as f:
            try:
                orders = json.load(f)
                # تحويل مفاتيح orders و pricing و invoice_numbers إلى str إذا كانت integers
                # لأن JSON قد يحولها إلى int إذا كانت أرقام
                orders = {str(k): v for k, v in orders.items()}
                for oid in orders:
                    if oid in pricing:
                        pricing[oid] = {str(pk): pv for pk, pv in pricing[oid].items()}
            except json.JSONDecodeError:
                orders = {}
                print("DEBUG: orders.json is corrupted or empty, reinitializing.")

    if os.path.exists(PRICING_FILE):
        with open(PRICING_FILE, "r") as f:
            try:
                pricing = json.load(f)
                pricing = {str(k): v for k, v in pricing.items()}
                for oid in pricing:
                    pricing[oid] = {str(pk): pv for pk, pv in pricing[oid].items()}
            except json.JSONDecodeError:
                pricing = {}
                print("DEBUG: pricing.json is corrupted or empty, reinitializing.")

    if os.path.exists(INVOICE_NUMBERS_FILE):
        with open(INVOICE_NUMBERS_FILE, "r") as f:
            try:
                invoice_numbers = json.load(f)
                invoice_numbers = {str(k): v for k, v in invoice_numbers.items()}
            except json.JSONDecodeError:
                invoice_numbers = {}
                print("DEBUG: invoice_numbers.json is corrupted or empty, reinitializing.")

    if os.path.exists(DAILY_PROFIT_FILE):
        with open(DAILY_PROFIT_FILE, "r") as f:
            try:
                daily_profit = json.load(f)
            except json.JSONDecodeError:
                daily_profit = 0.0
                print("DEBUG: daily_profit.json is corrupted or empty, reinitializing.")

# حفظ البيانات
def save_data():
    with open(ORDERS_FILE, "w") as f:
        json.dump(orders, f)
    with open(PRICING_FILE, "w") as f:
        json.dump(pricing, f)
    with open(INVOICE_NUMBERS_FILE, "w") as f:
        json.dump(invoice_numbers, f)
    with open(DAILY_PROFIT_FILE, "w") as f:
        json.dump(daily_profit, f)

# تهيئة ملف عداد الفواتير
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
OWNER_ID = int(os.getenv("OWNER_TELEGRAM_ID")) # تأكد من تحويله لرقم صحيح
# رقم الواتساب اللي راح يرسل الفاتورة عليه
OWNER_PHONE_NUMBER = "+9647733921468" # تم تحديده هنا مباشرة حسب طلبك

# التأكد من وجود المتغيرات
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("أهلاً بك! لإعداد طلبية، دز الطلبية كلها برسالة واحدة.\n\n*السطر الأول:* عنوان الزبون.\n*الأسطر الباقية:* كل منتج بسطر واحد.", parse_mode="Markdown")

async def receive_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_order(update, context, update.message)

async def edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.edited_message:
        return
    await process_order(update, context, update.edited_message, edited=True)

async def process_order(update, context, message, edited=False):
    user_id = str(message.from_user.id) # تحويل الـ ID إلى string للحفظ في JSON
    lines = message.text.strip().split('\n')
    if len(lines) < 2:
        if not edited: # لا ترد على الرسائل غير المؤهلة إذا لم تكن رسالة معدلة
            await message.reply_text("الرجاء التأكد من كتابة عنوان الزبون في السطر الأول والمنتجات في الأسطر التالية.")
        return

    title = lines[0]
    products = [p.strip() for p in lines[1:] if p.strip()] # تنظيف المنتجات

    if not products:
        if not edited:
            await message.reply_text("الرجاء إضافة منتجات بعد العنوان.")
        return

    existing_order_id = None
    # البحث عن طلبية موجودة لنفس المستخدم ونفس العنوان
    for oid, order in orders.items():
        # هنا نتأكد أن user_id المخزن هو str
        if order["user_id"] == user_id and order["title"] == title:
            existing_order_id = oid
            break

    # إذا كانت رسالة معدلة، ابحث عن الطلبية المرتبطة بها
    if edited:
        for oid, msg_id in last_button_message.items():
            if msg_id == message.message_id:
                existing_order_id = oid
                break
        if existing_order_id and orders[existing_order_id]["user_id"] != user_id:
            # تجنب تعديل طلبية شخص آخر عن طريق الخطأ
            existing_order_id = None


    if existing_order_id:
        order_id = existing_order_id
        old_products = set(orders[order_id]["products"])
        new_products = set(products)
        added_products = list(new_products - old_products) # المنتجات الجديدة فقط
        
        # تحديث العنوان والمنتجات للطلبية الموجودة
        orders[order_id]["title"] = title
        orders[order_id]["products"].extend([p for p in added_products if p not in orders[order_id]["products"]])
        
        # تهيئة التسعير للمنتجات المضافة حديثاً
        for p in added_products:
            if p not in pricing[order_id]:
                pricing[order_id][p] = {}
        
        save_data() # حفظ التغييرات
        await show_buttons(message.chat_id, context, user_id, order_id)
        return

    # إنشاء طلبية جديدة
    order_id = str(uuid.uuid4())[:8] # معرف فريد للطلبية
    invoice_no = get_invoice_number()
    orders[order_id] = {"user_id": user_id, "title": title, "products": products}
    pricing[order_id] = {p: {} for p in products} # تهيئة التسعير لكل منتج
    invoice_numbers[order_id] = invoice_no
    
    save_data() # حفظ البيانات بعد إنشاء طلب جديد
    
    await message.reply_text(f"استلمت الطلب بعنوان: *{title}* (عدد المنتجات: {len(products)})", parse_mode="Markdown")
    await show_buttons(message.chat_id, context, user_id, order_id)

async def show_buttons(chat_id, context, user_id, order_id):
    order = orders[order_id]
    buttons = []
    for p in order["products"]:
        # التحقق مما إذا كان المنتج قد تم تسعيره بالكامل
        is_done = p in pricing[order_id] and 'buy' in pricing[order_id][p] and 'sell' in pricing[order_id][p]
        label = f"✅ {p}" if is_done else p
        # هنا تم تعديل الـ callback_data لإضافة "product_select_" في البداية
        buttons.append([InlineKeyboardButton(label, callback_data=f"product_select_{order_id}|{p}")]) 
    
    markup = InlineKeyboardMarkup(buttons)
    
    # محاولة حذف الرسالة القديمة للأزرار لتجنب الفوضى
    if order_id in last_button_message:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=last_button_message[order_id])
        except Exception:
            # تجاهل الخطأ إذا كانت الرسالة غير موجودة أو لم يتم حذفها
            pass

    msg = await context.bot.send_message(chat_id=chat_id, text=f"اضغط على منتج لتحديد سعره من *{order['title']}*:", reply_markup=markup, parse_mode="Markdown")
    last_button_message[order_id] = msg.message_id

async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"DEBUG: Callback query received: {query.data}") # رسالة للـ Logs
    await query.answer() # يجب الإجابة على الكولباك كويري

    try:
        # تم تعديل طريقة تقسيم الـ callback_data
        # البحث عن أول | بعد "product_select_"
        data_parts = query.data.split("_", 1)[1] # إزالة "product_select_"
        order_id, product = data_parts.split("|", 1) 
    except IndexError as e:
        print(f"ERROR: Failed to parse callback_data: {query.data}. Error: {e}")
        await query.message.reply_text("عذراً، حدث خطأ في معالجة بيانات الزر. الرجاء المحاولة مرة أخرى.")
        return ConversationHandler.END
    
    user_id = str(query.from_user.id) # تحويل الـ ID إلى string

    # التحقق من أن الطلب والمنتج لا يزالان موجودين
    # تأكد من أن order_id موجود في orders قبل الوصول إلى [order_id]
    if order_id not in orders or product not in orders[order_id].get("products", []): # استخدام .get للحماية
        print(f"DEBUG: Order ID '{order_id}' not found in orders or product '{product}' not in products of '{order_id}'. Current orders keys: {list(orders.keys())}") # رسالة للـ Logs
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
    
    try:
        price = float(update.message.text.strip())
        if price < 0:
            await update.message.reply_text("سعر الشراء يجب أن يكون رقماً إيجابياً. بيش اشتريت بالضبط؟")
            return ASK_BUY
    except ValueError:
        await update.message.reply_text("الرجاء إدخال رقم صحيح لسعر الشراء. بيش اشتريت؟")
        return ASK_BUY
    
    pricing[order_id].setdefault(product, {})["buy"] = price
    save_data() # حفظ بعد تحديث سعر الشراء

    await update.message.reply_text(f"شكراً. وهسه، بيش راح تبيع *'{product}'*؟", parse_mode="Markdown")
    return ASK_SELL

async def receive_sell_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    data = current_product.get(user_id)
    if not data:
        await update.message.reply_text("عذراً، حدث خطأ. الرجاء المحاولة مرة أخرى أو بدء طلبية جديدة.")
        return ConversationHandler.END
    
    order_id, product = data["order_id"], data["product"]
    
    try:
        price = float(update.message.text.strip())
        if price < 0:
            await update.message.reply_text("سعر البيع يجب أن يكون رقماً إيجابياً. بيش راح تبيع بالضبط؟")
            return ASK_SELL
    except ValueError:
        await update.message.reply_text("الرجاء إدخال رقم صحيح لسعر البيع. بيش حتبيع؟")
        return ASK_SELL
    
    pricing[order_id][product]["sell"] = price
    save_data() # حفظ بعد تحديث سعر البيع

    await update.message.reply_text(f"تم حفظ السعر لـ *'{product}'*.", parse_mode="Markdown")
    
    order = orders[order_id]
    # التحقق مما إذا كانت جميع المنتجات قد تم تسعيرها
    all_priced = True
    for p in order["products"]:
        if p not in pricing[order_id] or "buy" not in pricing[order_id][p] or "sell" not in pricing[order_id][p]:
            all_priced = False
            break
            
    if all_priced:
        context.user_data["completed_order_id"] = order_id
        
        # أزرار اختيار عدد المحلات
        buttons = []
        emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']
        for i in range(1, 11):
            buttons.append(InlineKeyboardButton(emojis[i-1], callback_data=f"places_{i}"))
        
        # تقسيم الأزرار على سطرين أو أكثر
        keyboard = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text("كل المنتجات تم تسعيرها. كم محل كلفتك الطلبية؟ (اختر من الأزرار أو اكتب الرقم)", reply_markup=reply_markup)
        return ASK_PLACES
    else:
        await show_buttons(update.effective_chat.id, context, user_id, order_id)
        return ConversationHandler.END # إذا لم تكتمل جميع المنتجات، يتم إنهاء المحادثة والعودة للأزرار

def calculate_extra(places):
    extra_fees = {
        1: 0,
        2: 0,
        3: 1,
        4: 2,
        5: 3,
        6: 4
    }
    return extra_fees.get(places, places - 2) # إذا كان الرقم مو موجود بالقاموس، يرجع places - 2

async def receive_place_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global daily_profit
    
    places = None
    message_to_edit = None

    if update.callback_query:
        query = update.callback_query
        print(f"DEBUG: Places callback query received: {query.data}") # رسالة للـ Logs
        await query.answer()
        if query.data.startswith("places_"):
            places = int(query.data.split("_")[1])
            message_to_edit = query.message # استخدم رسالة الكويري للتعديل عليها لاحقاً
        else:
            await query.edit_message_text("عذراً، حدث خطأ في اختيار عدد المحلات.")
            return ConversationHandler.END
    elif update.message:
        message_to_edit = update.message
        try:
            places = int(message_to_edit.text.strip())
            if places < 0:
                await message_to_edit.reply_text("عدد المحلات يجب أن يكون رقماً موجباً. الرجاء إدخال عدد المحلات بشكل صحيح.")
                return ASK_PLACES
        except ValueError:
            await message_to_edit.reply_text("الرجاء إدخال عدد صحيح لعدد المحلات.")
            return ASK_PLACES
    
    if places is None: # للتأكد إذا ماكو لا كويري ولا رسالة
        return ASK_PLACES

    order_id = context.user_data.get("completed_order_id")
    if not order_id or order_id not in orders:
        await message_to_edit.reply_text("عذراً، لا توجد طلبية مكتملة لمعالجتها. الرجاء بدء طلبية جديدة.")
        return ConversationHandler.END

    order = orders[order_id]
    invoice = invoice_numbers.get(order_id, "غير معروف")
    total_buy = 0.0
    total_sell = 0.0
    
    # بناء الفاتورة الأصلية (للمجهز)
    invoice_text_for_owner = [
        f"رقم الفاتورة: {invoice}",
        f"عنوان الزبون: {order['title']}",
    ]

    for p in order["products"]:
        if p in pricing[order_id] and "buy" in pricing[order_id][p] and "sell" in pricing[order_id][p]:
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
    save_data() # حفظ الربح اليومي

    extra = calculate_extra(places)
    total_with_extra = total_sell + extra

    invoice_text_for_owner.append(f"\nالمجموع شراء: {format_float(total_buy)}")
    invoice_text_for_owner.append(f"المجموع بيع: {format_float(total_sell)}")
    invoice_text_for_owner.append(f"الربح الكلي: {format_float(net_profit)}")
    invoice_text_for_owner.append(f"عدد المحلات: {places} (+{format_float(extra)})")
    invoice_text_for_owner.append(f"السعر الكلي: {format_float(total_with_extra)}")
    
    final_owner_invoice_text = "\n".join(invoice_text_for_owner)

    # إرسال الفاتورة الأصلية للمجهز
    await message_to_edit.reply_text(
        f"**الفاتورة النهائية:**\n{final_owner_invoice_text}",
        parse_mode="Markdown"
    )

    # بناء نسخة الزبون (لأبي الأكبر)
    running_total = 0.0
    customer_lines = []
    for p in order["products"]:
        if p in pricing[order_id] and "sell" in pricing[order_id][p]:
            sell = pricing[order_id][p]["sell"]
            running_total += sell
            customer_lines.append(f"{p} - {format_float(sell)} = {format_float(running_total)}")
        else:
            customer_lines.append(f"{p} - (لم يتم تسعيره)")
    
    # سطر كلفة التجهيز للمحلات
    customer_lines.append(f"كلفة تجهيز من - {places} محلات {format_float(extra)} = {format_float(total_with_extra)}")
    
    customer_text = (
        f"أبو الأكبر للتوصيل\n"
        f"رقم الفاتورة: {invoice}\n"
        f"عنوان الزبون: {order['title']}\n\n"
        f"المواد:\n" + "\n".join(customer_lines) +
        f"\nالمجموع الكلي: {format_float(total_with_extra)} (مع احتساب عدد المحلات)"
    )
    
    await message_to_edit.reply_text("نسخة الزبون:\n" + customer_text, parse_mode="Markdown")

    # رابط الواتساب (للفاتورة الأصلية)
    encoded_owner_invoice = final_owner_invoice_text.replace(" ", "%20").replace("\n", "%0A").replace("*", "") # إزالة النجوم للمشاركة
    wa_link = f"https://wa.me/{OWNER_PHONE_NUMBER}?text={encoded_owner_invoice}"
    await message_to_edit.reply_text("دوس على هذا الرابط حتى ترسل الفاتورة *لي* على الواتساب:\n" + wa_link, parse_mode="Markdown")
    
    return ConversationHandler.END

async def show_profit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.from_user.id) != str(OWNER_ID): # تأكد من مقارنة السلاسل النصية
        await update.message.reply_text("عذراً، هذا الأمر متاح للمالك فقط.")
        return
    await update.message.reply_text(f"الربح التراكمي الإجمالي: *{format_float(daily_profit)}* دينار", parse_mode="Markdown")

async def reset_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.from_user.id) != str(OWNER_ID):
        await update.message.reply_text("عذراً، هذا الأمر متاح للمالك فقط.")
        return
    
    # طلب تأكيد قبل التصفير
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
        global daily_profit, orders, pricing, invoice_numbers
        daily_profit = 0.0
        orders.clear()
        pricing.clear()
        invoice_numbers.clear()
        
        # إعادة تعيين عداد الفواتير
        with open(COUNTER_FILE, "w") as f:
            f.write("1")

        save_data() # حفظ البيانات بعد التصفير
        await query.edit_message_text("تم تصفير الأرباح ومسح كل الطلبات بنجاح.")
    elif query.data == "cancel_reset":
        await query.edit_message_text("تم إلغاء عملية التصفير.")

async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.from_user.id) != str(OWNER_ID):
        await update.message.reply_text("عذراً، هذا الأمر متاح للمالك فقط.")
        return
    
    total_orders = len(orders)
    total_products = 0
    total_buy = 0.0
    total_sell = 0.0
    product_counter = Counter()
    details = []

    for order_id, order in orders.items():
        invoice = invoice_numbers.get(order_id, "غير معروف")
        details.append(f"\n**فاتورة رقم:** {invoice}")
        details.append(f"**عنوان:** {order['title']}")
        
        order_buy = 0.0
        order_sell = 0.0
        
        for p_name in order["products"]:
            total_products += 1
            product_counter[p_name] += 1
            
            if p_name in pricing[order_id] and "buy" in pricing[order_id][p_name] and "sell" in pricing[order_id][p_name]:
                buy = pricing[order_id][p_name]["buy"]
                sell = pricing[order_id][p_name]["sell"]
                profit = sell - buy
                total_buy += buy
                total_sell += sell
                order_buy += buy
                order_sell += sell
                details.append(f"  - {p_name} | شراء: {format_float(buy)} | بيع: {format_float(sell)} | ربح: {format_float(profit)}")
            else:
                details.append(f"  - {p_name} | (لم يتم تسعيره)")
        
        # ربح الطلبية الواحدة
        details.append(f"  *ربح هذه الطلبية:* {format_float(order_sell - order_buy)}")

    top_product_str = "لا يوجد"
    if product_counter:
        top_product_name, top_product_count = product_counter.most_common(1)[0]
        top_product_str = f"{top_product_name} ({top_product_count} مرة)"

    result = (
        f"**--- تقرير عام عن الطلبات ---**\n"
        f"**إجمالي الطلبات:** {total_orders}\n"
        f"**إجمالي المنتجات المباعة:** {total_products}\n"
        f"**أكثر منتج تم طلبه:** {top_product_str}\n\n"
        f"**مجموع الشراء الكلي:** {format_float(total_buy)}\n"
        f"**مجموع البيع الكلي:** {format_float(total_sell)}\n"
        f"**مجموع الأرباح الصافية الكلية:** {format_float(total_sell - total_buy)}\n"
        f"**الربح التراكمي في البوت:** {format_float(daily_profit)} دينار\n\n"
        f"**--- تفاصيل الطلبات ---**\n" + "\n".join(details)
    )
    await update.message.reply_text(result, parse_mode="Markdown")


def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # إضافة الـ Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^الارباح$|^ارباح$"), show_profit))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^صفر$|^تصفير$"), reset_all))
    app.add_handler(CallbackQueryHandler(confirm_reset, pattern="^(confirm_reset|cancel_reset)$")) # للتعامل مع أزرار التأكيد
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^التقارير$|^تقرير$|^تقارير$"), show_report))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, edited_message))

    # محادثة تجهيز الطلبات
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order),
            # تم تعديل pattern هنا ليلتقط الـ callback_data الخاصة بأزرار المنتجات
            CallbackQueryHandler(product_selected, pattern=r"^product_select_.*") 
        ],
        states={
            ASK_BUY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_buy_price)],
            ASK_SELL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sell_price)],
            ASK_PLACES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_place_count), # للتعامل مع الإدخال اليدوي
                CallbackQueryHandler(receive_place_count, pattern="^places_") # للتعامل مع الأزرار
            ],
        },
        fallbacks=[
            CommandHandler("cancel", lambda u, c: ConversationHandler.END) # ممكن تضيف أمر cancel
        ]
    )
    app.add_handler(conv_handler)

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
