import os
import re
import json
import base64
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
)
import anthropic
import requests

# ─────────────────────────────────────────────────────────────────────────────
# Настройка логирования
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Переменные окружения
# ─────────────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").lower().lstrip("@")

COUPLE_SCRIPT_URL = os.environ["COUPLE_SCRIPT_URL"]  # таблица Ppkfinance (личное)
BAR_SCRIPT_URL = os.environ["BAR_SCRIPT_URL"]        # таблица Бар (рабочее)

def _safe_int(name: str) -> int:
    """Читает chat_id из переменной окружения, устойчиво к пробелам/кавычкам."""
    raw = os.environ.get(name, "0").strip().strip('"').strip("'")
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"{name} не число: {raw!r}, ставлю 0")
        return 0

ADMIN_CHAT_ID = _safe_int("ADMIN_CHAT_ID")    # чат админов бара
COUPLE_CHAT_ID = _safe_int("COUPLE_CHAT_ID")  # чат пары

logger.info(f"ADMIN_CHAT_ID={ADMIN_CHAT_ID}  COUPLE_CHAT_ID={COUPLE_CHAT_ID}")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─────────────────────────────────────────────────────────────────────────────
# Работа с Google Sheets через Apps Script
# ─────────────────────────────────────────────────────────────────────────────
def write_to_sheet(url: str, sheet_name: str, rows: list) -> dict:
    try:
        payload = json.dumps({"sheet": sheet_name, "rows": rows})
        r = requests.get(url, params={"action": "write", "data": payload}, timeout=30)
        logger.info(f"write_to_sheet: status={r.status_code} body={r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            return {"status": "ok" if r.status_code == 200 else "error"}
    except Exception as e:
        logger.error(f"write_to_sheet error: {e}")
        return {"status": "error", "message": str(e)}

def read_from_sheet(url: str, sheet_name: str) -> list:
    try:
        r = requests.get(url, params={"sheet": sheet_name}, timeout=20)
        return r.json().get("rows", [])
    except Exception as e:
        logger.error(f"read_from_sheet error: {e}")
        return []

# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные
# ─────────────────────────────────────────────────────────────────────────────
def image_to_base64(file_bytes: bytes) -> str:
    return base64.standard_b64encode(file_bytes).decode("utf-8")

def get_sender_name(msg) -> str:
    user = msg.from_user
    if not user:
        return "Неизвестно"
    if user.first_name and user.last_name:
        return f"{user.first_name} {user.last_name}"
    return user.first_name or user.username or "Неизвестно"

def claude_text(prompt: str, max_tokens: int = 1000) -> str:
    resp = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()

def claude_vision(prompt: str, img_b64: str, max_tokens: int = 2000) -> str:
    resp = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                          "media_type": "image/jpeg", "data": img_b64}},
            {"type": "text", "text": prompt},
        ]}],
    )
    return resp.content[0].text.strip()

def parse_json(raw: str):
    """Чистит markdown-ограждения и парсит JSON. Возвращает None при ошибке."""
    cleaned = re.sub(r"```json|```", "", raw).strip()
    try:
        return json.loads(cleaned)
    except Exception as e:
        logger.error(f"JSON parse error: {e} | raw: {raw[:200]}")
        return None

def to_number(value):
    """Приводит '33.460₽' / '33 460' / '1,5' к числу. Иначе возвращает как есть.

    В русских отчётах точка обычно разделитель тысяч (33.460 = 33460),
    а запятая — дробная часть (1,5 = 1.5).
    """
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return value
    s = re.sub(r"[^\d,.\-]", "", value)
    if not s:
        return value
    # Запятая — десятичный разделитель
    has_comma = "," in s
    # Точка с ровно 3 цифрами после (и не запятая) — разделитель тысяч
    # 33.460 -> 33460, но 1.5 -> 1.5 (2 цифры), 33.46 -> 33.46
    if not has_comma and re.search(r"\.\d{3}(\.|$)", s):
        s = s.replace(".", "")
    if has_comma:
        s = s.replace(".", "").replace(",", ".")
    try:
        num = float(s)
        return int(num) if num.is_integer() else num
    except ValueError:
        return value

