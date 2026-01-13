import logging
import os
import sys

from dotenv import load_dotenv

# Load env vars first
load_dotenv()

from src.core.db import init_db
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler

from src.bot.handlers import (
    add_game,
    add_guest,
    cancel_night_callback,
    create_poll,
    exclude_game,
    guest_game,
    join_lobby_callback,
    leave_lobby_callback,
    mark_played,
    priority_game,
    priority_select_callback,
    restart_night_callback,
    resume_night_callback,
    set_bgg,
    start,
    start_night,
    start_poll_callback,
    test_mode,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set!")
        sys.exit(1)

    app = ApplicationBuilder().token(token).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setbgg", set_bgg))
    app.add_handler(CommandHandler("addgame", add_game))
    app.add_handler(CommandHandler("gamenight", start_night))
    app.add_handler(CommandHandler("poll", create_poll))
    app.add_handler(CommandHandler("markplayed", mark_played))
    app.add_handler(CommandHandler("exclude", exclude_game))
    app.add_handler(CommandHandler("priority", priority_game))
    app.add_handler(CommandHandler("testmode", test_mode))
    app.add_handler(CommandHandler("addguest", add_guest))
    app.add_handler(CommandHandler("guestgame", guest_game))
    app.add_handler(CallbackQueryHandler(join_lobby_callback, pattern="^join_lobby$"))
    app.add_handler(CallbackQueryHandler(leave_lobby_callback, pattern="^leave_lobby$"))
    app.add_handler(CallbackQueryHandler(resume_night_callback, pattern="^resume_night$"))
    app.add_handler(CallbackQueryHandler(restart_night_callback, pattern="^restart_night$"))
    app.add_handler(CallbackQueryHandler(start_poll_callback, pattern="^start_poll$"))
    app.add_handler(CallbackQueryHandler(cancel_night_callback, pattern="^cancel_night$"))
    app.add_handler(CallbackQueryHandler(priority_select_callback, pattern="^prio:"))

    # Init DB on startup
    # python-telegram-bot's Application has post_init
    async def post_init(application):
        await init_db()

    app.post_init = post_init

    logger.info("Bot is polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
