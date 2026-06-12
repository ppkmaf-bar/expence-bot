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
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").lower().lstrip("@")
COUPLE_SCRIPT_URL = os.environ["COUPLE_SCRIPT_URL"]
BAR_SCRIPT_URL = os.environ["BAR_SCRIPT_URL"]

def _safe_int(name: str) -> int:
    raw = os.environ.get(name, "0").strip().strip('"').strip("'")
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"{name} не число: {raw!r}, ставлю 0")
        return 0

ADMIN_CHAT_ID = _safe_int("ADMIN_CHAT_ID")
COUPLE_CHAT_ID = _safe_int("COUPLE_CHAT_ID")
logger.info(f"ADMIN_CHAT_ID={ADMIN_CHAT_ID}  COUPLE_CHAT_ID={COUPLE_CHAT_ID}")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets через Apps Script
# ─────────────────────────────────────────────────────────────────────────────
def write_to_sheet(url: str, sheet_name: str, rows: list) -> dict:
    try:
        payload = json.dumps({"sheet": sheet_name, "rows": rows})
        # Если данных мало — GET, если много — разбиваем на части
        if len(payload) < 1500:
            r = requests.get(url, params={"action": "write", "data": payload}, timeout=30)
        else:
            # Разбиваем на порции по 5 строк
            for i in range(0, len(rows), 5):
                chunk = rows[i:i+5]
                chunk_payload = json.dumps({"sheet": sheet_name, "rows": chunk})
                r = requests.get(url, params={"action": "write", "data": chunk_payload}, timeout=30)
                logger.info(f"write_to_sheet({sheet_name}) chunk {i//5+1}: status={r.status_code}")
            r_text = r.text if r else ""
            logger.info(f"write_to_sheet({sheet_name}): done, total rows={len(rows)}")
            return {"status": "ok", "written": len(rows)}
        logger.info(f"write_to_sheet({sheet_name}): status={r.status_code} body={r.text[:200]}")
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
def image_to_base64(fb: bytes) -> str:
    return base64.standard_b64encode(fb).decode("utf-8")

def get_sender_name(msg) -> str:
    u = msg.from_user
    if not u: return "Неизвестно"
    if u.first_name and u.last_name: return f"{u.first_name} {u.last_name}"
    return u.first_name or u.username or "Неизвестно"

def claude_text(prompt: str, max_tokens: int = 1000) -> str:
    r = claude.messages.create(model="claude-sonnet-4-6", max_tokens=max_tokens,
                               messages=[{"role": "user", "content": prompt}])
    return r.content[0].text.strip()

def claude_vision(prompt: str, img_b64: str, max_tokens: int = 2000) -> str:
    r = claude.messages.create(model="claude-sonnet-4-6", max_tokens=max_tokens,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
            {"type": "text", "text": prompt}]}])
    return r.content[0].text.strip()

def parse_json(raw: str):
    cleaned = re.sub(r"```json|```", "", raw).strip()
    try:
        return json.loads(cleaned)
    except Exception as e:
        logger.error(f"JSON parse error: {e} | raw: {raw[:300]}")
        return None

def to_number(value):
    if isinstance(value, (int, float)): return value
    if not isinstance(value, str): return value
    s = re.sub(r"[^\d,.\-]", "", value)
    if not s: return value
    has_comma = "," in s
    if not has_comma and re.search(r"\.\d{3}(\.|$)", s): s = s.replace(".", "")
    if has_comma: s = s.replace(".", "").replace(",", ".")
    try:
        num = float(s)
        return int(num) if num.is_integer() else num
    except ValueError:
        return value

# ═════════════════════════════════════════════════════════════════════════════
# ФИНАНСЫ ПАРЫ
# ═════════════════════════════════════════════════════════════════════════════

EXPENSE_TYPES = "зарплата админов, зарплата, школа, судьи, ведущие, комментаторы, хоз, бар, стройка, техника, аренда, чаши, уборка, охрана, маркетинг, призовой, прочее"
INCOME_TYPES = "бар, чаши, участие в турнире, фанки, аренда столов, прочее"

