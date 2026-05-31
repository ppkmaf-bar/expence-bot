import os
import json
import logging
import re
import base64
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").lower().lstrip("@")

# Два отдельных Apps Script URL — полная изоляция данных
COUPLE_SCRIPT_URL = os.environ["COUPLE_SCRIPT_URL"]  # таблица личных расходов
BAR_SCRIPT_URL    = os.environ["BAR_SCRIPT_URL"]     # таблица бара

ADMIN_CHAT_ID  = int(os.environ.get("ADMIN_CHAT_ID", "0"))
COUPLE_CHAT_ID = int(os.environ.get("COUPLE_CHAT_ID", "0"))

def write_to_sheet(url: str, sheet_name: str, rows: list):
    payload = {"sheet": sheet_name, "rows": rows}
    r = requests.post(url, json=payload, timeout=15)
    return r.json()

def read_from_sheet(url: str, sheet_name: str) -> list:
    r = requests.get(url, params={"sheet": sheet_name}, timeout=15)
    return r.json().get("rows", [])

def image_to_base64(file_bytes: bytes) -> str:
    return base64.standard_b64encode(file_bytes).decode("utf-8")

def get_sender_name(msg) -> str:
    user = msg.from_user
    if user.first_name and user.last_name:
        return f"{user.first_name} {user.last_name}"
    return user.first_name or user.username or "Неизвестно"

# ── ЛИЧНЫЕ РАСХОДЫ ────────────────────────────────────────────────────────────

def parse_expense(text: str, sender_name: str) -> list:
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = f"""Разбери сообщение и извлеки расходы пары. Сегодня: {today}.
Отправитель: {sender_name}

Верни ТОЛЬКО JSON-массив:
[{{"date":"ДД.ММ.ГГГГ","who":"Имя","amount":число,"type":"категория","desc":"описание"}}]

Правила:
- Все суммы в рублях
- Если написано "я" или имя не указано — используй: {sender_name}
- Категории: еда, транспорт, жильё, здоровье, развлечения, бар, другое
- Если расходов нет — верни []

Сообщение: "{text}" """
    resp = claude.messages.create(model="claude-sonnet-4-20250514", max_tokens=1000,
        messages=[{"role": "user", "content": prompt}])
    raw = re.sub(r"```json|```", "", resp.content[0].text).strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except:
        return []

def answer_expense_question(question: str) -> str:
    rows = read_from_sheet(COUPLE_SCRIPT_URL, "Расходы")
    table = "\n".join(["\t".join(str(c) for c in r) for r in rows[-200:]])
    prompt = f"""Ты финансовый помощник пары. Таблица личных расходов (в рублях):
{table}
Ответь кратко на русском. При вопросе о балансе — посчитай кто сколько потратил и кто кому должен (расходы делятся поровну).
Вопрос: {question}"""
    resp = claude.messages.create(model="claude-sonnet-4-20250514", max_tokens=600,
        messages=[{"role": "user", "content": prompt}])
    return resp.content[0].text.strip()

# ── НАКЛАДНЫЕ ─────────────────────────────────────────────────────────────────

def parse_invoice_image(image_bytes: bytes, caption: str, sender_name: str) -> list:
    img_b64 = image_to_base64(image_bytes)
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = f"""Разбери фото накладной бара. Сегодня: {today}. Принял: {sender_name}.
Верни ТОЛЬКО JSON-массив:
[{{"date":"ДД.ММ.ГГГГ","supplier":"поставщик","product":"товар","qty":кол,"unit":"ед","price":цена,"total":сумма,"category":"категория"}}]
Категории: алкоголь, безалкогольные напитки, еда/закуски, расходники, другое
Дату бери из накладной, если нет — сегодняшнюю.
Контекст: {caption}"""
    resp = claude.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=2000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
            {"type": "text", "text": prompt}
        ]}])
    raw = re.sub(r"```json|```", "", resp.content[0].text).strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except:
        return []

