"""Measure Telegram's undocumented inline-keyboard limits against the live API.

Telegram documents only ``callback_data`` (1-64 bytes). It documents no cap on
the number of buttons, on buttons per row, or on the serialized size of
``reply_markup`` -- yet it enforces all three, which is what repeatedly cost the
custom poll its game buttons. python-telegram-bot's ``InlineKeyboardMarkupLimit``
claims 100 buttons / 8 per row; both are wrong and neither is enforced in the
library. So the numbers have to be measured.

Results as of 2026-10-28 (basic group chat):

    reply_markup   ~10,300-10,400 bytes  -> error "Reply markup is too long"
    total buttons  300                   -> SILENTLY truncated, HTTP 200
    buttons/row    12                    -> SILENTLY truncated, HTTP 200
    message text   4096 chars            -> error "Message_too_long"
    rows           not a constraint (4/row caps at the same 300 buttons)

Two findings matter more than the numbers:

* Over a button limit Telegram returns **success** and quietly stores fewer
  buttons. There is no error to retry on, so a sender that trusts the response
  will claim to show more than it does.
* The byte ceiling is **not crisp**. The same byte count was accepted in one run
  and rejected in another with a different label-length distribution, so a byte
  count is only a proxy for whatever the server measures. Keep headroom.

Method: one anchor message, edited repeatedly. Edits are far more rate-tolerant
than sends (sending ~40 messages to a group trips flood control at ~20/min and
the errors then drown out the real verdicts), and the poll's own failure happens
on ``edit_message_text``, so this measures the exact call that breaks it.

Usage:
    TELEGRAM_BOT_TOKEN=... uv run python scripts/probe_telegram_keyboard_limits.py <chat_id>

Use a throwaway chat. Group ids are negative; a positive id is a private chat,
and the bot must have been started by that user. A bot can never message a bot.
"""

import asyncio
import json
import os
import re
import sys

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import TelegramError

FLOOD = re.compile(r"retry in (\d+)", re.I)
PAUSE = 1.5


def markup_bytes(markup: InlineKeyboardMarkup) -> int:
    """Serialized size the way the sender would count it: compact JSON, UTF-8."""
    return len(
        json.dumps(markup.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def build(
    n_buttons: int, per_row: int = 1, label_len: int = 10, pad_last: int = 0
) -> InlineKeyboardMarkup:
    """n_buttons buttons of label_len chars, per_row to a row.

    pad_last lengthens only the final label, which gives 1-byte granularity for
    binary-searching the byte ceiling without changing the button count.
    """
    buttons = []
    for i in range(n_buttons):
        label = f"{i}-{'x' * max(0, label_len - len(str(i)) - 1)}"[:label_len] or "x"
        if pad_last and i == n_buttons - 1:
            label += "y" * pad_last
        buttons.append(InlineKeyboardButton(label, callback_data=f"p:{i}"))
    return InlineKeyboardMarkup([buttons[i : i + per_row] for i in range(0, len(buttons), per_row)])


def sized(target_bytes: int, n_buttons: int = 100) -> InlineKeyboardMarkup:
    """A keyboard serializing to exactly target_bytes."""
    base = build(n_buttons, 1, 60)
    return build(n_buttons, 1, 60, pad_last=target_bytes - markup_bytes(base))


class Unreachable(Exception):
    """The bot cannot post in the target chat, so nothing can be measured."""


async def probe(bot: Bot, chat_id: int, mid: int, markup, text: str = "probe"):
    """Edit the anchor to this keyboard. Returns (outcome, detail).

    outcome is ok | truncated | rejected. Flood control is waited out rather
    than reported, since a rate limit says nothing about the keyboard.
    """
    for _ in range(6):
        try:
            msg = await bot.edit_message_text(
                chat_id=chat_id, message_id=mid, text=text, reply_markup=markup
            )
        except TelegramError as e:
            detail = str(e)
            lowered = detail.lower()
            if (m := FLOOD.search(detail)) is not None:
                await asyncio.sleep(int(m.group(1)) + 2)
                continue
            if "not modified" in lowered:
                return "ok", "not modified"
            if any(s in lowered for s in ("chat not found", "forbidden", "blocked", "kicked")):
                raise Unreachable(detail) from e
            return "rejected", detail
        sent = sum(len(r) for r in markup.inline_keyboard)
        if not isinstance(msg, Message):  # True for inline messages: nothing to read back
            return "ok", "accepted (no Message returned, truncation undetectable)"
        rows = msg.reply_markup.inline_keyboard if msg.reply_markup else ()
        kept = sum(len(r) for r in rows)
        if kept != sent:
            widest = max((len(r) for r in rows), default=0)
            return "truncated", f"sent {sent}, kept {kept} (widest row kept: {widest})"
        return "ok", f"kept all {kept}"
    return "rejected", "gave up retrying through flood control"


async def find_max(bot, chat_id, mid, title: str, make, lo: int, hi: int) -> int | None:
    """Binary-search the largest value that is neither rejected nor truncated."""
    print(f"\n### {title}")
    best = None
    while lo <= hi:
        mid_val = (lo + hi) // 2
        markup = make(mid_val)
        outcome, detail = await probe(bot, chat_id, mid, markup, f"probe {mid_val}")
        print(f"  {mid_val:>6} ({markup_bytes(markup):>6} B) -> {outcome}: {detail}")
        if outcome == "ok":
            best, lo = mid_val, mid_val + 1
        else:
            hi = mid_val - 1
        await asyncio.sleep(PAUSE)
    print(f"  => largest accepted: {best}")
    return best


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or len(sys.argv) < 2:
        sys.exit(f"usage: TELEGRAM_BOT_TOKEN=... python {sys.argv[0]} <chat_id>")
    chat_id = int(sys.argv[1])

    bot = Bot(token)
    async with bot:
        me = await bot.get_me()
        print(f"probing as @{me.username} in chat {chat_id}")
        anchor = await bot.send_message(chat_id=chat_id, text="probe anchor")
        mid = anchor.message_id

        try:
            await find_max(
                bot, chat_id, mid, "total buttons, 1 per row", lambda n: build(n, 1), 1, 400
            )
            # Same budget 4 to a row: if this reaches the same ceiling the cap is
            # on buttons; if it goes much further, the cap is on rows.
            await find_max(
                bot,
                chat_id,
                mid,
                "total buttons, 4 per row (rows vs buttons)",
                lambda n: build(n, 4),
                1,
                400,
            )
            await find_max(
                bot, chat_id, mid, "buttons per row", lambda n: build(n, n), 1, 20
            )
            await find_max(
                bot,
                chat_id,
                mid,
                "reply_markup bytes (100 fixed buttons, growing labels)",
                sized,
                9000,
                12000,
            )

            print("\n### message text ceiling (small keyboard)")
            small = build(2, 2)
            for n in (4000, 4096, 4097, 5000):
                outcome, detail = await probe(bot, chat_id, mid, small, "t" * n)
                print(f"  text={n:>5} -> {outcome}: {detail}")
                await asyncio.sleep(PAUSE)
        except Unreachable as exc:
            print(f"\naborted: the bot cannot post in chat {chat_id} ({exc})")
        finally:
            with_suppress = bot.delete_message(chat_id=chat_id, message_id=mid)
            try:
                await with_suppress
            except TelegramError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