def parse_finance(text: str, sender_name: str) -> list:
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = f"""Разбери сообщение и извлеки финансовые операции. Сегодня: {today}.
Отправитель: {sender_name}

Верни ТОЛЬКО JSON-массив без markdown:
[{{"date":"ДД.ММ.ГГГГ","who":"Имя","operation":"расход или доход","amount":число,"type":"тип","desc":"описание"}}]

Правила:
- operation: "расход" если потратили/заплатили/купили, "доход" если получили/пришло/заработали
- amount — только число в рублях
- Если "я"/"мне" без имени — используй: {sender_name}

ТИПЫ РАСХОДОВ (строго из списка): {EXPENSE_TYPES}
ТИПЫ ДОХОДОВ (строго из списка): {INCOME_TYPES}

Если тип не подходит ни под один — ставь "прочее" и в desc укажи подробности.
Если операций нет — верни []

Сообщение: "{text}" """
    data = parse_json(claude_text(prompt))
    return data if isinstance(data, list) else []

def answer_finance_question(question: str) -> str:
    rows = read_from_sheet(COUPLE_SCRIPT_URL, "Финансы")
    table = "\n".join(["\t".join(str(c) for c in r) for r in rows[-200:]])
    prompt = f"""Ты финансовый помощник. Таблица финансов (в рублях):
{table}

Ответь кратко на русском, опираясь на данные.
Колонка "Операция" содержит "расход" или "доход".
Вопрос: {question}"""
    return claude_text(prompt, max_tokens=600)

# ═════════════════════════════════════════════════════════════════════════════
# БАР — классификация фото
# ═════════════════════════════════════════════════════════════════════════════

def classify_photo(img_b64: str, caption: str = "") -> str:
    prompt = f"""Посмотри на фото и определи что это. Ответь ОДНИМ словом:
- invoice — если это НАКЛАДНАЯ от поставщика (документ поставки, обычно на листе А4, с реквизитами компании-поставщика, печатями)
- receipt — если это КАССОВЫЙ ЧЕК продаж (узкая бумажная лента из кассы или терминала, список проданных позиций)
- unknown — если непонятно

Подпись: "{caption}"
Ответь только одним словом: invoice, receipt или unknown."""
    result = claude_vision(prompt, img_b64, max_tokens=20).lower().strip()
    if "invoice" in result: return "invoice"
    if "receipt" in result: return "receipt"
    return "unknown"

# ═════════════════════════════════════════════════════════════════════════════
# БАР — НАКЛАДНЫЕ
# ═════════════════════════════════════════════════════════════════════════════

def parse_invoice_image(img_b64: str, caption: str, sender_name: str) -> list:
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = f"""Разбери фото накладной от поставщика для бара. Сегодня: {today}. Принял: {sender_name}.

ВАЖНО: найди название поставщика — оно обычно в шапке документа (название компании, ИП, ООО и т.п.).

Верни ТОЛЬКО JSON-массив без markdown:
[{{"date":"ДД.ММ.ГГГГ","supplier":"название поставщика","product":"товар","qty":число,"unit":"ед","price":число,"total":число,"category":"категория"}}]

Категории: алкоголь, безалкогольные напитки, еда/закуски, расходники, другое
Все числа без знаков валют. Дату бери из накладной если есть, иначе сегодняшнюю.
Если поставщика не видно — ставь "неизвестный".
Контекст: {caption}"""
    data = parse_json(claude_vision(prompt, img_b64))
    return data if isinstance(data, list) else []

def parse_invoice_text(text: str, sender_name: str) -> list:
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = f"""Разбери текстовую накладную для бара. Сегодня: {today}. Принял: {sender_name}.
Верни ТОЛЬКО JSON-массив:
[{{"date":"ДД.ММ.ГГГГ","supplier":"поставщик","product":"товар","qty":число,"unit":"ед","price":число,"total":число,"category":"категория"}}]
Категории: алкоголь, безалкогольные напитки, еда/закуски, расходники, другое
Если данных нет — верни []. Текст: "{text}" """
    data = parse_json(claude_text(prompt, 2000))
    return data if isinstance(data, list) else []

# ═════════════════════════════════════════════════════════════════════════════
# БАР — СМЕНЫ
# ═════════════════════════════════════════════════════════════════════════════

