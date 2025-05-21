from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters
)
import uuid

orders = {}
pricing = {}
current_product = {}
last_button_message = {}

ASK_BUY, ASK_SELL = range(2)
TOKEN = "7508502359:AAFtlXVMJGUiWaeqJZc0o03Yy-SgVYE_xz8"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("أرسل عنوان الطلب في السطر الأول، ثم المنتجات كل واحدة في سطر.")

async def receive_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    lines = update.message.text.strip().split('\n')
    if len(lines) < 2:
        await update.message.reply_text("أرسل العنوان في السطر الأول، وكل منتج في سطر جديد.")
        return

    title = lines[0]
    products = lines[1:]
    order_id = str(uuid.uuid4())[:8]

    orders[order_id] = {
        "user_id": user_id,
        "title": title,
        "products": products
    }
    pricing[order_id] = {}

    await update.message.reply_text(f"تم استلام الطلب بعنوان: {title}\nعدد المنتجات: {len(products)}")
    await show_buttons(update.effective_chat.id, context, user_id, order_id)

async def show_buttons(chat_id, context, user_id, order_id):
    order = orders[order_id]
    buttons = []
    for p in order["products"]:
        is_done = p in pricing[order_id] and 'buy' in pricing[order_id][p] and 'sell' in pricing[order_id][p]
        label = f"✅ {p}" if is_done else p
        buttons.append([InlineKeyboardButton(label, callback_data=f"{order_id}|{p}")])

    markup = InlineKeyboardMarkup(buttons)

    # حذف الرسالة السابقة إن وُجدت
    if user_id in last_button_message:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=last_button_message[user_id])
        except:
            pass

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"اضغط على منتج لتحديد السعر من {order['title']}:",
        reply_markup=markup
    )

    # حفظ رقم الرسالة الجديدة
    last_button_message[user_id] = msg.message_id

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
    except ValueError:
        await update.message.reply_text("رجاءً أرسل رقم صحيح لسعر الشراء.")
        return ASK_BUY

    pricing[order_id].setdefault(product, {})["buy"] = price
    await update.message.reply_text(f"بيش راح تبيع '{product}'؟")
    return ASK_SELL

async def receive_sell_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    data = current_product.get(user_id)
    if not data: return
    order_id, product = data["order_id"], data["product"]
    try:
        price = float(update.message.text)
    except ValueError:
        await update.message.reply_text("رجاءً أرسل رقم صحيح لسعر البيع.")
        return ASK_SELL

    pricing[order_id][product]["sell"] = price
    await update.message.reply_text(f"تم حفظ السعر لـ '{product}'.")

    await show_buttons(update.effective_chat.id, context, user_id, order_id)

    order = orders[order_id]
    if all(p in pricing[order_id] and 'buy' in pricing[order_id][p] and 'sell' in pricing[order_id][p] for p in order["products"]):
        summary = [f"عنوان الزبون: {order['title']}"]
        total_buy = total_sell = 0
        for p in order["products"]:
            buy = pricing[order_id][p]["buy"]
            sell = pricing[order_id][p]["sell"]
            profit = sell - buy
            total_buy += buy
            total_sell += sell
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

        customer_text = f"أبو الأكبر للتوصيل\nعنوان الزبون: {order['title']}\n\nالمواد:\n" + "\n".join(customer_lines)
        customer_text += f"\n\nمجموع القائمة الكلي: {running_total} (بدون كلفة التوصيل)"
        await update.message.reply_text("نسخة الزبون:\n" + customer_text)

        encoded = customer_text.replace(" ", "%20").replace("\n", "%0A")
        wa_link = f"https://wa.me/?text={encoded}"
        await update.message.reply_text("رابط إرسال الفاتورة بالواتساب:\n" + wa_link)

    return ConversationHandler.END

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))

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
