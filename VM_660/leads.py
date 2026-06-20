#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
leads.py — модуль сбора и журналирования лидов ExCentro.
Карточка лида по каждому user_id, извлечение полей через Claude,
upsert в Google Sheets через Apps Script webhook.
"""

import json
import logging
import re
import time
import urllib.request
from collections import defaultdict

import config

log = logging.getLogger("excentro-assistant.leads")

# Обязательные поля лида (для оценки полноты)
REQUIRED_FIELDS = ["entity", "company", "role", "contact", "industry", "goal", "region"]

# Человекочитаемые названия — для резюме инженеру
FIELD_LABELS = {
    "type": "Тип обращения",
    "lang": "Язык",
    "entity": "Тип лица",
    "company": "Компания/профиль",
    "role": "Должность/функция",
    "contact": "Контакты",
    "industry": "Отрасль/применение",
    "params": "Параметры (момент/скорость)",
    "region": "Регион/страна",
    "goal": "Цель обращения",
}

# Карточка лида: user_id -> dict полей. Пустые строки = не собрано.
def _empty_card():
    return {k: "" for k in
            ["type", "lang", "entity", "company", "role", "contact",
             "industry", "params", "region", "goal", "summary", "tg_name"]}

cards = defaultdict(_empty_card)
# Когда последний раз писали в Sheets (троттлинг, чтобы не дёргать на каждое слово)
_last_push = defaultdict(float)


def update_card(user_id: int, extracted: dict):
    """Обновляет карточку непустыми извлечёнными значениями."""
    card = cards[user_id]
    for k, v in (extracted or {}).items():
        if k in card and isinstance(v, str) and v.strip():
            # не затираем уже собранное пустым/«не указано»
            if v.strip().lower() not in ("не указано", "unknown", "n/a", "—", "-"):
                card[k] = v.strip()


def missing_required(user_id: int):
    """Список ещё не собранных обязательных полей."""
    card = cards[user_id]
    return [f for f in REQUIRED_FIELDS if not card.get(f)]


def card_summary(user_id: int) -> str:
    """Человекочитаемое резюме карточки для инженера."""
    card = cards[user_id]
    lines = []
    for k in ["type", "entity", "company", "role", "contact",
              "industry", "params", "region", "goal"]:
        v = card.get(k)
        if v:
            lines.append(f"• {FIELD_LABELS.get(k, k)}: {v}")
    miss = missing_required(user_id)
    if miss:
        lines.append("• Не собрано: " + ", ".join(FIELD_LABELS.get(m, m) for m in miss))
    return "\n".join(lines) if lines else "(данные ещё не собраны)"


def push_to_sheets(user_id: int, force: bool = False) -> bool:
    """
    Upsert карточки в Google Sheets через Apps Script webhook.
    Троттлинг: не чаще раза в N секунд на пользователя, если не force.
    """
    url = getattr(config, "SHEETS_WEBHOOK_URL", "").strip()
    secret = getattr(config, "SHEETS_WEBHOOK_SECRET", "").strip()
    if not url or not secret:
        return False  # журнал не настроен — тихо пропускаем

    now = time.time()
    if not force and (now - _last_push[user_id] < 8):
        return False  # слишком часто, пропускаем (последнее состояние допишется позже)
    _last_push[user_id] = now

    payload = {
        "secret": secret,
        "data": {**cards[user_id], "user_id": str(user_id)},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
            ok = '"ok":true' in body.replace(" ", "")
            if not ok:
                log.warning(f"Sheets webhook ответил: {body[:200]}")
            return ok
    except Exception as e:
        log.error(f"Sheets webhook error: {e}")
        return False


# ── Извлечение полей из реплики через Claude (structured) ──────────────────────
# Возвращает (ответ_клиенту, извлечённые_поля). Использует отдельный JSON-блок,
# который модель добавляет в конце и который мы вырезаем перед показом клиенту.

EXTRACTION_INSTRUCTION = """
=== СБОР ДАННЫХ О КЛИЕНТЕ (КРИТИЧЕСКИ ВАЖНО) ===
Параллельно с ответом ты ведёшь карточку лида. В КОНЦЕ каждого ответа добавляй
служебный блок строго в формате (его клиент НЕ увидит, система вырежет):

<LEAD>{"entity":"","company":"","role":"","contact":"","industry":"","params":"","region":"","goal":"","summary":""}</LEAD>

Заполняй ТОЛЬКО те поля, что явно следуют из диалога; остальные оставляй пустыми "".
- entity: тип лица — "физ" или "юр"
- company: название компании / профиль (для физлица — род деятельности)
- role: должность/функция (менеджер / инженер / закупщик / учащийся / преподаватель / …)
- contact: телефон, email или @username — как клиент дал
- industry: отрасль/применение (нефтегаз, горное, судостроение, энергетика, станки…)
- params: технические параметры, если назвал (момент кН·м, скорость об/мин)
- region: страна/регион
- goal: цель обращения (получить документацию / подобрать редуктор / цена-ТКП / NDA / сотрудничество…)
- summary: 1 короткая фраза — суть обращения

ПОЛИТИКА СБОРА (мягкая): по ходу живого диалога ненавязчиво уточняй недостающие
обязательные поля (тип лица, компания, должность, контакты, отрасль, цель, регион).
Если клиент не хочет что-то сообщать — НЕ дави, продолжай помогать. Один-два вопроса
за раз максимум, естественно вплетённые в разговор, а не анкетой.
"""

LEAD_RE = re.compile(r"<LEAD>\s*(\{.*?\})\s*</LEAD>", re.DOTALL)

def split_answer_and_lead(raw: str):
    """Вырезает <LEAD>{...}</LEAD> из ответа модели. Возвращает (чистый_текст, dict|None)."""
    extracted = None
    m = LEAD_RE.search(raw)
    if m:
        try:
            extracted = json.loads(m.group(1))
        except Exception as e:
            log.warning(f"LEAD JSON parse fail: {e}")
        raw = LEAD_RE.sub("", raw)
    return raw.strip(), extracted
