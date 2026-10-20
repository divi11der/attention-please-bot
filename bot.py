"""Attention Please! — Telegram support bot for a free Render Web Service.

Telegram sends updates over a verified HTTPS webhook; requests are stored in
Neon PostgreSQL so they survive Render sleeping and redeployments.
"""

import hashlib
import logging
import os
import ssl
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("attention_please")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
TEAM_CHAT_ID = int(os.environ.get("TEAM_CHAT_ID", "0"))
BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or ""
).strip().rstrip("/")
WEBHOOK_PATH = "/telegram/webhook"
PORT = int(os.environ.get("PORT", "10000"))

CATEGORIES = {
    "bug": "🐞 Сообщить об ошибке",
    "idea": "💡 Предложить идею",
    "question": "❓ Задать вопрос",
}
PROMPTS = {
    "bug": "Опиши ошибку: что случилось и как её повторить? Можно приложить скриншот.",
    "idea": "Расскажи, что ты хочешь добавить или изменить в игре!",
    "question": "Напиши свой вопрос об игре — разработчики постараются ответить.",
}
MENU = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"category:{key}")]
        for key, label in CATEGORIES.items()
    ] + [[InlineKeyboardButton(text="❌ Отменить обращение", callback_data="cancel_request")]]
)
CANCEL_MENU = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить обращение", callback_data="cancel_request")],
        [InlineKeyboardButton(text="⬅️ Выбрать другую тему", callback_data="back_to_menu")],
    ]
)

dp = Dispatcher()
pool: asyncpg.Pool | None = None


def database() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("База данных ещё не подключена")
    return pool


def clean_database_url(url: str) -> str:
    """Remove Neon libpq-only query flags unsupported by asyncpg.

    Neon provides ?sslmode=require&channel_binding=require. TLS is instead
    strictly enabled with an SSLContext that verifies the hostname.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("postgresql", "postgres") or not parts.hostname:
        raise ValueError("DATABASE_URL должен быть PostgreSQL URL из Neon")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


async def initialize_database() -> None:
    global pool
    # The SSLContext validates both certificate and host (unlike ssl='require').
    pool = await asyncpg.create_pool(
        dsn=clean_database_url(DATABASE_URL),
        ssl=ssl.create_default_context(),
        min_size=1,
        max_size=2,
        timeout=30,
        command_timeout=30,
        max_inactive_connection_lifetime=60,
    )
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS drafts (
            player_id BIGINT PRIMARY KEY,
            category TEXT NOT NULL
        )
    """)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id BIGSERIAL PRIMARY KEY,
            player_id BIGINT NOT NULL,
            category TEXT NOT NULL,
            original_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            player_message_id BIGINT NOT NULL,
            team_header_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (player_id, player_message_id)
        )
    """)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS message_links (
            team_message_id BIGINT PRIMARY KEY,
            ticket_id BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE
        )
    """)
    await pool.execute("""
        CREATE INDEX IF NOT EXISTS idx_tickets_player_status
        ON tickets (player_id, status, id DESC)
    """)
    logger.info("PostgreSQL connected; support tickets are stored persistently")


def describe_request(message: Message) -> str:
    if message.text:
        return message.text
    names = {
        "photo": "📷 Фото", "video": "🎬 Видео", "document": "📎 Файл",
        "voice": "🎤 Голосовое сообщение", "audio": "🎵 Аудиозапись",
        "animation": "🎞 GIF", "video_note": "🎥 Видеосообщение",
        "sticker": "🙂 Стикер",
    }
    label = names.get(str(message.content_type), "Сообщение игрока")
    return f"{label}: {message.caption}" if message.caption else label


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def send_cancel_notification(bot: Bot, ticket: asyncpg.Record) -> bool:
    if TEAM_CHAT_ID == 0:
        return False
    notice = (
        f"❌ Игрок отменил обращение №{ticket['id']}!\n"
        f"Категория: {CATEGORIES.get(ticket['category'], ticket['category'])}\n"
        f"ID игрока: {ticket['player_id']}\n"
        f"📝 Обращение: {shorten(ticket['original_text'], 650)}\n\n"
        "⛔ Отвечать на это обращение больше не нужно."
    )
    try:
        if ticket["team_header_id"]:
            await bot.send_message(
                TEAM_CHAT_ID,
                notice,
                reply_parameters=ReplyParameters(
                    message_id=ticket["team_header_id"],
                    allow_sending_without_reply=True,
                ),
            )
        else:
            await bot.send_message(TEAM_CHAT_ID, notice)
        return True
    except TelegramAPIError:
        logger.exception("Could not send cancel notice for ticket %s", ticket["id"])
        return False


