from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters
)
import uuid
import os
from collections import Counter

orders = {}
pricing = {}
current_product = {}
last_button_message = {}
invoice_numbers = {}
daily_profit = 0.0
OWNER_ID = 7032076289

ASK_BUY, ASK_SELL = range(2)
TOKEN = "7508502359:AAFtlXVMJGUiWaeqJZc0o03Yy-SgVYE_xz8"

counter_file = "invoice_counter.txt"
if not os.path.exists(counter_file):
    with open(counter_file, "w") as f:
        f.write("1")

def get_invoice_number():
    with open(counter_file, "r") as f:
        current = int(f.read().strip())
    with open(counter_file, "w") as f:
        f.write(str(current + 1))
    return current

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("دز الطلبيه كلها برساله واحده عنوان الزبون يكون بالسطر الاول وباقي المنتجات تكون كل منتج بسطر جوا الثاني.")

async def receive_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_order(update, context, update.message)

async def edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.edited_message:
        return
    await process_order(update, context, update.edited_message, edited=True)

async def process_order(update, context, message, edited=False):
    user_id = message.from_user.id
    lines = message.text.strip().split('\n')
    if len(lines) < 2:
        return

    title = lines[0]  
    products = lines[1:]  

    existing_order_id = None  
    for oid, order in orders.items():  
        if order["user_id"] == user_id and order["title"] == title:  
            existing_order_id = oid  
            break  

    if edited:  
        for oid, order in orders.items():  
            if order["user_id"] == user_id and message.message_id == last_button_message.get(oid):  
                existing_order_id = oid  
                break  

    if existing_order_id:  
        order_id = existing_order_id  
        old_products = set(orders[order_id]["products"])  
        new_products = set(products)  
        added_products = list(new_products - old_products)  
        orders[order_id]["title"] = title  
        orders[order_id]["products"] += [p for p in added_products if p not in orders[order_id]["products"]]  
        # الحفاظ على الأسعار القديمة دون المساس بها
        for p in added_products:
            if p not in pricing[order_id]:
                pricing[order_id][p] = {}  # إضافة المنتج الجديد دون التأثير على البيانات القديمة

        await show_buttons(message.chat_id, context, user_id, order_id)  
        return  

    order_id = str(uuid.uuid4())[:8]  
    invoice_no = get_invoice_number()  
    orders[order_id] = {"user_id": user_id, "title": title, "products": products}  
    pricing[order_id] = {}  
    invoice_numbers[order_id] = invoice_no  
    await message.reply_text(f"استلمت الطلب: {title} ({len(products)} منتج)")  
    await show_buttons(message.chat_id, context, user_id, order_id)

async def show_buttons(chat_id, context, user_id, order_id):
    order = orders[order_id]
    buttons = []
    for p in order["products"]:
        is_done = p in pricing[order_id] and 'buy' in pricing[order_id][p] and 'sell' in pricing[order_id][p]
        label = f"✅ {p}" if is_done else p
        buttons.append([InlineKeyboardButton(label, callback_data=f"{order_id}|{p}")])
    markup = InlineKeyboardMarkup(buttons)
    if order_id in last_button_message:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=last_button_message[order_id])
        except:
            pass
    msg = await context.bot.send_message(chat_id=chat_id, text=f"اضغط على منتج لتحديد السعر من {order['title']}:", reply_markup=markup)
    last_button_message[order_id] = msg.message_id

async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id, product = query.data.split("|", 1)
    user_id = query.from_user.id
    current_product[user_id] = {"order_id": order_id, "product": product}
    await query.message.reply_text(f"بيش اشتريت '{product}'؟")
    return ASK_BUY

async def receive_buy_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    data = current_product.get(user_id)
    if not data: return
    order_id, product = data["order_id"], data["product"]
    try:
        price = float(update.message.text)
    except:
        await update.message.reply_text("دكتب عدل بيش اشتريت.")
        return ASK_BUY
    pricing[order_id].setdefault(product, {})["buy"] = price
    await update.message.reply_text(f"بيش راح تبيع '{product}'؟")
    return ASK_SELL