# ─────────────────────────────────────────────────────────────────────────────
# ЛИЧНЫЕ РАСХОДЫ (чат пары)
# ─────────────────────────────────────────────────────────────────────────────
def parse_expense(text: str, sender_name: str) -> list:
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = f"""Разбери сообщение и извлеки личные расходы пары. Сегодня: {today}.
Отправитель сообщения: {sender_name}

Верни ТОЛЬКО JSON-массив без markdown:
[{{"date":"ДД.ММ.ГГГГ","who":"Имя","amount":число,"type":"категория","desc":"описание"}}]

Правила:
- Все суммы в рублях, amount — только число
- Если написано "я"/"мне"/"оплатил" без имени — это отправитель: {sender_name}
- Если явно указано другое имя — используй его
- Категории: еда, транспорт, жильё, здоровье, развлечения, бар, другое
- Если расходов в сообщении нет — верни []

Сообщение: "{text}" """
    data = parse_json(claude_text(prompt))
    return data if isinstance(data, list) else []

def answer_expense_question(question: str) -> str:
    rows = read_from_sheet(COUPLE_SCRIPT_URL, "Расходы")
    table = "\n".join(["\t".join(str(c) for c in r) for r in rows[-200:]])
    prompt = f"""Ты финансовый помощник пары. Таблица личных расходов (в рублях):
{table}

Ответь кратко на русском, опираясь на цифры из таблицы.
При вопросе о балансе посчитай, кто сколько потратил и кто кому должен (расходы делятся поровну).
Вопрос: {question}"""
    return claude_text(prompt, max_tokens=600)

# ─────────────────────────────────────────────────────────────────────────────
# НАКЛАДНЫЕ (чат бара)
# ─────────────────────────────────────────────────────────────────────────────
INVOICE_SCHEMA = """[{"date":"ДД.ММ.ГГГГ","supplier":"поставщик","product":"товар","qty":число,"unit":"ед","price":число,"total":число,"category":"категория"}]"""

def parse_invoice_image(image_bytes: bytes, caption: str, sender_name: str) -> list:
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = f"""Разбери фото накладной бара. Сегодня: {today}. Принял: {sender_name}.
Верни ТОЛЬКО JSON-массив без markdown:
{INVOICE_SCHEMA}
Категории: алкоголь, безалкогольные напитки, еда/закуски, расходники, другое
qty/price/total — только числа. Дату бери из накладной, если нет — сегодняшнюю.
Если ничего не распознать — верни [].
Контекст от пользователя: {caption}"""
    data = parse_json(claude_vision(prompt, image_to_base64(image_bytes)))
    return data if isinstance(data, list) else []

def parse_invoice_text(text: str, sender_name: str) -> list:
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = f"""Разбери текстовую накладную бара. Сегодня: {today}. Принял: {sender_name}.
Верни ТОЛЬКО JSON-массив без markdown:
{INVOICE_SCHEMA}
Категории: алкоголь, безалкогольные напитки, еда/закуски, расходники, другое
qty/price/total — только числа. Если данных нет — верни [].
Текст: "{text}" """
    data = parse_json(claude_text(prompt, max_tokens=2000))
    return data if isinstance(data, list) else []

