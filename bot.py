#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ExCentro Assistant — клиентский AI-ассистент первой линии (Telegram + Claude).
Отвечает на вопросы заказчиков о редукторах ВМ, квалифицирует лида,
эскалирует сложные/коммерческие запросы инженерам в группу.

aiogram 3.28 + anthropic SDK.

=== ВЕТРЯНОЙ ТРЕК ===
Deep-links: ?start=wind_ru | rosatom | nda_wind
Параметры config (необязательные): WIND_TEASER_FILE, NDA_FILE

=== ЖУРНАЛ ЛИДОВ (Google Sheets) ===
Бот автоматически пишет каждый лид в Google-таблицу (вкладка «Лиды»):
дата, время, тип, трек, источник, имя, username, user_id, сообщение, статус.
Если Google недоступен — лид сохраняется в резервный CSV (LEADS_FALLBACK_CSV)
и бот продолжает работать. Команда /leadtest в группе инженеров пишет
тестовую строку для проверки подключения.

Новые параметры в config.py:
  GSHEET_ID        = "1_pepk2_M53rTr5JEg0jZlb0cSf8AWComIvovu0iuLXg"
  GSHEET_WORKSHEET = "Лиды"
  GSHEET_KEY_FILE  = "gsheets-key.json"   # JSON-ключ сервисного аккаунта
