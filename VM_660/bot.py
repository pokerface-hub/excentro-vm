#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ExCentro Assistant — клиентский AI-ассистент первой линии (Telegram + Claude).
Отвечает на вопросы заказчиков о редукторах ВМ, квалифицирует лида,
эскалирует сложные/коммерческие запросы инженерам в группу.

aiogram 3.28 + anthropic SDK.
"""

import asyncio
import json
import logging
import os
import re
from collections import defaultdict, deque
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiogram.enums import ChatType

import anthropic

import config
import leads

# ── Логирование ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("excentro-assistant")

# ── Инициализация ─────────────────────────────────────────────────────────────
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()
# Клиент Claude. Если задан HTTPS_PROXY в config — ходим через прокси
# (нужно, когда сервер в регионе, заблокированном для api.anthropic.com).
if getattr(config, "ANTHROPIC_PROXY", "").strip():
    import httpx
    claude = anthropic.Anthropic(
        api_key=config.ANTHROPIC_API_KEY,
        http_client=httpx.Client(proxy=config.ANTHROPIC_PROXY, timeout=60.0),
    )
    log.info(f"Claude через прокси: {config.ANTHROPIC_PROXY.split('@')[-1]}")
else:
    claude = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# ── Загрузка промпта и базы знаний ────────────────────────────────────────────
def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

SYSTEM_PROMPT = load_text(config.SYSTEM_PROMPT_FILE)
KNOWLEDGE_BASE = load_text(config.KNOWLEDGE_BASE_FILE)

# Подставляем ссылку на калькулятор
SYSTEM_PROMPT = SYSTEM_PROMPT.replace("{CALCULATOR_URL}", config.CALCULATOR_URL)
KNOWLEDGE_BASE = KNOWLEDGE_BASE.replace("{CALCULATOR_URL}", config.CALCULATOR_URL)

# Итоговый system для Claude = промпт + база знаний
FULL_SYSTEM = (
    SYSTEM_PROMPT
    + "\n\n=== БАЗА ЗНАНИЙ (отвечай только на её основе) ===\n"
    + KNOWLEDGE_BASE
    + "\n" + leads.EXTRACTION_INSTRUCTION
)

# ── Память диалогов (в ОЗУ) ───────────────────────────────────────────────────
# user_id -> deque[{"role": "...", "content": "..."}]
history = defaultdict(lambda: deque(maxlen=config.HISTORY_LIMIT))

# ── Состояние «AI на паузе» (handoff инженеру) ────────────────────────────────
# Сохраняется в файл, чтобы переживать рестарт сервиса.
def load_paused() -> set:
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_paused(paused: set):
    try:
        with open(config.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(list(paused), f)
    except Exception as e:
        log.error(f"Не удалось сохранить состояние пауз: {e}")

paused_chats = load_paused()  # set из user_id, где AI молчит (ведёт инженер)

# Хранилище имён/контактов клиентов для удобства инженеров: user_id -> инфо
client_info = {}

# ── Триггеры эскалации (запасной детектор поверх решения модели) ───────────────
ESCALATION_PATTERNS = re.compile(
    r"(цен[аеуы]|стоимост|сколько стоит|прайс|ТКП|коммерческ|"
    r"скидк|договор|контракт|услови[яй] поставк|"
    r"NDA|переговор|инженер|менеджер|человек|связат|позвонит|"
    r"price|cost|quote|commercial|contract|negotiat|engineer|human)",
    re.IGNORECASE,
)

# Метка, которую модель ставит в ответ, если решила эскалировать
ESCALATION_TAG = "[ЭСКАЛАЦИЯ]"


# ══════════════════════════════════════════════════════════════════════════════
# ВЫЗОВ CLAUDE
# ══════════════════════════════════════════════════════════════════════════════
async def ask_claude(user_id: int, user_text: str) -> str:
    """Отправляет историю + новое сообщение в Claude, возвращает ответ."""
    msgs = list(history[user_id])
    msgs.append({"role": "user", "content": user_text})

    # anthropic SDK синхронный — выполняем в отдельном потоке, чтобы не блокировать loop
    def _call():
        return claude.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.MAX_TOKENS,
            temperature=config.TEMPERATURE,
            system=FULL_SYSTEM,
            messages=msgs,
        )

    try:
        resp = await asyncio.to_thread(_call)
        raw = "".join(block.text for block in resp.content if block.type == "text")
        # вырезаем служебный LEAD-блок, обновляем карточку лида
        clean, extracted = leads.split_answer_and_lead(raw)
        if extracted:
            leads.update_card(user_id, extracted)
        # в историю кладём текст БЕЗ служебного блока
        history[user_id].append({"role": "user", "content": user_text})
        history[user_id].append({"role": "assistant", "content": clean})
        return clean.strip()
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return ("Извините, временная техническая заминка на нашей стороне. "
                "Попробуйте повторить вопрос через минуту — или напишите на info@rusmashgroup.ru.")


# ══════════════════════════════════════════════════════════════════════════════
# ЭСКАЛАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════
async def escalate(user: types.User, user_text: str, assistant_text: str):
    """Шлёт резюме в группу инженеров (с карточкой лида) и ставит чат на паузу."""
    paused_chats.add(user.id)
    save_paused(paused_chats)

    uname = f"@{user.username}" if user.username else "(без username)"
    fullname = user.full_name or "—"
    client_info[user.id] = {"name": fullname, "username": uname}

    # дозаполним в карточке имя в Telegram и принудительно запишем в журнал
    leads.cards[user.id]["tg_name"] = f"{fullname} {uname}".strip()
    await asyncio.to_thread(leads.push_to_sheets, user.id, True)  # force=True

    clean = assistant_text.replace(ESCALATION_TAG, "").strip()
    card = leads.card_summary(user.id)
    miss = leads.missing_required(user.id)
    miss_line = ("\n⚠️ Не собрано: " + ", ".join(leads.FIELD_LABELS.get(m, m) for m in miss)) if miss else "\n✅ Минимум собран"

    summary = (
        f"🔔 *ЭСКАЛАЦИЯ — ExCentro Assistant*\n\n"
        f"👤 Клиент: {fullname} {uname}\n"
        f"🆔 user\\_id: `{user.id}`\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"📋 *Карточка лида:*\n{card}{miss_line}\n\n"
        f"💬 *Запрос:* {user_text[:400]}\n\n"
        f"🤖 *Ответ ассистента:*\n{clean[:600]}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Лид записан в журнал. Чат на *паузе* — ассистент молчит.\n"
        f"Ответить клиенту: `/say {user.id} текст`\n"
        f"Вернуть ассистенту: `/release {user.id}`"
    )
    try:
        await bot.send_message(config.ESCALATION_CHAT_ID, summary, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Не удалось отправить эскалацию в группу: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ХЭНДЛЕРЫ — ЛИЧНЫЕ СООБЩЕНИЯ КЛИЕНТОВ
# ══════════════════════════════════════════════════════════════════════════════
# Приветствия по языку deep-link (?start=ru / en / es из кнопки презентации)
GREETINGS = {
    "ru": (
        "Здравствуйте! Я ExCentro Assistant — помогу с вопросами по нашим "
        "редукторам на эксцентрико-циклоидальном зацеплении, в том числе по "
        "флагманскому модулю ВМ-660-41.\n\n"
        "Расскажите, какая у вас задача: отрасль, требуемый момент и скорость — "
        "и я подскажу решение. Если нужно подобрать редуктор под параметры, "
        "у нас есть калькулятор: {url}\n\n"
        "Чем могу помочь?"
    ),
    "en": (
        "Hello! I'm the ExCentro Assistant — here to help with questions about our "
        "gearboxes based on eccentric-cycloidal gearing, including our flagship "
        "VM-660-41 module.\n\n"
        "Tell me about your task: industry, required torque and input speed — "
        "and I'll suggest a solution. If you'd like to size a gearbox to your "
        "parameters, we have a calculator: {url}\n\n"
        "How can I help?"
    ),
    "es": (
        "¡Hola! Soy el Asistente ExCentro — le ayudo con consultas sobre nuestros "
        "reductores basados en engranaje excéntrico-cicloidal, incluido nuestro "
        "módulo insignia VM-660-41.\n\n"
        "Cuénteme su tarea: sector, par requerido y velocidad de entrada — "
        "y le propondré una solución. Si desea dimensionar un reductor según sus "
        "parámetros, tenemos una calculadora: {url}\n\n"
        "¿En qué puedo ayudarle?"
    ),
}

@dp.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, command: CommandObject):
    uid = message.from_user.id
    history.pop(uid, None)
    paused_chats.discard(uid)
    save_paused(paused_chats)
    # сбрасываем карточку лида на новый старт
    leads.cards.pop(uid, None)

    # deep-link параметр: [<type>_]<lang>
    #   языки: ru|en|es ; типы: req (техзапрос), nda (NDA/переговоры), calc (опросный)
    arg = (command.args or "").strip().lower()
    ltype, lang = "", ""
    if arg:
        parts = arg.split("_")
        for p in parts:
            if p in ("ru", "en", "es"):
                lang = p
            elif p in ("req", "nda", "calc", "tech"):
                ltype = p
    if not lang:
        tg_lang = (message.from_user.language_code or "ru")[:2]
        lang = tg_lang if tg_lang in GREETINGS else "ru"

    # засеваем карточку: язык и тип обращения
    type_map = {"req": "техзапрос", "tech": "техзапрос", "nda": "NDA/переговоры",
                "calc": "опросный", "": "общий"}
    leads.cards[uid]["lang"] = lang
    leads.cards[uid]["type"] = type_map.get(ltype, "общий")

    greeting = GREETINGS[lang].format(url=config.CALCULATOR_URL)
    # если пришёл с конкретной кнопки — добавим контекстную подводку
    ctx = CONTEXT_INTRO.get((ltype, lang)) or CONTEXT_INTRO.get((ltype, "ru"))
    if ctx:
        greeting = ctx + "\n\n" + greeting
    await message.answer(greeting)


# Контекстные подводки по типу кнопки (techзапрос / NDA)
CONTEXT_INTRO = {
    ("req", "ru"): "Вы оставили технический запрос по ВМ-660-41.",
    ("req", "en"): "You've started a technical inquiry about the VM-660-41.",
    ("req", "es"): "Ha iniciado una consulta técnica sobre el VM-660-41.",
    ("nda", "ru"): "Вы заинтересованы в сотрудничестве и обсуждении NDA — отлично, я соберу базовую информацию и передам нашему инженеру.",
    ("nda", "en"): "You're interested in cooperation and an NDA — I'll gather a few details and pass them to our engineer.",
    ("nda", "es"): "Está interesado en cooperación y un NDA — recopilaré algunos datos y los pasaré a nuestro ingeniero.",
}


@dp.message(F.chat.type == ChatType.PRIVATE)
async def handle_client(message: Message):
    user = message.from_user
    user_text = (message.text or "").strip()
    if not user_text:
        return

    # Если чат на паузе — AI молчит, ведёт инженер
    if user.id in paused_chats:
        # тихо пересылаем реплику клиента в группу, чтобы инженер видел
        try:
            await bot.send_message(
                config.ESCALATION_CHAT_ID,
                f"✉️ *Клиент* `{user.id}` ({user.full_name}):\n{user_text}\n\n"
                f"Ответить: `/say {user.id} ...`  •  Вернуть AI: `/release {user.id}`",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error(f"forward to group failed: {e}")
        return

    await bot.send_chat_action(message.chat.id, "typing")
    answer = await ask_claude(user.id, user_text)

    # держим имя в Telegram актуальным в карточке
    uname = f"@{user.username}" if user.username else ""
    leads.cards[user.id]["tg_name"] = f"{user.full_name or ''} {uname}".strip()

    # Решение об эскалации: тег модели ИЛИ запасной детектор по ключевым словам
    need_escalation = (ESCALATION_TAG in answer) or bool(ESCALATION_PATTERNS.search(user_text))

    # показываем клиенту ответ (без служебного тега)
    shown = answer.replace(ESCALATION_TAG, "").strip()
    await message.answer(shown[:4096])

    if need_escalation:
        await escalate(user, user_text, answer)
        await message.answer(
            "Я передал ваш запрос нашему инженеру — он свяжется с вами. "
            "Чтобы ускорить, можете сразу указать компанию и удобный контакт."
        )
    else:
        # пишем/обновляем лид в журнале по ходу диалога (с троттлингом внутри)
        await asyncio.to_thread(leads.push_to_sheets, user.id, False)


# ══════════════════════════════════════════════════════════════════════════════
# ХЭНДЛЕРЫ — ГРУППА ЭСКАЛАЦИИ (команды инженеров)
# ══════════════════════════════════════════════════════════════════════════════
@dp.message(Command("release"))
async def cmd_release(message: Message):
    """/release <user_id> — вернуть чат клиента ассистенту."""
    if message.chat.id != config.ESCALATION_CHAT_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.reply("Формат: `/release <user_id>`", parse_mode="Markdown")
        return
    uid = int(parts[1])
    paused_chats.discard(uid)
    save_paused(paused_chats)
    await message.reply(f"✅ Чат клиента `{uid}` возвращён ассистенту.", parse_mode="Markdown")
    try:
        await bot.send_message(uid, "С вами снова на связи ExCentro Assistant. Чем могу помочь?")
    except Exception:
        pass


@dp.message(Command("say"))
async def cmd_say(message: Message):
    """/say <user_id> <текст> — инженер пишет клиенту от имени компании."""
    if message.chat.id != config.ESCALATION_CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].lstrip("-").isdigit():
        await message.reply("Формат: `/say <user_id> текст сообщения`", parse_mode="Markdown")
        return
    uid = int(parts[1])
    text = parts[2]
    # гарантируем, что чат на паузе, раз инженер пишет вручную
    paused_chats.add(uid)
    save_paused(paused_chats)
    try:
        await bot.send_message(uid, text)
        await message.reply(f"✅ Отправлено клиенту `{uid}`.", parse_mode="Markdown")
    except Exception as e:
        await message.reply(f"❌ Не удалось отправить: {e}")


@dp.message(Command("pending"))
async def cmd_pending(message: Message):
    """/pending — список чатов на паузе (кого ведут инженеры)."""
    if message.chat.id != config.ESCALATION_CHAT_ID:
        return
    if not paused_chats:
        await message.reply("Нет чатов на паузе — все ведёт ассистент.")
        return
    lines = []
    for uid in paused_chats:
        info = client_info.get(uid, {})
        lines.append(f"• `{uid}` {info.get('name','')} {info.get('username','')}")
    await message.reply("⏸ *На паузе (ведёт инженер):*\n" + "\n".join(lines), parse_mode="Markdown")


@dp.message(Command("chatid"))
async def cmd_chatid(message: Message):
    """/chatid — показать ID текущего чата (утилита для настройки)."""
    await message.reply(f"chat_id: `{message.chat.id}`", parse_mode="Markdown")


@dp.message(Command("lead"))
async def cmd_lead(message: Message):
    """/lead <user_id> — показать карточку лида (для инженеров в группе)."""
    if message.chat.id != config.ESCALATION_CHAT_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.reply("Формат: `/lead <user_id>`", parse_mode="Markdown")
        return
    uid = int(parts[1])
    if uid not in leads.cards:
        await message.reply(f"По `{uid}` карточки нет.", parse_mode="Markdown")
        return
    await message.reply(
        f"📋 *Карточка лида* `{uid}`:\n{leads.card_summary(uid)}",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════════════════════════════════════
async def main():
    log.info("ExCentro Assistant запускается...")
    log.info(f"Модель: {config.CLAUDE_MODEL}")
    log.info(f"Группа эскалации: {config.ESCALATION_CHAT_ID}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