# ─────────────────────────────────────────────────────────────────────────────
# СМЕНЫ (чат бара)
# ─────────────────────────────────────────────────────────────────────────────
def parse_shift_report(text: str, sender_name: str) -> dict:
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = f"""Разбери отчёт о смене бара. Сегодня: {today}. Закрыл смену: {sender_name}.
Верни ТОЛЬКО JSON без markdown:
{{"date":"ДД.ММ.ГГГГ","closed_by":"имя","total":число,"bar":число,"services":число,"acquiring":число,"terminal":число,"cash":число,"notes":"заметки"}}

Все суммы — целые числа без знаков и пробелов (33460, не "33.460₽").
Если в тексте указано имя закрывшего смену — используй его, иначе: {sender_name}.
Текст: "{text}" """
    data = parse_json(claude_text(prompt, max_tokens=800))
    if not isinstance(data, dict):
        return {}
    # подстраховка: приводим суммы к числам
    for key in ("total", "bar", "services", "acquiring", "terminal", "cash"):
        if key in data:
            data[key] = to_number(data[key])
    return data

def parse_shift_receipts(image_bytes: bytes, sender_name: str) -> list:
    prompt = f"""Посмотри на фото чека из бара. Извлеки все позиции. Прислал: {sender_name}.
Верни ТОЛЬКО JSON-массив без markdown:
[{{"product":"название","qty":число,"price":число,"total":число}}]
Если это не чек или ничего не видно — верни []."""
    data = parse_json(claude_vision(prompt, image_to_base64(image_bytes), max_tokens=1500))
    return data if isinstance(data, list) else []

def answer_admin_question(question: str) -> str:
    shifts = read_from_sheet(BAR_SCRIPT_URL, "Смены")
    invoices = read_from_sheet(BAR_SCRIPT_URL, "Накладные")
    shifts_text = "\n".join(["\t".join(str(c) for c in r) for r in shifts[-60:]])
    invoices_text = "\n".join(["\t".join(str(c) for c in r) for r in invoices[-120:]])
    prompt = f"""Ты финансовый помощник бара. Все суммы в рублях.

СМЕНЫ (таблица):
{shifts_text}

НАКЛАДНЫЕ (таблица):
{invoices_text}

Ответь кратко и по делу на русском, опираясь на цифры из таблиц.
Вопрос: {question}"""
    return claude_text(prompt, max_tokens=900)

# ─────────────────────────────────────────────────────────────────────────────
# Классификация намерения
# ─────────────────────────────────────────────────────────────────────────────
def classify_intent(text: str) -> str:
    lower = text.lower()
    if any(w in lower for w in ["накладная", "накладн", "поставк", "поставщик", "привезли", "приход товар"]):
        return "invoice"
    if any(w in lower for w in ["смена", "выручка", "отчёт", "отчет", "результат смен", "закрыл смену"]):
        return "shift"
    if any(w in lower for w in ["сколько", "баланс", "итого", "кто должен", "статистик",
                                 "покажи", "список", "сводка", "продаж", "?"]):
        return "question"
    if re.search(r"\d+[.,]?\d*\s*(₽|руб|рубл|р\b|€|евро|usd|\$)?", lower):
        return "expense"
    return "unknown"

# ─────────────────────────────────────────────────────────────────────────────
# Определение типа чата — ЕДИНАЯ ТОЧКА ИСТИНЫ
# ─────────────────────────────────────────────────────────────────────────────
def route_chat(chat_id: int) -> str:
    """Возвращает 'bar', 'couple' или 'unknown'. Явная маршрутизация без догадок."""
    if chat_id == ADMIN_CHAT_ID:
        return "bar"
    if chat_id == COUPLE_CHAT_ID:
        return "couple"
    return "unknown"

# ─────────────────────────────────────────────────────────────────────────────
# Должен ли бот реагировать в группе
# ─────────────────────────────────────────────────────────────────────────────
def should_respond_in_group(msg, text: str) -> bool:
    is_mentioned = BOT_USERNAME and (f"@{BOT_USERNAME}" in text.lower())
    is_reply_to_bot = bool(
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and msg.reply_to_message.from_user.is_bot
    )
    return is_mentioned or is_reply_to_bot

def strip_mention(text: str) -> str:
    if not BOT_USERNAME:
        return text.strip()
    return re.sub(f"@{re.escape(BOT_USERNAME)}", "", text, flags=re.IGNORECASE).strip()

