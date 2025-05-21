import asyncio  # تأكد موجود بالأعلى

buttons_messages = {}  # تخزين معرف الرسالة القديمة

async def show_buttons(chat_id, context, order_id):
    order = orders[order_id]
    buttons = []
    for p in order["products"]:
        is_done = (
            p in pricing[order_id]
            and 'buy' in pricing[order_id][p]
            and 'sell' in pricing[order_id][p]
        )
        label = f"✅ {p}" if is_done else p
        buttons.append([InlineKeyboardButton(label, callback_data=f"{order_id}|{p}")])

    markup = InlineKeyboardMarkup(buttons)

    # إرسال الرسالة الجديدة أولاً
    new_message = await context.bot.send_message(
        chat_id=chat_id,
        text=f"اضغط على منتج لتحديد السعر من {order['title']}:",
        reply_markup=markup
    )

    # انتظر نصف ثانية ثم احذف الرسالة القديمة
    await asyncio.sleep(0.5)

    if order_id in buttons_messages:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=buttons_messages[order_id])
        except:
            pass  # ممكن تكون انحذفت أو ما موجودة

    # خزّن الرسالة الجديدة
    buttons_messages[order_id] = new_message.message_id