def parse_invoice_text(text: str, sender_name: str) -> list:
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = f"""Разбери текстовую накладную бара. Сегодня: {today}. Принял: {sender_name}.
Верни ТОЛЬКО JSON-массив:
[{{"date":"ДД.ММ.ГГГГ","supplier":"поставщик","product":"товар","qty":кол,"unit":"ед","price":цена,"total":сумма,"category":"категория"}}]
Категории: алкоголь, безалкогольные напитки, еда/закуски, расходники, другое
Если данных нет — верни [].
Текст: "{text}" """
    resp = claude.messages.create(model="claude-sonnet-4-20250514", max_tokens=2000,
        messages=[{"role": "user", "content": prompt}])
    raw = re.sub(r"```json|```", "", resp.content[0].text).strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except:
        return []

# ── СМЕНЫ ─────────────────────────────────────────────────────────────────────

def parse_shift_report(text: str, sender_name: str) -> dict:
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = f"""Разбери отчёт о смене бара. Сегодня: {today}. Закрыл смену: {sender_name}.
Верни ТОЛЬКО JSON:
{{"date":"ДД.ММ.ГГГГ","closed_by":"имя","total":число,"bar":число,"services":число,"acquiring":число,"terminal":число,"cash":число,"notes":"заметки"}}
Все суммы числами без знаков (33460 а не 33.460₽).
closed_by — имя из текста если упомянуто, иначе: {sender_name}
Текст: "{text}" """
    resp = claude.messages.create(model="claude-sonnet-4-20250514", max_tokens=800,
        messages=[{"role": "user", "content": prompt}])
    raw = re.sub(r"```json|```", "", resp.content[0].text).strip()
    try:
        return json.loads(raw)
    except:
        return {}

def parse_shift_receipts(image_bytes: bytes, sender_name: str) -> list:
    img_b64 = image_to_base64(image_bytes)
    prompt = f"""Посмотри на фото чека. Извлеки все позиции. Прислал: {sender_name}.
Верни ТОЛЬКО JSON-массив:
[{{"product":"название","qty":кол,"price":цена,"total":сумма}}]
Если не чек или ничего не видно — верни []."""
    resp = claude.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=1000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
            {"type": "text", "text": prompt}
        ]}])
    raw = re.sub(r"```json|```", "", resp.content[0].text).strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except:
        return []

def answer_admin_question(question: str) -> str:
    shifts = read_from_sheet(BAR_SCRIPT_URL, "Смены")
    invoices = read_from_sheet(BAR_SCRIPT_URL, "Накладные")
    shifts_text = "\n".join(["\t".join(str(c) for c in r) for r in shifts[-50:]])
    invoices_text = "\n".join(["\t".join(str(c) for c in r) for r in invoices[-100:]])
    prompt = f"""Ты финансовый помощник бара. Все суммы в рублях.

СМЕНЫ:
{shifts_text}

НАКЛАДНЫЕ:
{invoices_text}

Ответь кратко на русском.
Вопрос: {question}"""
    resp = claude.messages.create(model="claude-sonnet-4-20250514", max_tokens=800,
        messages=[{"role": "user", "content": prompt}])
    return resp.content[0].text.strip()

# ── INTENT ────────────────────────────────────────────────────────────────────

def classify_intent(text: str) -> str:
    lower = text.lower()
    if any(w in lower for w in ["накладная", "накладн", "поставк", "поставщик", "привезли", "приход товар"]):
        return "invoice"
    if any(w in lower for w in ["смена", "выручка", "отчёт", "результат смен", "закрыл смену"]):
        return "shift"
    if any(w in lower for w in ["сколько", "баланс", "итого", "кто должен", "статистика",
                                  "покажи", "список", "сводка", "продаж", "?"]):
        return "question"
    if re.search(r"\d+[\.,]?\d*\s*(₽|руб|рублей|€|евро)?", lower):
        return "expense"
    return "unknown"