def parse_shift_report(text: str, sender_name: str) -> dict:
    today = datetime.now().strftime("%d.%m.%Y")
    prompt = f"""Разбери отчёт о смене бара. Сегодня: {today}. Закрыл смену: {sender_name}.
Верни ТОЛЬКО JSON без markdown:
{{"date":"ДД.ММ.ГГГГ","closed_by":"имя","total":число,"bar":число,"services":число,"acquiring":число,"terminal":число,"cash":число,"notes":"заметки"}}

Все суммы — целые числа без знаков и пробелов (33460, не "33.460₽").
Если в тексте указано имя — используй его, иначе: {sender_name}.
Текст: "{text}" """
    data = parse_json(claude_text(prompt, 800))
    if not isinstance(data, dict): return {}
    for key in ("total", "bar", "services", "acquiring", "terminal", "cash"):
        if key in data: data[key] = to_number(data[key])
    return data

# ═════════════════════════════════════════════════════════════════════════════
# БАР — ПРОДАЖИ (чеки)
# ═════════════════════════════════════════════════════════════════════════════

def parse_sales_receipt(img_b64: str, sender_name: str) -> dict:
    prompt = f"""Посмотри на фото чека продаж из бара. Прислал: {sender_name}.

1. Найди дату кассового дня на чеке (обычно вверху: "кассовый день", "дата" и т.п.)
2. Извлеки все позиции

Верни ТОЛЬКО JSON без markdown:
{{"date":"ДД.ММ.ГГГГ","items":[{{"product":"название","qty":число,"price":число,"total":число,"category":"категория"}}]}}

Категории:
- "чаши" — если позиция содержит: чаша, чаши, продление чаш, продление чаши, кальян
- "бар" — все остальные (напитки, еда, закуски и т.д.)

Все числа без знаков валют. Если дату не видно — используй сегодняшнюю. Если не чек — верни {{"date":"","items":[]}}."""
    raw = claude_vision(prompt, img_b64, 2000)
    data = parse_json(raw)
    if not isinstance(data, dict):
        return {"date": "", "items": []}
    return data

def answer_admin_question(question: str) -> str:
    shifts = read_from_sheet(BAR_SCRIPT_URL, "Смены")
    invoices = read_from_sheet(BAR_SCRIPT_URL, "Накладные")
    sales = read_from_sheet(BAR_SCRIPT_URL, "Продажи")
    s_text = "\n".join(["\t".join(str(c) for c in r) for r in shifts[-60:]])
    i_text = "\n".join(["\t".join(str(c) for c in r) for r in invoices[-120:]])
    p_text = "\n".join(["\t".join(str(c) for c in r) for r in sales[-120:]])
    prompt = f"""Ты финансовый помощник бара. Все суммы в рублях.

СМЕНЫ:
{s_text}

НАКЛАДНЫЕ:
{i_text}

ПРОДАЖИ:
{p_text}

Ответь кратко на русском.
Вопрос: {question}"""
    return claude_text(prompt, 900)

# ═════════════════════════════════════════════════════════════════════════════
# Классификация намерения
# ═════════════════════════════════════════════════════════════════════════════

def classify_intent(text: str) -> str:
    lower = text.lower()
    if any(w in lower for w in ["накладная", "накладн", "поставк", "поставщик", "привезли", "приход товар"]):
        return "invoice"
    if any(w in lower for w in ["чек", "продажи", "позиции"]):
        return "receipt"
    if any(w in lower for w in ["смена", "выручка", "отчёт", "отчет", "результат смен", "закрыл смену"]):
        return "shift"
    if any(w in lower for w in ["сколько", "баланс", "итого", "кто должен", "статистик",
                                 "покажи", "список", "сводка", "продаж", "?"]):
        return "question"
    if re.search(r"\d+[.,]?\d*\s*(₽|руб|рубл|р\b|€|евро|usd|\$)?", lower):
        return "expense"
    return "unknown"

# ═════════════════════════════════════════════════════════════════════════════
# Маршрутизация
# ═════════════════════════════════════════════════════════════════════════════

def route_chat(chat_id: int) -> str:
    if chat_id == ADMIN_CHAT_ID: return "bar"
    if chat_id == COUPLE_CHAT_ID: return "couple"
    return "unknown"

def should_respond_in_group(msg, text: str) -> bool:
    mentioned = BOT_USERNAME and (f"@{BOT_USERNAME}" in text.lower())
    reply = bool(msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot)
    return mentioned or reply

def strip_mention(text: str) -> str:
    if not BOT_USERNAME: return text.strip()
    return re.sub(f"@{re.escape(BOT_USERNAME)}", "", text, flags=re.IGNORECASE).strip()