async def cancel_for_user(player_id: int, bot: Bot) -> str:
    conn = database()
    draft = await conn.fetchrow(
        "DELETE FROM drafts WHERE player_id = $1 RETURNING category", player_id
    )
    if draft:
        return "❌ Подготовка обращения отменена. Оно ещё не отправлялось разработчикам."

    ticket = await conn.fetchrow("""
        UPDATE tickets SET status = 'cancelled'
        WHERE id = (
            SELECT id FROM tickets
            WHERE player_id = $1 AND status = 'open'
            ORDER BY id DESC LIMIT 1
        ) AND status = 'open'
        RETURNING id, category, player_id, original_text, team_header_id
    """, player_id)
    if ticket is None:
        return "ℹ️ Сейчас нет активных обращений для отмены."

    if await send_cancel_notification(bot, ticket):
        return f"✅ Обращение №{ticket['id']} отменено. Разработчики получили уведомление."
    return (
        f"✅ Обращение №{ticket['id']} отменено, но уведомление разработчикам "
        "не доставлено."
    )


@dp.message(Command("id"))
async def show_chat_id(message: Message) -> None:
    await message.answer(f"ID этого чата: {message.chat.id}")


@dp.message(CommandStart(), F.chat.type == "private")
@dp.message(Command("menu"), F.chat.type == "private")
async def show_menu(message: Message) -> None:
    await database().execute("DELETE FROM drafts WHERE player_id = $1", message.from_user.id)
    await message.answer(
        "✈️ Добро пожаловать в поддержку Attention Please!\n\n"
        "Чем мы можем помочь? Выбери тему обращения:",
        reply_markup=MENU,
    )


@dp.message(Command("cancel"), F.chat.type == "private")
async def cancel_command(message: Message, bot: Bot) -> None:
    result = await cancel_for_user(message.from_user.id, bot)
    await message.answer(result, reply_markup=MENU)


