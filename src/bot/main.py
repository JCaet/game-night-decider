import logging
import os
import sys

from dotenv import load_dotenv

# Load env vars first
load_dotenv()

from telegram.ext import (  # noqa: E402
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    PollAnswerHandler,
)

from src.bot.handlers import (  # noqa: E402
    add_game,
    add_guest,
    cancel_night,
    cancel_night_callback,
    create_poll,
    custom_poll_action_callback,
    custom_poll_vote_callback,
    cycle_vote_limit_callback,
    guest_game,
    help_command,
    join_lobby_callback,
    leave_lobby_callback,
    manage_collection,
    manage_collection_callback,
    poll_settings_callback,
    receive_poll_answer,
    restart_night_callback,
    resume_night_callback,
    set_bgg,
    start,
    start_night,
    start_poll_callback,
    test_mode,
    toggle_hide_voters_callback,
    toggle_poll_mode_callback,
    toggle_weights_callback,
)
from src.core.db import init_db  # noqa: E402

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
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("testmode", test_mode))
    app.add_handler(CommandHandler("addguest", add_guest))
    app.add_handler(CommandHandler("guestgame", guest_game))
    app.add_handler(CommandHandler("manage", manage_collection))
    app.add_handler(CommandHandler("cancel", cancel_night))
    app.add_handler(CallbackQueryHandler(join_lobby_callback, pattern="^join_lobby$"))
    app.add_handler(CallbackQueryHandler(leave_lobby_callback, pattern="^leave_lobby$"))
    app.add_handler(CallbackQueryHandler(resume_night_callback, pattern="^resume_night$"))
    app.add_handler(CallbackQueryHandler(restart_night_callback, pattern="^restart_night$"))
    app.add_handler(CallbackQueryHandler(start_poll_callback, pattern="^start_poll$"))
    app.add_handler(CallbackQueryHandler(cancel_night_callback, pattern="^cancel_night$"))
    app.add_handler(CallbackQueryHandler(toggle_weights_callback, pattern="^toggle_weights$"))
    app.add_handler(CallbackQueryHandler(poll_settings_callback, pattern="^poll_settings$"))
    app.add_handler(CallbackQueryHandler(toggle_poll_mode_callback, pattern="^toggle_poll_mode$"))
    app.add_handler(CallbackQueryHandler(custom_poll_vote_callback, pattern=r"^vote:"))
    app.add_handler(CallbackQueryHandler(custom_poll_action_callback, pattern=r"^poll_refresh:"))
    app.add_handler(CallbackQueryHandler(custom_poll_action_callback, pattern=r"^poll_close:"))
    app.add_handler(
        CallbackQueryHandler(custom_poll_action_callback, pattern=r"^poll_random_vote:")
    )
    app.add_handler(
        CallbackQueryHandler(custom_poll_action_callback, pattern=r"^poll_toggle_voters:")
    )
    app.add_handler(
        CallbackQueryHandler(toggle_hide_voters_callback, pattern=r"^toggle_hide_voters$")
    )
    app.add_handler(CallbackQueryHandler(cycle_vote_limit_callback, pattern=r"^cycle_vote_limit$"))
    app.add_handler(CallbackQueryHandler(manage_collection_callback, pattern="^manage:"))
    app.add_handler(PollAnswerHandler(receive_poll_answer))

    # Init DB on startup
    # python-telegram-bot's Application has post_init
    async def post_init(application):
        await init_db()

    app.post_init = post_init

    logger.info("Bot is polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
