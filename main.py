from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup from telegram.ext import ( ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters ) import uuid import os

orders = {} pricing = {} current_product = {} last_button_message = {}

ASK_BUY, ASK_SELL = range(2) TOKEN = "7508502359:AAFtlXVMJGUiWaeqJZc0o03Yy-SgVYE_xz8"

إعداد رقم فاتورة تسلسلي

counter_file = "invoice_counter.txt" profit_file = "daily_profit.txt" if not os.path.exists(counter_file): with open(counter_file, "w") as f: f.write("1") if not os.path.exists(profit_file): with open(profit_file, "w") as f: f.write("0")

def get_invoice_number(): with open(counter_file, "r") as f: current = int(f.read().strip()) with open(counter_file, "w") as f: f.write(str(current + 1)) return current

def add_profit(amount): with open(profit_file, "r") as f: current = float(f.read().strip()) with open(profit_file, "w") as f: f.write(str(current + amount))

def get_profit(): with open(profit_file, "r") as f: return float(f.read().strip())

def reset_profit(): with open(profit_file, "w") as f: f.write("0")

invoice_numbers = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("دز رساله بيها عنوان الزبون بالسطر الاول وبنفس الرساله المنتجات الي يريدها الزبون واحد جوا الثاني .")

async def profit(update: Update, context: ContextTypes.DEFAULT_TYPE): amount = get_profit() await update.message.reply_text(f"الربح الحالي: {amount} دينار")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE): reset_profit() await update.message.reply_text("تم تصفير الأرباح.")

async def receive_order(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id = update.message.from_user.id lines = update.message.text.strip().split('\n') if len(lines) < 2: await update.message.reply_text("دز رساله بيها عنوان الزبون بالسطر الاول وبنفس الرساله المنتجات الي يريدها الزبون واحد جوا الثاني .") return

title = lines[0]
products = lines[1:]
order_id = str(uuid.uuid4())[:8]
invoice_no = get_invoice_number()

orders[order_id] = {
    "user_id": user_id,
    "title": title,
    "products": products
}
pricing[order_id] = {}
invoice_numbers[order_id] = invoice_no

await update.message.reply_text(f"استلمت طلبية: {title}\nعدد المنتجات: {len(products)}")
await show_buttons(update.effective_chat.id, context, user_id, order_id)

async def show_buttons(chat_id, context, user_id, order_id): order = orders[order_id] buttons = [] for p in order["products"]: is_done = p in pricing[order_id] and 'buy' in pricing[order_id][p] and 'sell' in pricing[order_id][p] label = f"✅ {p}" if is_done else p buttons.append([InlineKeyboardButton(label, callback_data=f"{order_id}|{p}")])

markup = InlineKeyboardMarkup(buttons)

if order_id in last_button_message:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=last_button_message[order_id])
    except:
        pass

msg = await context.bot.send_message(
    chat_id=chat_id,
    text=f"اضغط على منتج لتحديد او تحديث السعر من {order['title']}:",
    reply_markup=markup
)

last_button_message[order_id] = msg.message_id

async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE): query = update.callback_query await query.answer() order_id, product = query.data.split("|", 1) user_id = query.from_user.id current_product[user_id] = {"order_id": order_id, "product": product} await query.message.reply_text(f"بيش اشتريت '{product}'؟") return ASK_BUY

async def receive_buy_price(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id = update.message.from_user.id data = current_product.get(user_id) if not data: return order_id, product = data["order_id"], data["product"] try: price = float(update.message.text) except ValueError: await update.message.reply_text("دكتب عدل بيش اشتريت.") return ASK_BUY

pricing[order_id].setdefault(product, {})["buy"] = price
await update.message.reply_text(f"بيش ح نبيع '{product}'؟")
return ASK_SELL

async def receive_sell_price(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id = update.message.from_user.id data = current_product.get(user_id) if not data: return order_id, product = data["order_id"], data["product"] try: price = float(update.message.text) except ValueError: await update.message.reply_text("دكتب عدل بيش  حنبيع.") return ASK_SELL

pricing[order_id][product]["sell"] = price
await update.message.reply_text(f"تم حفظ السعر لـ '{product}'.")
await show_buttons(update.effective_chat.id, context, user_id, order_id)

order = orders[order_id]
invoice = invoice_numbers.get(order_id, "غير معروف")
if all(p in pricing[order_id] and 'buy' in pricing[order_id][p] and 'sell' in pricing[order_id][p] for p in order["products"]):
    summary = [f"رقم الفاتورة: {invoice}", f"عنوان الزبون: {order['title']}"]
    total_buy = total_sell = 0
    for p in order["products"]:
        buy = pricing[order_id][p]["buy"]
        sell = pricing[order_id][p]["sell"]
        profit = sell - buy
        total_buy += buy
        total_sell += sell
        add_profit(profit)
        summary.append(f"{p} - شراء: {buy}, بيع: {sell}, ربح: {profit}")
    net_profit = total_sell - total_buy
    result = "\n".join(summary)
    result += f"\n\nالمجموع شراء: {total_buy}\nالمجموع بيع: {total_sell}\nالربح الكلي: {net_profit}"
    await update.message.reply_text(result)

    customer_lines = []
    running_total = 0
    for p in order["products"]:
        sell = pricing[order_id][p]["sell"]
        running_total += sell
        customer_lines.append(f"{p} - {sell} = {running_total}")

    customer_text = (
        f"أبو الأكبر للتوصيل\n"
        f"رقم الفاتورة: {invoice}\n"
        f"عنوان الزبون: {order['title']}\n\n"
        f"المواد:\n" + "\n".join(customer_lines)
    )
    customer_text += f"\n\nمجموع القائمة الكلي: {running_total} (بدون كلفة التوصيل)"
    await update.message.reply_text("نسخة الزبون:\n" + customer_text)

    encoded = customer_text.replace(" ", "%20").replace("\n", "%0A")
    wa_link = f"https://wa.me/?text={encoded}"
    await update.message.reply_text("دوس الرابط حتى تروح لابو الاكبر:\n" + wa_link)

return ConversationHandler.END

app = ApplicationBuilder().token(TOKEN).build() app.add_handler(CommandHandler("start", start)) app.add_handler(CommandHandler("الارباح", profit)) app.add_handler(CommandHandler("تصفير_الارباح", reset))

conv_handler = ConversationHandler( entry_points=[ MessageHandler(filters.TEXT & ~filters.COMMAND, receive_order), CallbackQueryHandler(product_selected) ], states={ ASK_BUY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_buy_price)], ASK_SELL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sell_price)], }, fallbacks=[] ) app.add_handler(conv_handler)

print("Bot is running...") app.run_polling()

