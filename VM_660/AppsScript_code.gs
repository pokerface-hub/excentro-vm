// ════════════════════════════════════════════════════════════════════════════
// ExCentro — приём лидов из Telegram-бота в Google Sheets
// Apps Script (привязан к таблице). Принимает POST с данными лида,
// дописывает строку в журнал. Возвращает JSON-подтверждение.
// ════════════════════════════════════════════════════════════════════════════

// Простой секрет для защиты от посторонних запросов.
// ДОЛЖЕН совпадать с WEBHOOK_SECRET в config.py бота.
const SHARED_SECRET = "ВПИШИ_СВОЙ_СЕКРЕТ_например_excentro2026xZ";

// Имя листа-журнала
const SHEET_NAME = "Лиды";

// Порядок колонок журнала (если меняешь — синхронизируй с ботом)
const HEADERS = [
  "Дата/время",      // A
  "Тип обращения",   // B  техзапрос / NDA / опросный / общий
  "Язык",            // C
  "Тип лица",        // D  физ / юр
  "Компания/профиль",// E
  "Должность",       // F  менеджер / инженер / учащийся / преподаватель / ...
  "Контакты",        // G
  "Отрасль/применение",// H
  "Параметры",       // I  момент / скорость и т.п.
  "Регион/страна",   // J
  "Цель обращения",  // K
  "Имя в Telegram",  // L
  "user_id",         // M
  "Резюме диалога",  // N
  "Статус",          // O  Новый / В работе / Закрыт
];

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);

    // Проверка секрета
    if (body.secret !== SHARED_SECRET) {
      return json({ ok: false, error: "forbidden" });
    }

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    let sh = ss.getSheetByName(SHEET_NAME);
    if (!sh) {
      sh = ss.insertSheet(SHEET_NAME);
    }
    // Если лист пустой — пишем заголовки и форматируем
    if (sh.getLastRow() === 0) {
      sh.appendRow(HEADERS);
      const hr = sh.getRange(1, 1, 1, HEADERS.length);
      hr.setFontWeight("bold").setBackground("#1E2761").setFontColor("#FFFFFF");
      sh.setFrozenRows(1);
      sh.autoResizeColumns(1, HEADERS.length);
    }

    const now = Utilities.formatDate(new Date(), "Europe/Moscow", "yyyy-MM-dd HH:mm");
    const d = body.data || {};
    const uid = String(d.user_id || "");

    // ── UPSERT: ищем существующую строку по user_id (колонка M = 13) ──────────
    let targetRow = 0;
    if (uid) {
      const lastRow = sh.getLastRow();
      if (lastRow > 1) {
        const ids = sh.getRange(2, 13, lastRow - 1, 1).getValues(); // колонка M
        for (let i = 0; i < ids.length; i++) {
          if (String(ids[i][0]) === uid) { targetRow = i + 2; break; }
        }
      }
    }

    if (targetRow > 0) {
      // ── ОБНОВЛЕНИЕ существующей строки ──────────────────────────────────────
      // Берём текущие значения, перезаписываем только непустыми новыми.
      const cur = sh.getRange(targetRow, 1, 1, HEADERS.length).getValues()[0];
      const upd = [
        cur[0] || now,                          // Дата/время — не трогаем (первое обращение)
        d.type     || cur[1],
        d.lang     || cur[2],
        d.entity   || cur[3],
        d.company  || cur[4],
        d.role     || cur[5],
        d.contact  || cur[6],
        d.industry || cur[7],
        d.params   || cur[8],
        d.region   || cur[9],
        d.goal     || cur[10],
        d.tg_name  || cur[11],
        uid,
        d.summary  || cur[13],
        cur[14] || "Новый",                     // Статус — не сбрасываем (ведётся вручную)
      ];
      // последнее обновление допишем в «Резюме диалога» с отметкой времени, если пришло
      sh.getRange(targetRow, 1, 1, HEADERS.length).setValues([upd]);
      return json({ ok: true, mode: "update", row: targetRow });
    } else {
      // ── НОВАЯ строка ────────────────────────────────────────────────────────
      const row = [
        now,
        d.type || "", d.lang || "", d.entity || "", d.company || "",
        d.role || "", d.contact || "", d.industry || "", d.params || "",
        d.region || "", d.goal || "", d.tg_name || "", uid,
        d.summary || "", "Новый",
      ];
      sh.appendRow(row);
      return json({ ok: true, mode: "insert", row: sh.getLastRow() });
    }
  } catch (err) {
    return json({ ok: false, error: String(err) });
  }
}

// GET — для быстрой проверки, что веб-приложение живо
function doGet() {
  return json({ ok: true, service: "ExCentro lead intake", time: new Date().toISOString() });
}

function json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
