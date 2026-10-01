import asyncio
import hashlib
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, WebAppInfo

from .database import Database
from .handlers import router
from .payment_web import start_payment_web
from .storage import R2Storage, storage_worker


async def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    database_url = os.environ.get("DATABASE_URL")
    admin_id = os.environ.get("ADMIN_ID")
    channel_id = os.environ.get("CHANNEL_ID")
    payment_url = os.environ.get("PAYMENT_PAGE_URL")
    if not token or not database_url or not admin_id:
        raise RuntimeError("BOT_TOKEN, DATABASE_URL va ADMIN_ID environment variable bo‘lishi shart")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = Database(database_url)
    await db.connect()
    await db.init_schema()

    local_api = (
        os.environ.get("BOT_API_LOCAL", "").strip().lower() in {"1", "true", "yes"}
        and bool(os.environ.get("TELEGRAM_API_ID"))
        and bool(os.environ.get("TELEGRAM_API_HASH"))
    )
    default_props = DefaultBotProperties(parse_mode=ParseMode.HTML)
    if local_api:
        token_fingerprint = hashlib.sha256(token.encode()).hexdigest()[:16]
        previous_fingerprint = await db.get_setting("local_bot_api_token_fingerprint", "")
        if previous_fingerprint != token_fingerprint:
            cloud_bot = Bot(token=token, default=default_props)
            try:
                await cloud_bot.log_out()
                await db.set_setting("local_bot_api_token_fingerprint", token_fingerprint)
                await db.set_setting("local_bot_api_logged_out", "true")
            finally:
                await cloud_bot.session.close()
        api_base = os.environ.get("BOT_API_BASE_URL", "http://127.0.0.1:8081").rstrip("/")
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(api_base, is_local=True)
        )
        bot = Bot(token=token, session=session, default=default_props)
        logging.getLogger(__name__).info("Using Local Telegram Bot API: %s", api_base)
    else:
        bot = Bot(token=token, default=default_props)
    dp = Dispatcher()
    dp["db"] = db
    dp["admin_id"] = int(admin_id)
    dp["channel_id"] = channel_id
    dp["payment_url"] = payment_url
    dp.include_router(router)
    storage = R2Storage()
    dp["storage"] = storage
    web_runner = await start_payment_web(bot, db, int(admin_id), storage)

    public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if public_domain:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="AIKINOUZ PREMIERE",
                    web_app=WebAppInfo(url=f"https://{public_domain}/app"),
                )
            )
        except Exception:
            logging.getLogger(__name__).exception("Telegram menu button update failed")
    storage_stop = asyncio.Event()
    storage_task = asyncio.create_task(storage_worker(db, bot, storage, storage_stop, int(admin_id)))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        storage_stop.set()
        try:
            await storage_task
        except asyncio.CancelledError:
            pass
        await web_runner.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
