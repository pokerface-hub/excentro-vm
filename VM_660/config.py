# config.py — конфигурация ExCentro Assistant
# ВНИМАНИЕ: впиши свои значения. Этот файл НЕ выкладывать в публичный git.

import os

# ── Токен нового Telegram-бота @ExCentroAssistantbot (от @BotFather) ──────────
# Можно задать прямо здесь ИЛИ через переменную окружения BOT_TOKEN.
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВПИШИ_ТОКЕН_БОТА_СЮДА")

# ── Claude API ключ (console.anthropic.com) ──────────────────────────────────
# Лучше через переменную окружения ANTHROPIC_API_KEY, но можно и здесь.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "ВПИШИ_CLAUDE_API_КЛЮЧ_СЮДА")

# ── ID группы эскалации «ExCentro — эскалация» ───────────────────────────────
# Отрицательное число вида -1001234567890. Как узнать — см. README, шаг 5.
ESCALATION_CHAT_ID = int(os.getenv("ESCALATION_CHAT_ID", "-1000000000000"))

# ── Прокси для api.anthropic.com (если сервер в заблокированном регионе) ─────
# Формат: "http://user:pass@host:port"  или  "socks5://user:pass@host:port"
# Оставь пустым "", если сервер в разрешённом регионе.
ANTHROPIC_PROXY = os.getenv("ANTHROPIC_PROXY", "")

# ── Модель Claude ────────────────────────────────────────────────────────────
# claude-sonnet-4-6 — оптимум цена/качество для ассистента первой линии.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 1024
TEMPERATURE = 0.3

# ── Ссылка на калькулятор подбора (после публикации на GitHub Pages) ──────────
CALCULATOR_URL = os.getenv(
    "CALCULATOR_URL",
    "https://pokerface-hub.github.io/excentro-vm/excentro-calculator.html"
)

# ── Журнал лидов: Google Sheets через Apps Script webhook ─────────────────────
# URL веб-приложения (вида https://script.google.com/macros/s/.../exec)
SHEETS_WEBHOOK_URL = os.getenv("SHEETS_WEBHOOK_URL", "")
# Секрет — ДОЛЖЕН совпадать с SHARED_SECRET в коде Apps Script
SHEETS_WEBHOOK_SECRET = os.getenv("SHEETS_WEBHOOK_SECRET", "")

# ── Пути к файлам промпта и базы знаний ───────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SYSTEM_PROMPT_FILE = os.path.join(BASE_DIR, "system_prompt.txt")
KNOWLEDGE_BASE_FILE = os.path.join(BASE_DIR, "knowledge_base.txt")

# ── Память диалога: сколько последних сообщений держим в контексте ────────────
HISTORY_LIMIT = 12  # пар «вопрос-ответ» суммарно (6 обменов)

# ── Файл для хранения состояния (паузы AI по чатам) ──────────────────────────
STATE_FILE = os.path.join(BASE_DIR, "paused_chats.json")