# ── ХЭНДЛЕРЫ ──────────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    chat_id = msg.chat_id
    chat_type = msg.chat.type
    text = (msg.text or msg.caption or "").strip()
    sender_name = get_sender_name(msg)
    is_admin_chat = (chat_id == ADMIN_CHAT_ID)
    is_couple_chat = (chat_id == COUPLE_CHAT_ID)

    if chat_type in ("group", "supergroup"):
        is_mentioned = BOT_USERNAME and f"@{BOT_USERNAME}" in text.lower()
        is_reply_to_bot = (msg.reply_to_message and
                           msg.reply_to_message.from_user and
                           msg.reply_to_message.from_user.is_bot)
        if not is_mentioned and not is_reply_to_bot:
            return
        text = re.sub(f"@{re.escape(BOT_USERNAME)}", "", text, flags=re.IGNORECASE).strip()

    added_ts = datetime.now().strftime("%d.%m.%Y %H:%M")

    try:
        # ── ЧАТ АДМИНОВ ──────────────────────────────────────────────────────
        if is_admin_chat or (ADMIN_CHAT_ID == 0 and not is_couple_chat):

            if msg.photo:
                photo = msg.photo[-1]
                file = await context.bot.get_file(photo.file_id)
                img_bytes = bytes(await file.download_as_bytearray())
                caption_lower = text.lower()

                if any(w in caption_lower for w in ["накладная", "накладн", "поставк", "привезли"]):
                    await msg.reply_text("📋 Разбираю накладную...")
                    items = parse_invoice_image(img_bytes, text, sender_name)
                    if not items:
                        await msg.reply_text("Не удалось распознать. Попробуй прислать фото чётче или текстом.")
                        return
                    rows = [[i.get("date",""), i.get("supplier",""), i.get("product",""),
                             i.get("qty",""), i.get("unit",""), i.get("price",""),
                             i.get("total",""), i.get("category","другое"), sender_name, added_ts] for i in items]
                    write_to_sheet(BAR_SCRIPT_URL, "Накладные", rows)
                    lines = [f"• {i.get('product','')} — {i.get('qty','')} {i.get('unit','')} × {i.get('price','')}₽ = {i.get('total','')}₽" for i in items]
                    await msg.reply_text(f"✅ Накладная от {sender_name}:\n" + "\n".join(lines))
                else:
                    await msg.reply_text("🧾 Разбираю чек...")
                    items = parse_shift_receipts(img_bytes, sender_name)
                    if not items:
                        await msg.reply_text("Не удалось распознать чек.")
                        return
                    rows = [[added_ts, "—", i.get("product",""), i.get("qty",""), "шт",
                             i.get("price",""), i.get("total",""), "закупка", sender_name, added_ts] for i in items]
                    write_to_sheet(BAR_SCRIPT_URL, "Накладные", rows)
                    lines = [f"• {i.get('product','')} × {i.get('qty','')} = {i.get('total','')}₽" for i in items]
                    await msg.reply_text(f"✅ Чек от {sender_name}:\n" + "\n".join(lines))
                return

            if not text:
                return

            intent = classify_intent(text)

            if intent == "shift":
                await msg.reply_text("📊 Разбираю отчёт смены...")
                data = parse_shift_report(text, sender_name)
                if not data:
                    await msg.reply_text(
                        "Не удалось разобрать. Пришли в формате:\n"
                        "«28.05.26 Выручка: 33460₽\n— бар: 8190₽\n— услуги: 25270₽\n"
                        "Оплата:\n— эквайринг: 0₽\n— терминал: 25700₽\n— наличные: 7760₽»"
                    )
                    return
                closed_by = data.get("closed_by", sender_name)
                write_to_sheet(BAR_SCRIPT_URL, "Смены", [[
                    data.get("date",""), closed_by, data.get("total",""), data.get("bar",""),
                    data.get("services",""), data.get("acquiring",""), data.get("terminal",""),
                    data.get("cash",""), data.get("notes",""), added_ts
                ]])
                await msg.reply_text(
                    f"✅ Смена {data.get('date','')} записана\n"
                    f"Закрыл: {closed_by}\n"
                    f"Выручка: {data.get('total','')}₽\n"
                    f"• Бар: {data.get('bar','')}₽\n"
                    f"• Услуги: {data.get('services','')}₽\n"
                    f"Оплата:\n"
                    f"• Эквайринг: {data.get('acquiring','')}₽\n"
                    f"• Терминал: {data.get('terminal','')}₽\n"
                    f"• Наличные: {data.get('cash','')}₽"
                )

            elif intent == "invoice":
                await msg.reply_text("📋 Разбираю накладную...")
                items = parse_invoice_text(text, sender_name)
                if not items:
                    await msg.reply_text("Не нашёл позиций в накладной.")
                    return
                rows = [[i.get("date",""), i.get("supplier",""), i.get("product",""),
                         i.get("qty",""), i.get("unit",""), i.get("price",""),
                         i.get("total",""), i.get("category",""), sender_name, added_ts] for i in items]
                write_to_sheet(BAR_SCRIPT_URL, "Накладные", rows)
                lines = [f"• {i.get('product','')} {i.get('qty','')} {i.get('unit','')} = {i.get('total','')}₽" for i in items]
                await msg.reply_text(f"✅ Накладная от {sender_name}:\n" + "\n".join(lines))

            elif intent == "question":
                await msg.reply_text("🔍 Смотрю данные...")
                answer = answer_admin_question(text)
                await msg.reply_text(answer)

            else:
                await msg.reply_text(
                    "Привет! Я бот бара 🍸\n\n"
                    "• Фото с подписью «накладная» → занесу поставку\n"
                    "• Фото чека → занесу покупки\n"
                    "• Текст отчёта смены → занесу выручку\n"
                    "• «Выручка за май?» → отвечу по данным"
                )

        # ── ЧАТ ПАРЫ — доступ только к своей таблице ─────────────────────────
        else:
            if not text:
                return

            intent = classify_intent(text)

            if intent == "question":
                await msg.reply_text("🔍 Смотрю таблицу...")
                answer = answer_expense_question(text)
                await msg.reply_text(answer)

            elif intent in ("expense", "unknown"):
                expenses = parse_expense(text, sender_name)
                if expenses:
                    rows = [[e.get("date",""), e.get("who", sender_name), e.get("amount",""),
                             e.get("type","другое"), e.get("desc",""), added_ts] for e in expenses]
                    write_to_sheet(COUPLE_SCRIPT_URL, "Расходы", rows)
                    lines = [f"• {e.get('who', sender_name)}: {e['amount']}₽ — {e['desc']} ({e['type']})" for e in expenses]
                    await msg.reply_text("✅ Записал:\n" + "\n".join(lines))
                else:
                    await msg.reply_text(
                        "Привет! Я трекер расходов 💰\n\n"
                        "Примеры:\n«заплатил 450₽ за продукты»\n"
                        "«Серёжа оплатил 3200₽ аренду»\n"
                        "Или спроси: «Какой баланс?»"
                    )

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await msg.reply_text("❌ Ошибка, попробуй ещё раз.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if chat_id == ADMIN_CHAT_ID:
        await update.message.reply_text(
            "Привет! Я бот учёта бара 🍸\n\n"
            "• Фото накладной (подпись «накладная») → запишу поставку\n"
            "• Фото чека → запишу покупки\n"
            "• Отчёт смены текстом → запишу выручку\n"
            "• «Выручка за май?» → отвечу по данным"
        )
    else:
        await update.message.reply_text(
            "Привет! Я трекер совместных расходов 💰\n\n"
            "• «заплатил 450₽ за продукты» → запишу на тебя\n"
            "• «Серёжа оплатил 3200₽ аренду» → запишу на Серёжу\n"
            "• «Какой баланс?» → посчитаю"
        )

async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ID этого чата: `{update.message.chat_id}`",
        parse_mode="Markdown"
    )

if __name__ == "__main__":
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", get_chat_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_message))
    logger.info("Bot started")
    app.run_polling()