# ─────────────────────────────────────────────────────────────────────────────
# Обработчики ветвей
# ─────────────────────────────────────────────────────────────────────────────
async def handle_bar(msg, text: str, sender_name: str, context):
    added_ts = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Фото: накладная (по подписи) или чек
    if msg.photo:
        file = await context.bot.get_file(msg.photo[-1].file_id)
        img_bytes = bytes(await file.download_as_bytearray())
        caption = text.lower()

        if any(w in caption for w in ["накладная", "накладн", "поставк", "привезли", "приход"]):
            await msg.reply_text("📋 Разбираю накладную...")
            items = parse_invoice_image(img_bytes, text, sender_name)
            if not items:
                await msg.reply_text("Не удалось распознать накладную. Пришли фото чётче или текстом.")
                return
            rows = [[i.get("date", ""), i.get("supplier", ""), i.get("product", ""),
                     to_number(i.get("qty", "")), i.get("unit", ""), to_number(i.get("price", "")),
                     to_number(i.get("total", "")), i.get("category", "другое"), sender_name, added_ts]
                    for i in items]
            write_to_sheet(BAR_SCRIPT_URL, "Накладные", rows)
            lines = [f"• {i.get('product','')} — {i.get('qty','')} {i.get('unit','')} × {i.get('price','')}₽ = {i.get('total','')}₽" for i in items]
            await msg.reply_text(f"✅ Накладная от {sender_name}:\n" + "\n".join(lines))
        else:
            await msg.reply_text("🧾 Разбираю чек...")
            items = parse_shift_receipts(img_bytes, sender_name)
            if not items:
                await msg.reply_text("Не удалось распознать чек.")
                return
            rows = [[added_ts, "—", i.get("product", ""), to_number(i.get("qty", "")), "шт",
                     to_number(i.get("price", "")), to_number(i.get("total", "")), "закупка", sender_name, added_ts]
                    for i in items]
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
        if not data or not data.get("total"):
            await msg.reply_text(
                "Не удалось разобрать. Пришли в формате:\n"
                "«28.05.26 Выручка: 33460 — бар: 8190 — услуги: 25270\n"
                "эквайринг: 0 — терминал: 25700 — наличные: 7760»"
            )
            return
        closed_by = data.get("closed_by") or sender_name
        write_to_sheet(BAR_SCRIPT_URL, "Смены", [[
            data.get("date", ""), closed_by, data.get("total", ""), data.get("bar", ""),
            data.get("services", ""), data.get("acquiring", ""), data.get("terminal", ""),
            data.get("cash", ""), data.get("notes", ""), added_ts,
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
        rows = [[i.get("date", ""), i.get("supplier", ""), i.get("product", ""),
                 to_number(i.get("qty", "")), i.get("unit", ""), to_number(i.get("price", "")),
                 to_number(i.get("total", "")), i.get("category", ""), sender_name, added_ts]
                for i in items]
        write_to_sheet(BAR_SCRIPT_URL, "Накладные", rows)
        lines = [f"• {i.get('product','')} {i.get('qty','')} {i.get('unit','')} = {i.get('total','')}₽" for i in items]
        await msg.reply_text(f"✅ Накладная от {sender_name}:\n" + "\n".join(lines))

    elif intent == "question":
        await msg.reply_text("🔍 Смотрю данные бара...")
        await msg.reply_text(answer_admin_question(text))

    else:
        await msg.reply_text(
            "Я бот учёта бара 🍸\n\n"
            "• Фото с подписью «накладная» → занесу поставку\n"
            "• Фото чека → занесу покупки\n"
            "• Текст отчёта смены → занесу выручку\n"
            "• «Выручка за май?» → отвечу по данным"
        )

async def handle_couple(msg, text: str, sender_name: str, context):
    added_ts = datetime.now().strftime("%d.%m.%Y %H:%M")

    # В чате пары фото не обрабатываем (личные расходы — текстом)
    if msg.photo and not text:
        return
    if not text:
        return

    intent = classify_intent(text)

    if intent == "question":
        await msg.reply_text("🔍 Смотрю таблицу...")
        await msg.reply_text(answer_expense_question(text))
        return

    # всё остальное пытаемся разобрать как расход
    expenses = parse_expense(text, sender_name)
    if expenses:
        rows = [[e.get("date", ""), e.get("who") or sender_name, to_number(e.get("amount", "")),
                 e.get("type", "другое"), e.get("desc", ""), added_ts] for e in expenses]
        write_to_sheet(COUPLE_SCRIPT_URL, "Расходы", rows)
        lines = [f"• {e.get('who') or sender_name}: {e.get('amount','')}₽ — {e.get('desc','')} ({e.get('type','')})" for e in expenses]
        await msg.reply_text("✅ Записал:\n" + "\n".join(lines))
    else:
        await msg.reply_text(
            "Я трекер совместных расходов 💰\n\n"
            "• «заплатил 450₽ за продукты» → запишу на тебя\n"
            "• «Серёжа оплатил 3200₽ аренду» → запишу на Серёжу\n"
            "• «Какой баланс?» → посчитаю"
        )

# ─────────────────────────────────────────────────────────────────────────────
# Главный обработчик сообщений
# ─────────────────────────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    chat_id = msg.chat_id
    chat_type = msg.chat.type
    text = (msg.text or msg.caption or "").strip()
    sender_name = get_sender_name(msg)

    route = route_chat(chat_id)
    logger.info(f"msg chat_id={chat_id} type={chat_type} route={route} text={text[:50]!r}")

    # Неизвестный чат — бот молчит (защита приватности!)
    if route == "unknown":
        logger.warning(f"Сообщение из неизвестного чата {chat_id} — игнорирую")
        return

    # В группах/супергруппах реагируем только на @упоминание или reply
    if chat_type in ("group", "supergroup"):
        if not should_respond_in_group(msg, text):
            return
        text = strip_mention(text)

    try:
        if route == "bar":
            await handle_bar(msg, text, sender_name, context)
        elif route == "couple":
            await handle_couple(msg, text, sender_name, context)
    except Exception as e:
        logger.error(f"handle_message error: {e}", exc_info=True)
        await msg.reply_text("❌ Произошла ошибка, попробуй ещё раз.")

# ─────────────────────────────────────────────────────────────────────────────
# Команды
# ─────────────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    route = route_chat(update.message.chat_id)
    if route == "bar":
        await update.message.reply_text(
            "Привет! Я бот учёта бара 🍸\n\n"
            "• Фото накладной (подпись «накладная») → запишу поставку\n"
            "• Фото чека → запишу покупки\n"
            "• Отчёт смены текстом → запишу выручку\n"
            "• «Выручка за май?» → отвечу по данным"
        )
    elif route == "couple":
        await update.message.reply_text(
            "Привет! Я трекер совместных расходов 💰\n\n"
            "• «заплатил 450₽ за продукты» → запишу на тебя\n"
            "• «Серёжа оплатил 3200₽ аренду» → запишу на Серёжу\n"
            "• «Какой баланс?» → посчитаю"
        )
    else:
        await update.message.reply_text(
            "Этот чат не подключён к боту.\n"
            f"ID этого чата: `{update.message.chat_id}`\n"
            "Передайте этот ID администратору для настройки.",
            parse_mode="Markdown"
        )

async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    route = route_chat(chat_id)
    await update.message.reply_text(
        f"ID этого чата: `{chat_id}`\n"
        f"Распознан как: *{route}*",
        parse_mode="Markdown"
    )

# ─────────────────────────────────────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", get_chat_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_message))
    logger.info("Bot started")
    app.run_polling()