# ═════════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК БАРА
# ═════════════════════════════════════════════════════════════════════════════

async def handle_bar(msg, text: str, sender_name: str, context):
    added_ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    has_photo = bool(msg.photo)
    intent = classify_intent(text) if text else "unknown"

    # ── ТЕКСТ + ФОТО: смена — обрабатываем только текст, фото игнорируем ─
    if has_photo and text and intent == "shift":
        await msg.reply_text("📊 Разбираю отчёт смены...")
        data = parse_shift_report(text, sender_name)
        if data and data.get("total"):
            closed_by = data.get("closed_by") or sender_name
            write_to_sheet(BAR_SCRIPT_URL, "Смены", [[
                data.get("date",""), closed_by, data.get("total",""), data.get("bar",""),
                data.get("services",""), data.get("acquiring",""), data.get("terminal",""),
                data.get("cash",""), data.get("notes",""), added_ts]])
            await msg.reply_text(
                f"✅ Смена {data.get('date','')} записана\n"
                f"Закрыл: {closed_by}\n"
                f"Выручка: {data.get('total','')}₽\n"
                f"• Бар: {data.get('bar','')}₽ | Услуги: {data.get('services','')}₽\n"
                f"• Эквайринг: {data.get('acquiring','')}₽ | Терминал: {data.get('terminal','')}₽ | Нал: {data.get('cash','')}₽\n"
                f"\n📎 Чек с позициями пришли отдельным фото — разберу и занесу в продажи.")
        else:
            await msg.reply_text("⚠️ Не удалось разобрать текст отчёта смены.")
        return

        file = await context.bot.get_file(msg.photo[-1].file_id)
        img_bytes = bytes(await file.download_as_bytearray())
        img_b64 = image_to_base64(img_bytes)
        result = parse_sales_receipt(img_b64, sender_name)
                receipt_date = result.get("date") or datetime.now().strftime("%d.%m.%Y")
                items = result.get("items", [])
                if items:
            rows = [[receipt_date, sender_name, i.get("product",""),
                     i.get("product",""), to_number(i.get("qty","")), to_number(i.get("price","")),
                     to_number(i.get("total","")), i.get("category","бар"), added_ts] for i in items]
            write_to_sheet(BAR_SCRIPT_URL, "Продажи", rows)
            lines = [f"• {i.get('product','')} × {i.get('qty','')} = {i.get('total','')}₽" for i in items]
            await msg.reply_text(f"🧾 Чек продаж записан:\n" + "\n".join(lines))
        else:
            await msg.reply_text("⚠️ Не удалось распознать чек с фото.")
        return

    # ── ТОЛЬКО ФОТО (без текста или текст не про смену) ───────────────────
    if has_photo:
        file = await context.bot.get_file(msg.photo[-1].file_id)
        img_bytes = bytes(await file.download_as_bytearray())
        img_b64 = image_to_base64(img_bytes)
        
        if intent == "receipt":
            await msg.reply_text("🧾 Разбираю чек продаж...")
            items = parse_sales_receipt(img_b64, sender_name)
            if not items:
                await msg.reply_text("Не удалось распознать чек.")
                return
            rows = [[added_ts, sender_name, i.get("product",""), to_number(i.get("qty","")),
                     to_number(i.get("price","")), to_number(i.get("total","")), i.get("category","бар"), added_ts] for i in items]
            write_to_sheet(BAR_SCRIPT_URL, "Продажи", rows)
            lines = [f"• {i.get('product','')} × {i.get('qty','')} = {i.get('total','')}₽" for i in items]
            await msg.reply_text(f"✅ Чек продаж от {sender_name}:\n" + "\n".join(lines))
            return
            
        if intent == "invoice":
            await msg.reply_text("📋 Разбираю накладную...")
            items = parse_invoice_image(img_b64, text, sender_name)
            if not items:
                await msg.reply_text("Не удалось распознать накладную. Пришли фото чётче.")
                return
            rows = [[i.get("date",""), i.get("supplier","неизвестный"), i.get("product",""),
                     to_number(i.get("qty","")), i.get("unit",""), to_number(i.get("price","")),
                     to_number(i.get("total","")), i.get("category","другое"), sender_name, added_ts]
                    for i in items]
            write_to_sheet(BAR_SCRIPT_URL, "Накладные", rows)
            supplier = items[0].get("supplier", "неизвестный") if items else "?"
            lines = [f"• {i.get('product','')} — {i.get('qty','')} {i.get('unit','')} × {i.get('price','')}₽ = {i.get('total','')}₽" for i in items]
            await msg.reply_text(f"✅ Накладная от {supplier} ({sender_name}):\n" + "\n".join(lines))
            return

        await msg.reply_text("🔍 Определяю тип документа...")
        photo_type = classify_photo(img_b64, text)
        logger.info(f"photo classified as: {photo_type}")

        if photo_type == "invoice":
            items = parse_invoice_image(img_b64, text, sender_name)
            if not items:
                await msg.reply_text("Не удалось распознать накладную.")
                return
            rows = [[i.get("date",""), i.get("supplier","неизвестный"), i.get("product",""),
                     to_number(i.get("qty","")), i.get("unit",""), to_number(i.get("price","")),
                     to_number(i.get("total","")), i.get("category","другое"), sender_name, added_ts]
                    for i in items]
            write_to_sheet(BAR_SCRIPT_URL, "Накладные", rows)
            supplier = items[0].get("supplier", "неизвестный") if items else "?"
            lines = [f"• {i.get('product','')} — {i.get('qty','')} {i.get('unit','')} × {i.get('price','')}₽" for i in items]
            await msg.reply_text(f"✅ Накладная от {supplier} ({sender_name}):\n" + "\n".join(lines))

        elif photo_type == "receipt":
            items = parse_sales_receipt(img_b64, sender_name)
            if not items:
                await msg.reply_text("Не удалось распознать чек.")
                return
            rows = [[added_ts, sender_name, i.get("product",""), to_number(i.get("qty","")),
                     to_number(i.get("price","")), to_number(i.get("total","")), i.get("category","бар"), added_ts] for i in items]
            write_to_sheet(BAR_SCRIPT_URL, "Продажи", rows)
            lines = [f"• {i.get('product','')} × {i.get('qty','')} = {i.get('total','')}₽" for i in items]
            await msg.reply_text(f"✅ Чек продаж от {sender_name}:\n" + "\n".join(lines))

        else:
            await msg.reply_text("Не понял что на фото. Подпиши «накладная» или «чек» чтобы я понял.")
        return

    # ── ТОЛЬКО ТЕКСТ ──────────────────────────────────────────────────────
    if not text:
        return

    if intent == "shift":
        await msg.reply_text("📊 Разбираю отчёт смены...")
        data = parse_shift_report(text, sender_name)
        if not data or not data.get("total"):
            await msg.reply_text("Не удалось разобрать. Пришли в формате:\n"
                "«28.05 Выручка: 33460 — бар: 8190 — услуги: 25270\n"
                "эквайринг: 0 — терминал: 25700 — наличные: 7760»")
            return
        closed_by = data.get("closed_by") or sender_name
        write_to_sheet(BAR_SCRIPT_URL, "Смены", [[
            data.get("date",""), closed_by, data.get("total",""), data.get("bar",""),
            data.get("services",""), data.get("acquiring",""), data.get("terminal",""),
            data.get("cash",""), data.get("notes",""), added_ts]])
        await msg.reply_text(
            f"✅ Смена {data.get('date','')} записана\n"
            f"Закрыл: {closed_by}\n"
            f"Выручка: {data.get('total','')}₽\n"
            f"• Бар: {data.get('bar','')}₽ | Услуги: {data.get('services','')}₽\n"
            f"• Эквайринг: {data.get('acquiring','')}₽ | Терминал: {data.get('terminal','')}₽ | Нал: {data.get('cash','')}₽")

    elif intent == "invoice":
        await msg.reply_text("📋 Разбираю накладную...")
        items = parse_invoice_text(text, sender_name)
        if not items:
            await msg.reply_text("Не нашёл позиций в накладной.")
            return
        rows = [[i.get("date",""), i.get("supplier",""), i.get("product",""),
                 to_number(i.get("qty","")), i.get("unit",""), to_number(i.get("price","")),
                 to_number(i.get("total","")), i.get("category",""), sender_name, added_ts]
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
            "• Фото накладной → занесу поставку\n"
            "• Фото чека продаж → занесу в продажи\n"
            "• Текст отчёта смены → занесу выручку\n"
            "• Текст + фото чека → смена + продажи\n"
            "• «Выручка за май?» → отвечу по данным")

