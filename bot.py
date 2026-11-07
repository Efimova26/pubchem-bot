"""Telegram-бот: только обвязка вокруг service.py (команды, форматирование, расписание)."""
from __future__ import annotations

import html
import logging
import os
from datetime import timedelta

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (Application, ApplicationBuilder, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from db import Database
from pubchem import Compound, PubChemClient
from service import lookup, refresh_all

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # не писать в лог каждый запрос с токеном
logging.getLogger("apscheduler").setLevel(logging.WARNING)  # не шуметь о каждом запуске задачи
log = logging.getLogger("bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "compounds.db")
REFRESH_HOURS = float(os.environ.get("REFRESH_HOURS", "24"))
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"]) if os.environ.get("ADMIN_USER_ID") else None

SOURCE_LABEL = {"api": "PubChem", "cache": "база бота (кэш)"}

HELP_TEXT = (
    "Пришли название вещества на английском, например <code>aspirin</code> "
    "или <code>caffeine</code>, и я найду его в PubChem.\n\n"
    "/history — твои последние запросы\n"
    "/stats — что лежит в базе\n"
    "/help — эта подсказка"
)


def format_compound(c: Compound, source: str, other_name: bool) -> str:
    e = html.escape
    lines = [
        f"<b>{e(c.iupac_name or c.formula)}</b>",
        f'CID: <a href="https://pubchem.ncbi.nlm.nih.gov/compound/{c.cid}">{c.cid}</a>',
        f"Формула: {e(c.formula)}",
        f"Молярная масса: {c.mol_weight:.2f} г/моль",
        f"XLogP: {c.xlogp if c.xlogp is not None else '—'}",
        f"SMILES: <code>{e(c.smiles)}</code>",
        f"InChIKey: <code>{e(c.inchikey)}</code>",
        "",
        f"Источник: {SOURCE_LABEL.get(source, source)}",
    ]
    if other_name:
        lines.append("Это вещество уже было в базе под другим названием — дубль не создан.")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! " + HELP_TEXT, parse_mode=ParseMode.HTML)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    client: PubChemClient = context.bot_data["client"]
    result = await lookup(db, client, update.message.text, update.effective_user.id)
    if result.compound is None:
        await update.message.reply_text(result.message)
        return
    await update.message.reply_text(
        format_compound(result.compound, result.source, result.known_under_other_name),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    rows = db.history(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Запросов пока нет.")
        return
    lines = ["<b>Последние запросы</b>"]
    for r in rows:
        found = f"{html.escape(r['formula'])} (CID {r['cid']})" if r["formula"] else r["status"]
        lines.append(f"{r['created_at'][:16].replace('T', ' ')} · {html.escape(r['raw_query'])} → {found}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = context.bot_data["db"].stats()
    answered = s["cache_hits"] + s["api_calls"]
    hit_rate = f"{100 * s['cache_hits'] / answered:.0f}%" if answered else "—"
    await update.message.reply_text(
        f"Веществ в базе: {s['compounds']}\n"
        f"Названий (синонимов): {s['aliases']}\n"
        f"Запросов всего: {s['queries']}, не найдено: {s['not_found']}\n"
        f"Ответов из кэша: {hit_rate}\n"
        f"Изменений, найденных при обновлениях: {s['changes']}"
    )


async def refresh_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    report = await refresh_all(context.bot_data["db"], context.bot_data["client"])
    if ADMIN_USER_ID and (report.changed or report.failed):
        await context.bot.send_message(
            ADMIN_USER_ID,
            f"Обновление базы: проверено {report.checked}, "
            f"изменилось {len(report.changed)}, ошибок {len(report.failed)}.",
        )


async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("Эта команда только для администратора.")
        return
    report = await refresh_all(context.bot_data["db"], context.bot_data["client"])
    await update.message.reply_text(
        f"Проверено {report.checked}, изменилось {len(report.changed)}, ошибок {len(report.failed)}."
    )


async def post_init(app: Application) -> None:
    app.bot_data["db"] = Database(DB_PATH)
    app.bot_data["client"] = PubChemClient()
    app.job_queue.run_repeating(refresh_job, interval=timedelta(hours=REFRESH_HOURS),
                                first=timedelta(minutes=1))
    log.info("Bot started, DB=%s, refresh every %s h", DB_PATH, REFRESH_HOURS)


async def post_shutdown(app: Application) -> None:
    await app.bot_data["client"].aclose()
    app.bot_data["db"].close()


def main() -> None:
    if not TOKEN:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN: создай файл .env по образцу .env.example")
    app = (ApplicationBuilder().token(TOKEN)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("refresh", refresh_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
