import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .database import Database
from .handlers import router
from .payment_web import start_payment_web


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

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp["db"] = db
    dp["admin_id"] = int(admin_id)
    dp["channel_id"] = channel_id
    dp["payment_url"] = payment_url
    dp.include_router(router)
    web_runner = await start_payment_web(bot, db, int(admin_id))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        await web_runner.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