# ═════════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК ПАРЫ
# ═════════════════════════════════════════════════════════════════════════════

async def handle_couple(msg, text: str, sender_name: str, context):
    added_ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    if not text:
        return

    intent = classify_intent(text)

    if intent == "question":
        await msg.reply_text("🔍 Смотрю таблицу...")
        await msg.reply_text(answer_finance_question(text))
        return

    ops = parse_finance(text, sender_name)
    if ops:
        rows = [[e.get("date",""), e.get("who") or sender_name, e.get("operation","расход"),
                 to_number(e.get("amount","")), e.get("type","прочее"), e.get("desc",""), added_ts]
                for e in ops]
        write_to_sheet(COUPLE_SCRIPT_URL, "Финансы", rows)
        lines = [f"• {e.get('operation','расход').upper()} | {e.get('who') or sender_name}: "
                 f"{e.get('amount','')}₽ — {e.get('desc','')} ({e.get('type','')})" for e in ops]
        await msg.reply_text("✅ Записал:\n" + "\n".join(lines))
    else:
        await msg.reply_text(
            "Я трекер финансов 💰\n\n"
            "Расходы: «заплатил 450₽ за уборку»\n"
            "Доходы: «получил 50000₽ зарплата»\n"
            "Вопросы: «Какой баланс?», «Расходы за май?»")

