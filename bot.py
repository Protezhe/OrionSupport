#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-бот техподдержки Орион.

Использует ту же базу проблем/решений (CSV / Google Sheets)
и логику нечёткого поиска из search_solution.py.

Запуск:
  source .venv/bin/activate
  python bot.py
"""

import logging
import time

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from search_solution import (
    load_config,
    get_sheet_url,
    get_object_synonyms,
    load_rows_with_fallback,
    detect_object_code,
    find_best_with_object,
    fetch_rows,
    _get_field_case_insensitive,
)

# ─── Config ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

config = load_config()
BOT_TOKEN = config.get("telegram_bot_token", "")
TOP_N = 1
MIN_SCORE = 0.35

# ─── Global state ────────────────────────────────────────────────────────────

sheet_url = get_sheet_url(config)
object_synonyms = get_object_synonyms(config)
rows: list[dict] = []
upload_mode: dict[int, float] = {}  # user_id -> expiry timestamp

UPLOAD_TIMEOUT = 300  # 5 минут


def refresh_rows() -> None:
    global rows
    new = fetch_rows(sheet_url)
    if new:
        rows = new
        logger.info("Данные обновлены из Google Sheets (%d строк).", len(rows))


# ─── Formatting ──────────────────────────────────────────────────────────────


def _parse_file_ids(raw: str) -> list[str]:
    """Split comma-separated file_ids, skip empty."""
    return [fid.strip() for fid in raw.split(",") if fid.strip()]


def format_result(scored: list) -> tuple[str, list[str], list[str]]:
    """Format search results. Returns (text, video_file_ids, photo_file_ids)."""
    good = [(score, row) for score, row in scored if score >= MIN_SCORE]
    if not good:
        return (
            "Эхх, решение не нашлось, пусечка…\n"
            "Попробуй переформулировать запрос или напиши дежурному инженеру, ладненько?",
            [],
            [],
        )

    score, row = good[0]
    problem = _get_field_case_insensitive(row, "Проблема")
    solution = _get_field_case_insensitive(row, "Решение")
    solution2 = _get_field_case_insensitive(row, "Решение_2")
    obj = _get_field_case_insensitive(row, "Объект")

    header = "▸ Найдено"
    if obj:
        header += f"  [{obj.upper()}]"
    header += f"  (совпадение {score:.0%})"

    block = [header]
    if problem:
        block.append(f"Проблема: {problem}")
    if solution:
        block.append(f"✅ Решение: {solution}")
    if solution2.strip():
        block.append(f"✅ Решение 2: {solution2}")

    video_ids = _parse_file_ids(_get_field_case_insensitive(row, "Видео"))
    photo_ids = _parse_file_ids(_get_field_case_insensitive(row, "Фото"))

    return "\n".join(block), video_ids, photo_ids


# ─── Handlers ────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "Я — бот техподдержки Орион, твоя аниме‑помощница.\n\n"
    "Опиши проблему, и я постараюсь найти решение, сенпай.\n\n"
    "Примеры:\n"
    "• «розовый цвет проектора»\n"
    "• «нет звука в зале кп»\n"
    "• «не работает платформа»\n\n"
    "Команды:\n"
    "/start — приветствие\n"
    "/help — эта справка\n"
    "/reload — обновить базу знаний\n"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет-привет! Я аниме‑тянка из техподдержки Орион.\n\n"
        "Опиши проблему — я поищу решение в базе знаний, ага.\n"
        "Если нужна справка: /help"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    upload_mode[uid] = time.time() + UPLOAD_TIMEOUT
    await update.message.reply_text(
        "Режим загрузки включён на 5 минут.\n"
        "Отправь видео или фото — я верну file_id для таблицы."
    )


async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    expiry = upload_mode.get(uid, 0)

    # Upload mode активен — вернуть file_id
    if time.time() <= expiry:
        if update.message.video or update.message.document:
            media = update.message.video or update.message.document
            label = "Видео"
        elif update.message.photo:
            media = update.message.photo[-1]
            label = "Фото"
        else:
            return
        await update.message.reply_text(
            f"{label} file_id для таблицы:\n\n<code>{media.file_id}</code>",
            parse_mode="HTML",
        )
        return

    # Upload mode выключен — обработать подпись как обычный запрос
    caption = (update.message.caption or "").strip()
    if not caption:
        return

    # В групповых чатах обрабатываем подпись только если бота тэгнули
    if update.effective_chat.type in ("group", "supergroup"):
        bot_username = context.bot.username
        if f"@{bot_username}" not in caption:
            return
        caption = caption.replace(f"@{bot_username}", "").strip()

    if caption:
        await _search_and_reply(update, caption)


async def _search_and_reply(update: Update, query: str) -> None:
    if not rows:
        await update.message.reply_text(
            "База знаний пуста. Попробуй /reload или напиши инженеру."
        )
        return
    logger.info("Запрос от %s: %s", update.effective_user.first_name, query)
    obj_code = detect_object_code(query, object_synonyms)
    scored = find_best_with_object(query, rows, TOP_N, obj_code)
    answer, video_ids, photo_ids = format_result(scored)
    await update.message.reply_text(answer)
    for pid in photo_ids:
        try:
            await update.message.reply_photo(pid)
        except Exception:
            logger.warning("Не удалось отправить фото: %s", pid)
    for vid in video_ids:
        try:
            await update.message.reply_video(vid)
        except Exception:
            logger.warning("Не удалось отправить видео: %s", vid)


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    refresh_rows()
    await update.message.reply_text(f"База обновлена. Записей: {len(rows)}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return

    # В групповых чатах отвечаем только если бота тэгнули
    if update.effective_chat.type in ("group", "supergroup"):
        bot_username = context.bot.username
        if f"@{bot_username}" not in text:
            return
        query = text.replace(f"@{bot_username}", "").strip()
    else:
        query = text

    if query:
        await _search_and_reply(update, query)


# ─── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    # Initial data load
    global rows
    rows = load_rows_with_fallback(sheet_url)
    if not rows:
        logger.warning("Не удалось загрузить данные при старте!")

    logger.info("Загружено %d записей. Запускаю бота…", len(rows))

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("upload", cmd_upload))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO | filters.PHOTO, handle_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