Зависимости: pip install gspread google-auth
Если параметры не заданы или библиотеки не установлены — журнал просто
отключён, всё остальное работает как раньше.
"""

import asyncio
import csv
import json
import logging
import os
import re
from collections import defaultdict, deque
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, FSInputFile
from aiogram.enums import ChatType

import anthropic

import config

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
)

# ── Память диалогов (в ОЗУ) ───────────────────────────────────────────────────
history = defaultdict(lambda: deque(maxlen=config.HISTORY_LIMIT))

# Трек клиента для журнала: user_id -> "ВЭУ" | "Общепром"
user_track = {}

# ── Состояние «AI на паузе» (handoff инженеру) ────────────────────────────────
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

paused_chats = load_paused()
client_info = {}

# ── Триггеры эскалации (запасной детектор поверх решения модели) ───────────────
ESCALATION_PATTERNS = re.compile(
    r"(цен[аеуы]|стоимост|сколько стоит|прайс|ТКП|коммерческ|"
    r"скидк|договор|контракт|услови[яй] поставк|"
    r"NDA|переговор|инженер|менеджер|человек|связат|позвонит|"
    r"price|cost|quote|commercial|contract|negotiat|engineer|human)",
    re.IGNORECASE,
)
ESCALATION_TAG = "[ЭСКАЛАЦИЯ]"


# ══════════════════════════════════════════════════════════════════════════════
# ЖУРНАЛ ЛИДОВ — Google Sheets + резервный CSV
# ══════════════════════════════════════════════════════════════════════════════
GSHEET_ID = getattr(config, "GSHEET_ID", "").strip()
GSHEET_WORKSHEET = getattr(config, "GSHEET_WORKSHEET", "Лиды").strip() or "Лиды"
GSHEET_KEY_FILE = getattr(config, "GSHEET_KEY_FILE", "gsheets-key.json").strip()
LEADS_FALLBACK_CSV = getattr(config, "LEADS_FALLBACK_CSV", "leads_fallback.csv")

LEAD_HEADER = ["Дата", "Время", "Тип", "Трек", "Источник",
               "Имя", "Username", "user_id", "Сообщение", "Статус"]

_ws = None            # кэш worksheet
_sheets_ok = None     # None = не пробовали; True/False = результат подключения

def _classify_gs_error(e):
    """Классифицирует ошибку журнала → (КОД, человекочитаемая причина + что делать)."""
    s = str(e)
    name = type(e).__name__
    if isinstance(e, FileNotFoundError):
        return "KEY_MISSING", f"JSON-ключ не найден по пути {GSHEET_KEY_FILE}"
    if name == "JSONDecodeError" or "Expecting value" in s:
        return "KEY_BROKEN", ("JSON-ключ повреждён — файл не читается как JSON. "
                              "Перевставьте содержимое целиком, от {{ до }}")
    if "invalid_grant" in s or "RefreshError" in name:
        return "KEY_REJECTED", ("Google отклонил ключ (invalid_grant): ключ отозван "
                                "или сбито время на сервере — проверьте timedatectl")
    if "PERMISSION_DENIED" in s or "403" in s:
        return "NO_ACCESS", ("Нет доступа к таблице: выдайте права Редактора адресу "
                             "client_email из JSON-ключа (Настройки доступа в таблице)")
    if "404" in s or "NOT_FOUND" in s:
        return "NOT_FOUND", "Таблица не найдена: проверьте GSHEET_ID в config.py"
    if "429" in s or "RESOURCE_EXHAUSTED" in s or "Quota" in s:
        return "QUOTA", "Превышена квота Google Sheets API — повторите через минуту"
    if name == "ModuleNotFoundError":
        return "NO_LIBS", "Не установлены библиотеки: ./venv/bin/pip install gspread google-auth"
    low = s.lower()
    if any(k in name for k in ("Connection", "Timeout")) or any(
            k in low for k in ("timed out", "connection", "name resolution",
                               "temporary failure", "unreachable")):
        return "NETWORK", "Сетевая ошибка: сервер не смог достучаться до googleapis.com"
    return "UNKNOWN", f"{name}: {s[:200]}"

def _open_worksheet():
    """Ленивое подключение к Google Sheets (выполняется в отдельном потоке)."""
    global _ws
    if _ws is not None:
        return _ws
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(GSHEET_KEY_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    sh = client.open_by_key(GSHEET_ID)
    try:
        ws = sh.worksheet(GSHEET_WORKSHEET)
    except Exception:
        ws = sh.add_worksheet(title=GSHEET_WORKSHEET, rows=1000, cols=len(LEAD_HEADER))
    # Заголовок: если первая строка пуста — записываем шапку и закрепляем её
    first = ws.row_values(1)
    if not first:
        ws.append_row(LEAD_HEADER, value_input_option="USER_ENTERED")
        try:
            ws.format("A1:J1", {"textFormat": {"bold": True}})
            ws.freeze(rows=1)
        except Exception:
            pass
    _ws = ws
    return _ws

def _append_row_sync(row):
    ws = _open_worksheet()
    ws.append_row(row, value_input_option="USER_ENTERED")

def _fallback_csv(row):
    """Резерв: дописываем лид в локальный CSV, чтобы ничего не потерять."""
    try:
        new = not os.path.isfile(LEADS_FALLBACK_CSV)
        with open(LEADS_FALLBACK_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow(LEAD_HEADER)
            w.writerow(row)
    except Exception as e:
        log.error(f"Не удалось записать лид даже в CSV: {e}")

def _diagnose_sync(tester_name: str):
    """Пошаговая проверка полного цикла записи. Возвращает (ok, строки отчёта)."""
    lines = []
    ok = lambda t: lines.append("✅ " + t)
    fail = lambda t: lines.append("❌ " + t)

    # Шаг 1 — конфигурация
    if not GSHEET_ID:
        fail("GSHEET_ID не задан в config.py")
        return False, lines
    ok(f"Конфиг: таблица …{GSHEET_ID[-8:]}, вкладка «{GSHEET_WORKSHEET}»")

    # Шаг 2 — библиотеки
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except Exception as e:
        _, human = _classify_gs_error(e)
        fail(f"Библиотеки: {human}")
        return False, lines
    ok("Библиотеки gspread / google-auth установлены")

    # Шаг 3 — JSON-ключ
    try:
        with open(GSHEET_KEY_FILE, encoding="utf-8") as f:
            key = json.load(f)
        email = key.get("client_email", "")
        if not email or not key.get("private_key"):
            fail("Ключ читается, но в нём нет client_email/private_key — "
                 "похоже, скачан не тот файл (нужен ключ сервисного аккаунта)")
            return False, lines
    except Exception as e:
        _, human = _classify_gs_error(e)
        fail(f"JSON-ключ: {human}")
        return False, lines
    ok(f"Ключ прочитан, сервисный аккаунт: {email}")

    # Шаг 4 — авторизация в Google
    try:
        creds = Credentials.from_service_account_file(
            GSHEET_KEY_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        client = gspread.authorize(creds)
    except Exception as e:
        _, human = _classify_gs_error(e)
        fail(f"Авторизация в Google: {human}")
        return False, lines
    ok("Авторизация в Google пройдена")

    # Шаг 5 — открытие таблицы
    try:
        sh = client.open_by_key(GSHEET_ID)
    except Exception as e:
        code, human = _classify_gs_error(e)
        if code == "NO_ACCESS":
            human += f" — конкретно: {email}"
        fail(f"Открытие таблицы: {human}")
        return False, lines
    ok(f"Таблица открыта: «{sh.title}»")

    # Шаг 6 — вкладка «Лиды»
    try:
        try:
            ws = sh.worksheet(GSHEET_WORKSHEET)
            ok(f"Вкладка «{GSHEET_WORKSHEET}» найдена")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=GSHEET_WORKSHEET, rows=1000,
                                  cols=len(LEAD_HEADER))
            ok(f"Вкладки не было — создал «{GSHEET_WORKSHEET}»")
    except Exception as e:
        _, human = _classify_gs_error(e)
        fail(f"Вкладка: {human}")
        return False, lines

    # Шаг 7 — заголовок
    try:
        if not ws.row_values(1):
            ws.append_row(LEAD_HEADER, value_input_option="USER_ENTERED")
            try:
                ws.format("A1:J1", {"textFormat": {"bold": True}})
                ws.freeze(rows=1)
            except Exception:
                pass
            ok("Заголовок записан (жирный, строка закреплена)")
        else:
            ok("Заголовок уже на месте")
    except Exception as e:
        _, human = _classify_gs_error(e)
        fail(f"Заголовок: {human}")
        return False, lines

    # Шаг 8 — тестовая строка
    try:
        now = datetime.now()
        ws.append_row(
            [now.strftime("%d.%m.%Y"), now.strftime("%H:%M"), "Тест", "—",
             "Команда /leadtest", tester_name, "", "",
             "Полный цикл записи проверен", "Новый"],
            value_input_option="USER_ENTERED")
    except Exception as e:
        _, human = _classify_gs_error(e)
        fail(f"Запись строки: {human}")
        return False, lines
    ok("Тестовая строка записана — проверьте вкладку «Лиды»")
    return True, lines


async def log_lead(kind: str, track: str, source: str,
                   user: types.User, message: str = "") -> bool:
    """Пишет лид в Google-таблицу; при сбое — в CSV. Возвращает True при успехе Sheets."""
    global _sheets_ok, _ws
    now = datetime.now()
    uname = f"@{user.username}" if user.username else ""
    row = [
        now.strftime("%d.%m.%Y"), now.strftime("%H:%M"),
        kind, track, source,
        (user.full_name or "").strip(), uname, str(user.id),
        (message or "").replace("\n", " ").strip()[:500],
        "Новый",
    ]
    if not GSHEET_ID:
        _fallback_csv(row)
        return False
    try:
        await asyncio.to_thread(_append_row_sync, row)
        if _sheets_ok is not True:
            _sheets_ok = True
            log.info("Журнал лидов: запись в Google Sheets работает")
        return True
    except Exception as e:
        _sheets_ok = False
        _ws = None  # сброс кэша: после устранения причины переподключимся без рестарта
        code, human = _classify_gs_error(e)
        log.error(f"Журнал лидов → резервный CSV. Причина [{code}]: {human}. "
                  f"Техдетали: {type(e).__name__}: {e}")
        _fallback_csv(row)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# ВЫЗОВ CLAUDE
# ══════════════════════════════════════════════════════════════════════════════
async def ask_claude(user_id: int, user_text: str) -> str:
    """Отправляет историю + новое сообщение в Claude, возвращает ответ."""
    msgs = list(history[user_id])
    msgs.append({"role": "user", "content": user_text})

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
        text = "".join(block.text for block in resp.content if block.type == "text")
        history[user_id].append({"role": "user", "content": user_text})
        history[user_id].append({"role": "assistant", "content": text})
        return text.strip()
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return ("Извините, временная техническая заминка на нашей стороне. "
                "Попробуйте повторить вопрос через минуту — или напишите на info@rusmashgroup.ru.")


# ══════════════════════════════════════════════════════════════════════════════
# ЭСКАЛАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════
async def escalate(user: types.User, user_text: str, assistant_text: str):
    """Шлёт резюме в группу инженеров, пишет лид в журнал, ставит чат на паузу."""
    paused_chats.add(user.id)
    save_paused(paused_chats)

    uname = f"@{user.username}" if user.username else "(без username)"
    fullname = user.full_name or "—"
    client_info[user.id] = {"name": fullname, "username": uname}

    clean = assistant_text.replace(ESCALATION_TAG, "").strip()

    # журнал лидов
    track = user_track.get(user.id, "Общепром")
    await log_lead("Эскалация", track, "Диалог с ассистентом", user, user_text)

    summary = (
        f"🔔 *ЭСКАЛАЦИЯ — ExCentro Assistant*\n\n"
        f"👤 Клиент: {fullname} {uname}\n"
        f"🆔 user\\_id: `{user.id}`\n"
        f"🏷 Трек: {track}\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"💬 *Запрос клиента:*\n{user_text}\n\n"
        f"🤖 *Что ответил ассистент:*\n{clean[:800]}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Чат клиента поставлен на *паузу* — ассистент молчит.\n"
        f"Чтобы ответить клиенту: напишите в этой группе\n"
        f"`/say {user.id} ваш текст`\n"
        f"Чтобы вернуть чат ассистенту: `/release {user.id}`"
    )
    try:
        await bot.send_message(config.ESCALATION_CHAT_ID, summary, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Не удалось отправить эскалацию в группу: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ВЕТРЯНОЙ ТРЕК — deep-links, материалы, уведомления о лидах
# ══════════════════════════════════════════════════════════════════════════════
WIND_STARTS = {
    "wind_ru":  "Презентация «Ветроэнергетика»",
    "rosatom":  "Письмо / QR — Росатом",
    "nda_wind": "Кнопка «NDA и переговоры» · ВЭУ",
}

WIND_GREETING = (
    "Здравствуйте! Я ExCentro Assistant — на связи по ветроэнергетическому "
    "направлению.\n\n"
    "Наше предложение комплексное, в два трека:\n"
    "1️⃣ *Сегодня* — приводы поворота гондолы и питча (ЭЦ-ПЗР): одна ступень "
    "вместо трёх, выше КПД, ниже масса и себестоимость. Полностью совместимо "
    "с безредукторной (direct-drive) платформой действующих российских ВЭУ.\n"
    "2️⃣ *Перспектива* — запатентованный одноступенчатый мультипликатор ВМ "
    "(5 т против 11 т) для редукторных архитектур и экспортных проектов.\n\n"
    "Подобрать привод под ваши параметры можно в калькуляторе: {url}\n\n"
    "Расскажите о вашей задаче: роль (производитель ВЭУ / оператор парка / "
    "институт), интересующий узел и параметры — подскажу решение."
)

NDA_WIND_REPLY = (
    "Принято — запрос на NDA и переговоры по ветроэнергетическому направлению "
    "зафиксирован, наш инженер свяжется с вами.\n\n"
    "Чтобы ускорить подготовку соглашения, укажите, пожалуйста: организацию, "
    "ИНН и контактное лицо."
)

def _seed_wind_context(user_id: int):
    """Помечаем диалог как ветряной, чтобы Claude отвечал в контексте ВЭУ."""
    user_track[user_id] = "ВЭУ"
    history[user_id].append({
        "role": "user",
        "content": ("[Служебный контекст: клиент пришёл из ветроэнергетического трека. "
                    "Веди диалог в контексте ВЭУ: трек 1 — приводы поворота гондолы и "
                    "питча ЭЦ-ПЗР (совместимы с direct-drive платформой), трек 2 — "
                    "перспективный мультипликатор ВМ для редукторных архитектур. "
                    "Не предлагай менять архитектуру турбины заказчика.]"),
    })
    history[user_id].append({"role": "assistant", "content": "Принято, контекст ВЭУ."})

async def _send_wind_teaser(chat_id: int):
    path = getattr(config, "WIND_TEASER_FILE", "").strip()
    if not path:
        return
    if not os.path.isfile(path):
        log.warning(f"WIND_TEASER_FILE задан, но файл не найден: {path}")
        return
    try:
        await bot.send_document(
            chat_id, FSInputFile(path),
            caption="Неконфиденциальный тизер: приводы ВЭУ на ЭЦ-зацеплении (PDF)",
        )
    except Exception as e:
        log.error(f"Не удалось отправить тизер: {e}")

async def _send_nda_draft(chat_id: int):
    path = getattr(config, "NDA_FILE", "").strip()
    if not path or not os.path.isfile(path):
        return
    try:
        await bot.send_document(
            chat_id, FSInputFile(path),
            caption="Проект соглашения о конфиденциальности (для ознакомления)",
        )
    except Exception as e:
        log.error(f"Не удалось отправить NDA: {e}")

async def _notify_wind_lead(user: types.User, source_label: str, is_nda: bool, journal_ok: bool):
    uname = f"@{user.username}" if user.username else "(без username)"
    fullname = user.full_name or "—"
    client_info[user.id] = {"name": fullname, "username": uname}
    kind = "🔏 *NDA-ЗАПРОС · ВЭУ*" if is_nda else "🌬 *НОВЫЙ ЛИД · ВЭУ*"
    journal_note = ("📒 Записан в журнал лидов (Google-таблица)"
                    if journal_ok else
                    "📒 Журнал: записан в резервный CSV — проверьте Google Sheets")
    text = (
        f"{kind}\n\n"
        f"👤 {fullname} {uname}\n"
        f"🆔 user\\_id: `{user.id}`\n"
        f"📥 Источник: {source_label}\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"{journal_note}\n\n"
        f"Ассистент ведёт диалог в контексте ВЭУ. "
        f"Перехватить: `/say {user.id} ...`"
    )
    try:
        await bot.send_message(config.ESCALATION_CHAT_ID, text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Не удалось отправить уведомление о лиде: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ХЭНДЛЕРЫ — ЛИЧНЫЕ СООБЩЕНИЯ КЛИЕНТОВ
# ══════════════════════════════════════════════════════════════════════════════
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
    history.pop(message.from_user.id, None)
    user_track.pop(message.from_user.id, None)
    paused_chats.discard(message.from_user.id)
    save_paused(paused_chats)
    arg = (command.args or "").strip().lower()

    # ── Ветряной трек ──────────────────────────────────────────────────────
    if arg in WIND_STARTS:
        _seed_wind_context(message.from_user.id)
        await message.answer(
            WIND_GREETING.format(url=config.CALCULATOR_URL),
            parse_mode="Markdown",
        )
        await _send_wind_teaser(message.chat.id)
        if arg == "nda_wind":
            await message.answer(NDA_WIND_REPLY)
            await _send_nda_draft(message.chat.id)
        kind = "NDA-запрос" if arg == "nda_wind" else "Новый лид"
        journal_ok = await log_lead(kind, "ВЭУ", WIND_STARTS[arg], message.from_user)
        await _notify_wind_lead(
            message.from_user, WIND_STARTS[arg],
            is_nda=(arg == "nda_wind"), journal_ok=journal_ok,
        )
        return

    # ── Общий трек (как раньше) ────────────────────────────────────────────
    if arg in GREETINGS:
        lang = arg
    else:
        tg_lang = (message.from_user.language_code or "ru")[:2]
        lang = tg_lang if tg_lang in GREETINGS else "ru"
    await message.answer(GREETINGS[lang].format(url=config.CALCULATOR_URL))


@dp.message(F.chat.type == ChatType.PRIVATE)
async def handle_client(message: Message):
    user = message.from_user
    user_text = (message.text or "").strip()
    if not user_text:
        return

    if user.id in paused_chats:
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

    need_escalation = (ESCALATION_TAG in answer) or bool(ESCALATION_PATTERNS.search(user_text))

    shown = answer.replace(ESCALATION_TAG, "").strip()
    await message.answer(shown[:4096])

    if need_escalation:
        await escalate(user, user_text, answer)
        await message.answer(
            "Я передал ваш запрос нашему инженеру — он свяжется с вами. "
            "Чтобы ускорить, можете сразу указать компанию и удобный контакт."
        )


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


@dp.message(Command("leadtest"))
async def cmd_leadtest(message: Message):
    """/leadtest — пошаговая проверка журнала: конфиг → ключ → доступ → вкладка → запись."""
    if message.chat.id != config.ESCALATION_CHAT_ID:
        return
    status = await message.reply("🔎 Проверяю журнал лидов по шагам…")
    try:
        okall, lines = await asyncio.to_thread(
            _diagnose_sync, message.from_user.full_name or "инженер")
    except Exception as e:
        code, human = _classify_gs_error(e)
        await status.edit_text(f"❌ Непредвиденный сбой [{code}]: {human}")
        return
    verdict = ("\n\n🎉 Полный цикл работает — журнал готов к бою."
               if okall else
               "\n\n⛔ Исправьте отмеченный шаг и повторите /leadtest. "
               "Детали также в логе: journalctl -u excentro-assistant -n 20")
    await status.edit_text("\n".join(lines) + verdict)


@dp.message(Command("chatid"))
async def cmd_chatid(message: Message):
    """/chatid — показать ID текущего чата (утилита для настройки)."""
    await message.reply(f"chat_id: `{message.chat.id}`", parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
async def main():
    log.info("ExCentro Assistant запускается...")
    log.info(f"Модель: {config.CLAUDE_MODEL}")
    log.info(f"Группа эскалации: {config.ESCALATION_CHAT_ID}")
    if getattr(config, "WIND_TEASER_FILE", ""):
        log.info(f"Ветряной тизер: {config.WIND_TEASER_FILE}")
    if GSHEET_ID:
        log.info(f"Журнал лидов: Google Sheets …{GSHEET_ID[-8:]} / «{GSHEET_WORKSHEET}» "
                 f"(ключ: {GSHEET_KEY_FILE})")
    else:
        log.info("Журнал лидов: GSHEET_ID не задан — только резервный CSV")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