# ═════════════════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ ОБРАБОТЧИК
# ═════════════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return

    chat_id = msg.chat_id
    chat_type = msg.chat.type
    text = (msg.text or msg.caption or "").strip()
    sender_name = get_sender_name(msg)
    route = route_chat(chat_id)

    logger.info(f"msg chat_id={chat_id} type={chat_type} route={route} photo={bool(msg.photo)} text={text[:60]!r}")

    if route == "unknown":
        return

    if chat_type in ("group", "supergroup"):
        if not should_respond_in_group(msg, text):
            return
        text = strip_mention(text)

# ── Reply на старое сообщение: берём данные из оригинала ──────────────
        replied = msg.reply_to_message
        if replied and replied.from_user and not replied.from_user.is_bot:
            original_text = (replied.text or replied.caption or "").strip()
            original_photo = replied.photo if replied.photo else None

            clean = text.lower().strip()
            trigger_words = ["забери", "запиши", "занеси", "разбери", "обработай", "чек", "накладная", "смена"]
            is_just_trigger = clean == "" or any(w in clean for w in trigger_words)

        if is_just_trigger and (original_text or original_photo):
                logger.info(f"Reply mode: берём данные из оригинального сообщения")
                text = original_text
                sender_name = get_sender_name(replied)
                if original_photo:
                    msg = replied

    try:
        if route == "bar":
            await handle_bar(msg, text, sender_name, context)
        elif route == "couple":
            await handle_couple(msg, text, sender_name, context)
    except Exception as e:
        logger.error(f"handle error: {e}", exc_info=True)
        await update.message.reply_text("❌ Ошибка, попробуй ещё раз.")

# ═════════════════════════════════════════════════════════════════════════════
# КОМАНДЫ
# ═════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    route = route_chat(update.message.chat_id)
    if route == "bar":
        await update.message.reply_text(
            "Привет! Я бот учёта бара 🍸\n\n"
            "• Фото накладной → запишу поставку\n"
            "• Фото чека продаж → запишу в продажи\n"
            "• Текст отчёта смены → запишу выручку\n"
            "• Текст + фото чека → смена + продажи\n"
            "• «Выручка за май?» → отвечу по данным")
    elif route == "couple":
        await update.message.reply_text(
            "Привет! Я трекер финансов 💰\n\n"
            "• «заплатил 450₽ за уборку» → расход\n"
            "• «получил 50000₽ зарплата» → доход\n"
            "• «Какой баланс?» → посчитаю")
    else:
        await update.message.reply_text(
            f"Этот чат не подключён.\nID: `{update.message.chat_id}`", parse_mode="Markdown")

async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.message.chat_id
    await update.message.reply_text(f"ID: `{cid}`\nРаспознан: *{route_chat(cid)}*", parse_mode="Markdown")

# ═════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", get_chat_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_message))
    logger.info("Bot started")
    app.run_polling()
