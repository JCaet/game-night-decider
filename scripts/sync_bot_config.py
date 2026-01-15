#!/usr/bin/env python3
"""
Sync bot configuration to Telegram via Bot API.

This script updates bot settings (commands, name, description) without
needing to interact with BotFather. Run it whenever you change the bot's
configuration.

Usage:
    uv run python scripts/sync_bot_config.py

Environment:
    TELEGRAM_BOT_TOKEN: Required. Your bot's API token from BotFather.
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from telegram import Bot, BotCommand

# Load environment variables
load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================================================================
# BOT CONFIGURATION - Edit these values to update your bot's settings
# ============================================================================

# Commands shown in the Telegram menu (max 100 commands, each max 32 chars)
BOT_COMMANDS = [
    BotCommand("start", "Welcome message and command list"),
    BotCommand("help", "Show all available commands"),
    BotCommand("setbgg", "Link your BoardGameGeek account"),
    BotCommand("addgame", "Manually add a game to your collection"),
    BotCommand("manage", "Manage collection (⬜→🌟→❌ cycle)"),
    BotCommand("gamenight", "Start a new game night lobby"),
    BotCommand("cancel", "Cancel the current game night"),
    BotCommand("poll", "Generate polls based on lobby"),
    BotCommand("addguest", "Add a guest player to game night"),
    BotCommand("guestgame", "Add a game for a guest"),
    BotCommand("testmode", "Add fake test users [count]"),
]

# Bot name (max 64 characters, leave None to keep current)
BOT_NAME: str | None = "Game Night Decider"

# Bot description - shown in the chat with the bot when chat is empty (max 512 chars)
# Leave None to keep current
BOT_DESCRIPTION: str | None = (
    "🎲 **Welcome to Game Night Decider!** 🎲\n\n"
    "Struggling to pick a game? I'm here to help your group decide what to play!\n\n"
    "✨ **Features:**\n"
    "• 🔗 **Sync BGG:** Import your collection from BoardGameGeek.\n"
    "• 🗳️ **Smart Voting:** I'll find games that fit your player count and generate a poll.\n"
    "• 👥 **Fair Play:** Filters games based on who is actually playing.\n\n"
    "👇 **Get Started:**\n"
    "Add me to your group chat and use /gamenight to begin!"
)

# Bot short description - shown on the bot's profile page (max 120 chars)
# Leave None to keep current
BOT_SHORT_DESCRIPTION: str | None = (
    "Your board game night companion! 🎲 Syncs BGG collections and helps groups vote on what to play."
)

# ============================================================================


async def sync_config() -> None:
    """Sync the bot configuration to Telegram."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables!")
        sys.exit(1)

    bot = Bot(token)

    async with bot:
        # Get current bot info for logging
        me = await bot.get_me()
        logger.info(f"Syncing configuration for bot: @{me.username} ({me.first_name})")

        # 1. Update commands
        logger.info("Setting bot commands...")
        await bot.set_my_commands(BOT_COMMANDS)
        logger.info(f"  ✓ Set {len(BOT_COMMANDS)} command(s)")

        # 2. Update bot name (if specified)
        if BOT_NAME is not None:
            logger.info("Setting bot name...")
            await bot.set_my_name(BOT_NAME)
            logger.info(f"  ✓ Name set to: {BOT_NAME}")

        # 3. Update bot description (if specified)
        if BOT_DESCRIPTION is not None:
            logger.info("Setting bot description...")
            await bot.set_my_description(BOT_DESCRIPTION)
            logger.info("  ✓ Description updated")

        # 4. Update bot short description (if specified)
        if BOT_SHORT_DESCRIPTION is not None:
            logger.info("Setting bot short description...")
            await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
            logger.info("  ✓ Short description updated")

        logger.info("=" * 50)
        logger.info("✅ Bot configuration synced successfully!")
        logger.info("=" * 50)

        # Show current state
        logger.info("\nCurrent bot configuration:")
        logger.info(f"  Username: @{me.username}")
        logger.info(f"  Name: {me.first_name}")
        commands = await bot.get_my_commands()
        logger.info(f"  Commands: {len(commands)}")
        for cmd in commands:
            logger.info(f"    /{cmd.command} - {cmd.description}")


def main() -> None:
    """Entry point."""
    asyncio.run(sync_config())


if __name__ == "__main__":
    main()