@dp.callback_query(F.data == "cancel_request")
async def cancel_button(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    await callback.answer()
    result = await cancel_for_user(callback.from_user.id, bot)
    await bot.send_message(callback.from_user.id, result, reply_markup=MENU)


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    await database().execute("DELETE FROM drafts WHERE player_id = $1", callback.from_user.id)
    await callback.answer()
    await bot.send_message(
        callback.from_user.id, "Выбери тему обращения:", reply_markup=MENU
    )


@dp.callback_query(F.data.startswith("category:"))
async def select_category(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    category = (callback.data or "").removeprefix("category:")
    if category not in CATEGORIES:
        await callback.answer("Неизвестная категория", show_alert=True)
        return
    await database().execute("""
        INSERT INTO drafts (player_id, category) VALUES ($1, $2)
        ON CONFLICT (player_id) DO UPDATE SET category = EXCLUDED.category
    """, callback.from_user.id, category)
    await callback.answer()
    await bot.send_message(
        callback.from_user.id,
        f"{CATEGORIES[category]}\n\n{PROMPTS[category]}\n\n"
        "Отправь одно сообщение, фото или файл.\nЧтобы отменить — /cancel.",
        reply_markup=CANCEL_MENU,
    )


@dp.message(F.chat.id == TEAM_CHAT_ID)
async def team_reply(message: Message, bot: Bot) -> None:
    """Send a staff reply to the player, quoting their original request."""
    if not message.reply_to_message or (message.from_user and message.from_user.is_bot):
        return
    # The message must reply to our ticket header or copied player message.
    ticket = await database().fetchrow("""
        SELECT t.id, t.player_id, t.original_text, t.status
        FROM message_links AS l
        JOIN tickets AS t ON t.id = l.ticket_id
        WHERE l.team_message_id = $1
    """, message.reply_to_message.message_id)
    if not ticket:
        return
    if ticket["status"] == "cancelled":
        await message.reply(f"⛔ Обращение №{ticket['id']} отменено игроком. Ответ не отправлен.")
        return

    introduction = (
        "📩 Администрация ответила на ваше сообщение!\n\n"
        f"📝 Ваше обращение:\n{shorten(ticket['original_text'], 900)}\n\n"
        "💬 Ответ администрации:\n"
    )
    try:
        if message.text and len(introduction) + len(message.text) <= 4096:
            await bot.send_message(ticket["player_id"], introduction + message.text)
        else:
            await bot.send_message(ticket["player_id"], introduction + "⬇️")
            await message.copy_to(ticket["player_id"])
    except (TelegramAPIError, TypeError):
        logger.exception("Unable to deliver staff reply for ticket %s", ticket["id"])
        await message.reply("❌ Ответ не доставлен. Возможно, игрок заблокировал бота.")
        return

    await database().execute(
        "UPDATE tickets SET status = 'answered' WHERE id = $1 AND status = 'open'",
        ticket["id"],
    )
    await message.reply(f"✅ Ответ отправлен игроку (обращение №{ticket['id']}).")


@dp.message(F.chat.type == "private")
async def player_request(message: Message, bot: Bot) -> None:
    player_id = message.from_user.id
    draft = await database().fetchrow(
        "SELECT category FROM drafts WHERE player_id = $1", player_id
    )
    if draft is None:
        await message.answer("Сначала выбери тему обращения:", reply_markup=MENU)
        return
    if TEAM_CHAT_ID == 0:
        await message.answer("⚙️ Поддержка пока не настроена. Попробуй позже.")
        return

    category = draft["category"]
    if category not in CATEGORIES:
        await message.answer("Выбери тему заново:", reply_markup=MENU)
        return

    original = describe_request(message)
    row = await database().fetchrow("""
        INSERT INTO tickets (player_id, category, original_text, player_message_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (player_id, player_message_id) DO NOTHING
        RETURNING id
    """, player_id, category, original, message.message_id)
    if not row:
        # Telegram can resend a webhook after a timeout. Do not create a duplicate.
        return
    ticket_id = row["id"]
    header = None
    try:
        header = await bot.send_message(
            TEAM_CHAT_ID,
            f"📩 Новое обращение №{ticket_id}\n"
            f"Категория: {CATEGORIES[category]}\n"
            f"Игрок: {shorten(message.from_user.full_name, 120)}\n"
            f"ID игрока: {player_id}\n\n"
            "↩️ Ответьте на этот заголовок или сообщение игрока ниже.",
        )
        copied = await message.copy_to(
            TEAM_CHAT_ID,
            reply_parameters=ReplyParameters(message_id=header.message_id),
        )
    except (TelegramAPIError, TypeError):
        logger.exception("Could not forward player request %s", ticket_id)
        # No active ticket remains if the Telegram delivery failed.
        await database().execute("DELETE FROM tickets WHERE id = $1", ticket_id)
        if header:
            try:
                await bot.delete_message(TEAM_CHAT_ID, header.message_id)
            except TelegramAPIError:
                pass
        await message.answer("❌ Не удалось передать обращение. Попробуй ещё раз.")
        return

    # Persist BOTH links, so replying to either group message works.
    async with database().acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO message_links (team_message_id, ticket_id)
                VALUES ($1, $3), ($2, $3)
                ON CONFLICT (team_message_id) DO NOTHING
            """, header.message_id, copied.message_id, ticket_id)
            await conn.execute(
                "UPDATE tickets SET team_header_id = $1 WHERE id = $2",
                header.message_id, ticket_id,
            )
            await conn.execute(
                "DELETE FROM drafts WHERE player_id = $1", player_id
            )

    await message.answer(
        f"✅ Обращение №{ticket_id} доставлено разработчикам!\n\n"
        "До ответа администрации его можно отменить через /cancel "
        "или кнопкой ниже.",
        reply_markup=MENU,
    )


async def on_startup(bot: Bot) -> None:
    if not BOT_TOKEN or not DATABASE_URL:
        raise RuntimeError("На Render нужны переменные BOT_TOKEN и DATABASE_URL")
    if not BASE_URL.startswith("https://"):
        raise RuntimeError("Нужен PUBLIC_BASE_URL=https://... или RENDER_EXTERNAL_URL")
    await initialize_database()
    # Telegram includes this secret header on legitimate webhook calls.
    secret_token = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()
    await bot.set_webhook(
        f"{BASE_URL}{WEBHOOK_PATH}",
        secret_token=secret_token,
        drop_pending_updates=False,
        allowed_updates=dp.resolve_used_update_types(),
    )
    logger.info("Telegram webhook enabled: %s%s", BASE_URL, WEBHOOK_PATH)


async def on_shutdown(bot: Bot) -> None:
    global pool
    # Do NOT delete the webhook: Telegram must be able to queue updates as
    # Render sleeps and wakes. The handler closes the bot session itself.
    if pool is not None:
        await pool.close()
        pool = None


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "app": "Attention Please Support"})


def create_app() -> web.Application:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")
    bot = Bot(BOT_TOKEN)
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    secret_token = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=secret_token,
        handle_in_background=False,
    ).register(app, path=WEBHOOK_PATH)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    setup_application(app, dp, bot=bot)
    return app


if __name__ == "__main__":
    logger.info("Starting Attention Please webhook server on port %s", PORT)
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