async def receive_sell_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global daily_profit
    user_id = update.message.from_user.id
    data = current_product.get(user_id)
    if not data: return
    order_id, product = data["order_id"], data["product"]
    try:
        price = float(update.message.text)
    except:
        await update.message.reply_text("دكتب عدل بيش حتبيع.")
        return ASK_SELL
    pricing[order_id][product]["sell"] = price
    await update.message.reply_text(f"تم حفظ السعر لـ '{product}'.")
    await show_buttons(update.effective_chat.id, context, user_id, order_id)
    order = orders[order_id]
    invoice = invoice_numbers.get(order_id, "غير معروف")
    if all(p in pricing[order_id] and "buy" in pricing[order_id][p] and "sell" in pricing[order_id][p] for p in order["products"]):
        total_buy = total_sell = 0
        summary = [f"رقم الفاتورة: {invoice}", f"عنوان الزبون: {order['title']}"]
        for p in order["products"]:
            buy = pricing[order_id][p]["buy"]
            sell = pricing[order_id][p]["sell"]
            profit = sell - buy
            total_buy += buy
            total_sell += sell
            summary.append(f"{p} - شراء: {buy}, بيع: {sell}, ربح: {profit}")
        net_profit = total_sell - total_buy
        daily_profit += net_profit
        await update.message.reply_text("\n".join(summary) + f"\n\nالمجموع شراء: {total_buy}\nالمجموع بيع: {total_sell}\nالربح الكلي: {net_profit}")
        running_total = 0
        customer_lines = []
        for p in order["products"]:
            sell = pricing[order_id][p]["sell"]
            running_total += sell
            customer_lines.append(f"{p} - {sell} = {running_total}")
        customer_text = f"أبو الأكبر للتوصيل\nرقم الفاتورة: {invoice}\nعنوان الزبون: {order['title']}\n\nالمواد:\n" + "\n".join(customer_lines)
        customer_text += f"\n\nمجموع القائمة الكلي: {running_total} (بدون كلفة التوصيل)"
        await update.message.reply_text("نسخة الزبون:\n" + customer_text)
        encoded = customer_text.replace(" ", "%20").replace("\n", "%0A")
        wa_link = f"https://wa.me/?text={encoded}"
        await update.message.reply_text("دوس الرابط حتى تروح لابو الاكبر:\n" + wa_link)
    return ConversationHandler.END

async def show_profit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    await update.message.reply_text(f"الربح التراكمي: {daily_profit} دينار")

async def reset_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    global daily_profit, orders, pricing, invoice_numbers, last_button_message
    daily_profit = 0.0
    orders.clear()
    pricing.clear()
    invoice_numbers.clear()
    last_button_message.clear()
    await update.message.reply_text("تم تصفير الأرباح ومسح كل الطلبات.")

async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    total_orders = len(orders)
    total_products = sum(len(o["products"]) for o in orders.values())
    total_buy = total_sell = 0
    product_counter = Counter()
    details = []
    for order_id, order in orders.items():
        invoice = invoice_numbers.get(order_id, "غير معروف")
        details.append(f"\nفاتورة: {invoice}\nعنوان: {order['title']}")
        for p in order["products"]:
            product_counter[p] += 1
            if p in pricing[order_id]:
                buy = pricing[order_id][p].get("buy", 0)
                sell = pricing[order_id][p].get("sell", 0)
                profit = sell - buy
                total_buy += buy
                total_sell += sell
                details.append(f"{p} - شراء: {buy}, بيع: {sell}, ربح: {profit}")
    top_product = product_counter.most_common(1)[0][0] if product_counter else "لا يوجد"
    result = "\n".join(details)
    result += f"\n\nإجمالي الطلبات: {total_orders}"
    result += f"\nإجمالي المنتجات: {total_products}"
    result += f"\nأكثر منتج تم طلبه: {top_product}"
    result += f"\n\nمجموع الشراء: {total_buy}\nمجموع البيع: {total_sell}\nمجموع الأرباح: {total_sell - total_buy}"
    await update.message.reply_text(result)

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^الارباح$|^ارباح$"), show_profit))
app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^صفر$|^تصفير$"), reset_all))
app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^التقارير$|^تقرير$|^تقارير$"), show_report))
app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, edited_message))

conv_handler = ConversationHandler(
    entry_points=[
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order),
        CallbackQueryHandler(product_selected)
    ],
    states={
        ASK_BUY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_buy_price)],
        ASK_SELL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sell_price)],
    },
    fallbacks=[]
)
app.add_handler(conv_handler)

print("Bot is running...")
app.run_polling()
