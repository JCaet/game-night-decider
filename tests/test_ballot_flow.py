"""Voting through ballots: the widget in each player's private chat with the bot."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from telegram.constants import ChatType
from telegram.error import BadRequest

from src.bot import handlers
from src.bot.handlers import (
    cancel_night,
    create_poll,
    custom_poll_action_callback,
    prompt_reply,
    render_poll_message,
    start,
)
from src.core import db
from src.core.models import (
    Collection,
    Game,
    GameNightPoll,
    GameState,
    PollBallot,
    PollVote,
    Session,
    SessionPlayer,
    User,
    VoteType,
)

CHAT_ID = 12345
POLL_ID = "poll_12345_1"
ALICE, BOB = 111, 222


async def _seed(complexities=(3.9, 3.7, 1.8), players=(ALICE, BOB), **session_kwargs):
    """A running poll: games 1..n at the given complexities, owned by every player."""
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=CHAT_ID, is_active=True, **session_kwargs))
        session.add_all(User(telegram_id=p, telegram_name=f"User{p}") for p in players)
        await session.flush()
        games = [
            Game(id=i, name=f"Game{i}", min_players=1, max_players=6, playing_time=60, complexity=c)
            for i, c in enumerate(complexities, start=1)
        ]
        session.add_all(games)
        await session.flush()
        for p in players:
            session.add_all(Collection(user_id=p, game_id=g.id) for g in games)
            session.add(SessionPlayer(session_id=CHAT_ID, user_id=p))
        session.add(
            GameNightPoll(
                poll_id=POLL_ID, chat_id=CHAT_ID, message_id=999, shuffle_seed=7, chat_title="Crew"
            )
        )
        await session.commit()


async def _votes(user_id=None):
    async with db.AsyncSessionLocal() as session:
        stmt = select(PollVote).where(PollVote.poll_id == POLL_ID)
        if user_id is not None:
            stmt = stmt.where(PollVote.user_id == user_id)
        return (await session.execute(stmt)).scalars().all()


async def _ballot_rows():
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(select(PollBallot))).scalars().all()


async def _render(context):
    async with db.AsyncSessionLocal() as session:
        games, priority_ids = await handlers.get_session_valid_games(session, CHAT_ID)
        await render_poll_message(context.bot, CHAT_ID, 999, session, POLL_ID, games, priority_ids)


# ============================================================================
# Voting
# ============================================================================


@pytest.mark.asyncio
async def test_tap_records_a_vote_and_answers_with_fresh_state(mock_context, ballot):
    await _seed()

    state = await ballot.tap(POLL_ID, ALICE, game_id=2)

    votes = await _votes()
    assert [(v.user_id, v.game_id, v.vote_type) for v in votes] == [(ALICE, 2, VoteType.GAME)]
    assert votes[0].user_name == "Voter"
    # Ballot order is heaviest level first, by name: Game1, Game2, then Game3.
    assert state["m"] == [1]
    assert state["t"] == "010"
    assert "e" not in state and "p" not in state


@pytest.mark.asyncio
async def test_tap_updates_the_group_tally_and_stores_the_ballot_state(mock_context, ballot):
    await _seed()

    await ballot.tap(POLL_ID, ALICE, game_id=2)

    # No job queue in tests, so the render runs inline.
    text = mock_context.bot.edit_message_text.call_args.kwargs["text"]
    assert "**1** - Game2" in text
    # An answer is not stored by Telegram, so the state is also written as
    # data: once straight away, and again by the render for every ballot.
    stored = ballot.api_calls("editMessageWidgetData")
    assert len(stored) == 2
    for call in stored:
        assert call["chat_id"] == ALICE and call["message_id"] == 5000 + ALICE
        assert '"m":[1]' in call["chat_data"]


@pytest.mark.asyncio
async def test_second_tap_removes_the_vote(mock_context, ballot):
    await _seed()

    await ballot.tap(POLL_ID, ALICE, game_id=2)
    state = await ballot.tap(POLL_ID, ALICE, game_id=2)

    assert await _votes() == []
    assert state["m"] == [] and state["t"] == "000"


@pytest.mark.asyncio
async def test_a_voter_can_pick_several_games(mock_context, ballot):
    await _seed()

    await ballot.tap(POLL_ID, ALICE, game_id=1)
    state = await ballot.tap(POLL_ID, ALICE, game_id=3)

    assert sorted(v.game_id for v in await _votes()) == [1, 3]
    assert state["m"] == [0, 2]


@pytest.mark.asyncio
async def test_category_vote_toggles(mock_context, ballot):
    await _seed()

    state = await ballot.tap(POLL_ID, ALICE, level=3)

    (vote,) = await _votes()
    assert (vote.vote_type, vote.category_level, vote.game_id) == (VoteType.CATEGORY, 3, None)
    # Levels on the ballot are 3 and 1; the tally string follows that order.
    assert state["c"] == [3] and state["k"] == "10"

    state = await ballot.tap(POLL_ID, ALICE, level=3)

    assert await _votes() == []
    assert state["c"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["v:99", "v:-1", "c:5", "x:1", "v:abc", ""])
async def test_request_outside_the_ballot_is_refused(mock_context, ballot, data):
    await _seed()
    _, message_id = await ballot.open(POLL_ID, ALICE)

    state = await ballot.send(ALICE, message_id, data)

    assert state["e"] == "Invalid vote."
    assert await _votes() == []


@pytest.mark.asyncio
async def test_tallies_are_withheld_when_results_are_hidden(mock_context, ballot):
    await _seed(hide_results=True)

    state = await ballot.tap(POLL_ID, ALICE, game_id=1)

    assert state["m"] == [0]
    assert "t" not in state and "k" not in state


@pytest.mark.asyncio
async def test_vote_is_debounced_when_a_job_queue_exists(mock_context, ballot):
    await _seed()
    mock_context.job_queue = MagicMock()
    mock_context.job_queue.get_jobs_by_name.return_value = ()

    await ballot.tap(POLL_ID, ALICE, game_id=1)

    mock_context.job_queue.run_once.assert_called_once()
    assert mock_context.job_queue.run_once.call_args.kwargs["name"] == f"render:{POLL_ID}"
    mock_context.bot.edit_message_text.assert_not_called()


# ============================================================================
# Ballots that no longer match the poll
# ============================================================================


@pytest.mark.asyncio
async def test_stale_ballot_records_nothing_and_is_replaced(mock_context, ballot):
    """Positions on an old ballot may point at different games now."""
    await _seed()
    _, message_id = await ballot.open(POLL_ID, ALICE)
    async with db.AsyncSessionLocal() as session:
        session.add(
            Game(
                id=9, name="Aaa New", min_players=1, max_players=6, playing_time=60, complexity=3.5
            )
        )
        session.add(Collection(user_id=BOB, game_id=9))
        await session.commit()

    state = await ballot.send(ALICE, message_id, "v:0")

    assert await _votes() == []
    assert "game list changed" in state["x"]
    (sent,) = ballot.api_calls("sendWidget")
    assert sent["chat_id"] == ALICE and "Aaa New" in sent["html"]
    mock_context.bot.delete_message.assert_called_once_with(chat_id=ALICE, message_id=message_id)
    (row,) = await _ballot_rows()
    assert row.message_id == 7000

    # The replacement is current, so the same tap now counts — for the new game.
    await ballot.send(ALICE, 7000, "v:0")
    assert [v.game_id for v in await _votes()] == [9]


@pytest.mark.asyncio
async def test_tap_on_a_ballot_of_a_finished_poll_stamps_it_closed(mock_context, ballot):
    await _seed()

    state = await ballot.send(ALICE, 4242, "v:0")

    assert state["x"] == "This poll is closed."
    (stored,) = ballot.api_calls("editMessageWidgetData")
    assert stored["message_id"] == 4242 and "This poll is closed." in stored["chat_data"]
    assert await _votes() == []


@pytest.mark.asyncio
async def test_player_who_left_cannot_vote_and_is_told(mock_context, ballot):
    await _seed(players=(ALICE, BOB, 333))
    await ballot.tap(POLL_ID, ALICE, game_id=1)
    async with db.AsyncSessionLocal() as session:
        row = await session.scalar(select(SessionPlayer).where(SessionPlayer.user_id == ALICE))
        await session.delete(row)
        await session.commit()

    state = await ballot.tap(POLL_ID, ALICE, game_id=2)

    assert state["p"] == 1
    # Their earlier vote is kept for a rejoin, shown to them, but not counted.
    assert [v.game_id for v in await _votes(ALICE)] == [1]
    assert state["m"] == [0] and state["t"] == "000"


# ============================================================================
# Opening a ballot
# ============================================================================


def _deep_link(mock_update, mock_context, user_id=ALICE, poll_id=POLL_ID):
    mock_update.effective_user.id = user_id
    mock_update.effective_chat.type = ChatType.PRIVATE
    mock_context.args = [f"b_{poll_id}"]


@pytest.mark.asyncio
async def test_start_deep_link_sends_a_ballot(mock_update, mock_context, ballot):
    await _seed()
    await ballot.tap(POLL_ID, BOB, game_id=3)
    mock_context.bot.do_api_request.reset_mock()
    _deep_link(mock_update, mock_context)

    await start(mock_update, mock_context)

    (sent,) = ballot.api_calls("sendWidget")
    assert sent["chat_id"] == ALICE and sent["allow_queries"] is True
    assert '"n":"Crew"' in sent["html"] and "Game2" in sent["html"]
    # Opens already showing the tally: one vote on the third game.
    assert '"t":"001"' in sent["chat_data"]
    rows = {r.user_id: r for r in await _ballot_rows()}
    assert rows[ALICE].message_id == 7000
    mock_update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_tapping_vote_again_keeps_the_ballot_already_there(mock_update, mock_context, ballot):
    """Deleting and resending a ballot the voter is looking at reads as a glitch."""
    await _seed()
    _deep_link(mock_update, mock_context)
    mock_update.message.delete = AsyncMock()

    await start(mock_update, mock_context)
    await start(mock_update, mock_context)

    assert len(ballot.api_calls("sendWidget")) == 1
    (refreshed,) = ballot.api_calls("editMessageWidgetData")
    assert refreshed["message_id"] == 7000
    mock_context.bot.delete_message.assert_not_called()
    (row,) = await _ballot_rows()
    assert row.message_id == 7000
    # The "/start b_..." message the deep link produced is cleared away each time.
    assert mock_update.message.delete.await_count == 2


@pytest.mark.asyncio
async def test_tapping_vote_again_resends_a_ballot_the_voter_deleted(
    mock_update, mock_context, ballot
):
    await _seed()
    _deep_link(mock_update, mock_context)
    await start(mock_update, mock_context)
    sent = iter([{"message_id": 7001}])

    async def message_is_gone(endpoint, api_kwargs=None, **_):
        if endpoint == "editMessageWidgetData":
            raise BadRequest("Message to edit not found")
        return next(sent)

    mock_context.bot.do_api_request.side_effect = message_is_gone

    await start(mock_update, mock_context)

    (row,) = await _ballot_rows()
    assert row.message_id == 7001


@pytest.mark.asyncio
async def test_tapping_vote_again_replaces_an_out_of_date_ballot(mock_update, mock_context, ballot):
    await _seed()
    _deep_link(mock_update, mock_context)
    await start(mock_update, mock_context)
    async with db.AsyncSessionLocal() as session:
        row = await session.scalar(select(PollBallot))
        row.catalog_hash = "built-from-an-older-list"
        await session.commit()

    await start(mock_update, mock_context)

    assert len(ballot.api_calls("sendWidget")) == 2
    mock_context.bot.delete_message.assert_called_once_with(chat_id=ALICE, message_id=7000)
    (row,) = await _ballot_rows()
    assert row.message_id == 7001


@pytest.mark.asyncio
async def test_non_participant_is_told_to_join_instead(mock_update, mock_context, ballot):
    await _seed()
    _deep_link(mock_update, mock_context, user_id=999)

    await start(mock_update, mock_context)

    assert "not in this game night" in mock_update.message.reply_text.call_args.args[0]
    assert ballot.api_calls("sendWidget") == []
    assert await _ballot_rows() == []


@pytest.mark.asyncio
async def test_deep_link_to_a_finished_poll_says_so(mock_update, mock_context, ballot):
    await _seed()
    _deep_link(mock_update, mock_context, poll_id="poll_gone")

    await start(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once_with("That poll is closed.")
    assert ballot.api_calls("sendWidget") == []


@pytest.mark.asyncio
async def test_shuffled_poll_gives_every_ballot_the_same_order(mock_context, ballot):
    await _seed(complexities=[2.5] * 12, shuffle_options=True)

    first, _ = await ballot.open(POLL_ID, ALICE)
    second, _ = await ballot.open(POLL_ID, BOB)

    assert first.game_ids == second.game_ids
    assert first.game_ids != tuple(range(1, 13))
    assert sorted(first.game_ids) == list(range(1, 13))


# ============================================================================
# Keeping ballots in step with the poll
# ============================================================================


@pytest.mark.asyncio
async def test_render_pushes_state_to_current_ballots_and_replaces_stale_ones(mock_context, ballot):
    await _seed()
    await ballot.open(POLL_ID, ALICE)
    await ballot.open(POLL_ID, BOB)
    async with db.AsyncSessionLocal() as session:
        row = await session.scalar(select(PollBallot).where(PollBallot.user_id == BOB))
        row.catalog_hash = "built-from-an-older-list"
        await session.commit()

    await _render(mock_context)

    (pushed,) = ballot.api_calls("editMessageWidgetData")
    assert pushed["chat_id"] == ALICE
    (resent,) = ballot.api_calls("sendWidget")
    assert resent["chat_id"] == BOB and resent["disable_notification"] is True
    mock_context.bot.delete_message.assert_called_once_with(chat_id=BOB, message_id=5000 + BOB)
    rows = {r.user_id: r for r in await _ballot_rows()}
    assert rows[BOB].message_id == 7000
    assert rows[BOB].catalog_hash == rows[ALICE].catalog_hash


@pytest.mark.asyncio
async def test_starring_a_game_replaces_ballots(mock_context, ballot):
    """Starred games lead their level, so a star changes the ballot's order."""
    await _seed()
    await ballot.open(POLL_ID, ALICE)
    async with db.AsyncSessionLocal() as session:
        col = await session.scalar(
            select(Collection).where(Collection.user_id == BOB, Collection.game_id == 2)
        )
        col.state = GameState.STARRED
        await session.commit()

    await _render(mock_context)

    (resent,) = ballot.api_calls("sendWidget")
    assert resent["html"].index("Game2") < resent["html"].index("Game1")


@pytest.mark.asyncio
async def test_one_unreachable_voter_does_not_block_the_rest(mock_context, ballot):
    await _seed()
    await ballot.open(POLL_ID, ALICE)
    await ballot.open(POLL_ID, BOB)
    delivered = []

    async def flaky(endpoint, api_kwargs=None, **_):
        if api_kwargs["chat_id"] == ALICE:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        delivered.append(api_kwargs["chat_id"])
        return True

    mock_context.bot.do_api_request.side_effect = flaky

    await _render(mock_context)

    assert delivered == [BOB]


@pytest.mark.asyncio
async def test_collection_change_refreshes_the_users_polls(mock_update, mock_context):
    await _seed()
    mock_update.effective_chat.id = ALICE  # e.g. /manage in the private chat
    mock_update.effective_user.id = ALICE

    await handlers._refresh_affected_polls(mock_update, mock_context)

    assert mock_context.bot.edit_message_text.call_args.kwargs["message_id"] == 999


def test_collection_handlers_carry_the_refresh_hook():
    for handler in (
        handlers.set_bgg,
        handlers.add_game,
        handlers.guest_game,
        handlers.manage_collection_callback,
    ):
        assert hasattr(handler, "__wrapped__"), handler.__name__


# ============================================================================
# Closing
# ============================================================================


@pytest.mark.asyncio
async def test_closing_the_poll_retires_its_ballots(mock_update, mock_context, ballot):
    await _seed()
    await ballot.tap(POLL_ID, ALICE, game_id=2)
    await ballot.open(POLL_ID, BOB)
    mock_context.bot.do_api_request.reset_mock()
    mock_update.callback_query.data = f"poll_close:{POLL_ID}"
    mock_update.callback_query.message.chat.id = CHAT_ID

    await custom_poll_action_callback(mock_update, mock_context)

    retired = ballot.api_calls("editMessageWidgetData")
    assert {call["chat_id"] for call in retired} == {ALICE, BOB}
    assert all("Poll closed. Winner: Game2" in call["chat_data"] for call in retired)
    assert await _ballot_rows() == []


@pytest.mark.asyncio
async def test_cancel_command_closes_the_running_poll(mock_update, mock_context, ballot):
    await _seed()
    await ballot.open(POLL_ID, ALICE)
    mock_update.effective_chat.id = CHAT_ID

    await cancel_night(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        assert await session.get(GameNightPoll, POLL_ID) is None
    (retired,) = ballot.api_calls("editMessageWidgetData")
    assert "Game Night Cancelled" in retired["chat_data"]
    assert await _ballot_rows() == []


@pytest.mark.asyncio
async def test_poll_command_replaces_a_running_poll(mock_update, mock_context, ballot):
    await _seed()
    await ballot.open(POLL_ID, ALICE)
    mock_update.effective_chat.id = CHAT_ID

    await create_poll(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        polls = (await session.execute(select(GameNightPoll))).scalars().all()
    assert len(polls) == 1 and polls[0].poll_id != POLL_ID
    assert await _ballot_rows() == []


# ============================================================================
# The poll message
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_adding", [False, True])
async def test_poll_message_links_to_the_ballot(mock_context, allow_adding):
    await _seed(allow_adding_options=allow_adding)

    await _render(mock_context)

    keyboard = mock_context.bot.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert keyboard[0][0].url == f"https://t.me/test_bot?start=b_{POLL_ID}"
    actions = [[button.callback_data.split(":")[0] for button in row] for row in keyboard[1:]]
    assert actions == [
        ["poll_guest", "poll_mygame"],
        (["poll_add"] if allow_adding else []) + ["poll_close"],
    ]


@pytest.mark.asyncio
async def test_tally_lists_most_voted_first_whatever_the_complexity(mock_context, ballot):
    await _seed()
    await ballot.tap(POLL_ID, ALICE, game_id=3)  # the lightest game
    await ballot.tap(POLL_ID, BOB, game_id=3)
    await ballot.tap(POLL_ID, ALICE, game_id=1)  # the heaviest
    await ballot.tap(POLL_ID, BOB, level=3)

    text = mock_context.bot.edit_message_text.call_args.kwargs["text"]

    assert text.index("**2** - Game3") < text.index("**1** - Game1")
    # A category vote says what it is a vote for.
    assert "**1** - 🎲 Random complexity 3 game" in text
    assert "Category" not in text


# ============================================================================
# Buttons that need a name: the bot prompts, the player replies
# ============================================================================


async def _tap_button(mock_update, mock_context, action):
    """Tap a poll button as Alice and return the prompt it posted."""
    mock_update.callback_query.data = f"{action}:{POLL_ID}"
    mock_update.callback_query.message.chat.id = CHAT_ID
    mock_update.callback_query.from_user.mention_html = MagicMock(return_value="<a>Alice</a>")
    await custom_poll_action_callback(mock_update, mock_context)
    prompt: dict = mock_context.bot.send_message.call_args.kwargs
    return prompt


async def _reply(mock_update, mock_context, prompt_text, answer):
    """Reply to a bot message the way a player answering a prompt does."""
    mock_context.bot.id = 4242
    mock_update.effective_chat.id = CHAT_ID
    mock_update.effective_user.id = ALICE
    mock_update.effective_user.mention_html = MagicMock(return_value="<a>Alice</a>")
    mock_update.message.reply_to_message.from_user.id = 4242
    # Telegram hands back the prompt as plain text, the mention's markup gone.
    mock_update.message.reply_to_message.text = prompt_text.replace("<a>", "").replace("</a>", "")
    mock_update.message.reply_to_message.delete = AsyncMock()
    mock_update.message.text = answer
    await prompt_reply(mock_update, mock_context)


async def _guests():
    async with db.AsyncSessionLocal() as session:
        return (await session.execute(select(User).where(User.is_guest))).scalars().all()


async def _collection_names(user_id):
    async with db.AsyncSessionLocal() as session:
        stmt = select(Game.name).join(Collection).where(Collection.user_id == user_id)
        return sorted((await session.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_guest_button_asks_for_a_name(mock_update, mock_context):
    await _seed()

    prompt = await _tap_button(mock_update, mock_context, "poll_guest")

    assert prompt["chat_id"] == CHAT_ID
    assert prompt["text"] == "<a>Alice</a>, reply to this message with the guest's name."
    # Selective: only the mentioned player gets the reply box opened.
    assert prompt["reply_markup"].selective is True


@pytest.mark.asyncio
async def test_guest_name_reply_seats_the_guest_and_asks_for_their_game(mock_update, mock_context):
    await _seed()
    prompt = await _tap_button(mock_update, mock_context, "poll_guest")

    await _reply(mock_update, mock_context, prompt["text"], "  Carol ")

    assert [g.telegram_name for g in await _guests()] == ["Carol"]
    # Three players now, and the poll message says so.
    assert "3 players" in mock_context.bot.edit_message_text.call_args.kwargs["text"]
    mock_update.message.reply_to_message.delete.assert_awaited_once()
    follow_up = mock_context.bot.send_message.call_args.kwargs["text"]
    assert follow_up == "<a>Alice</a>, reply to this message with a game for the guest: Carol"


@pytest.mark.asyncio
async def test_guest_game_reply_adds_the_game_and_asks_for_another(mock_update, mock_context):
    await _seed()
    prompt = await _tap_button(mock_update, mock_context, "poll_guest")
    await _reply(mock_update, mock_context, prompt["text"], "Carol Ann")
    game_prompt = mock_context.bot.send_message.call_args.kwargs["text"]
    mock_context.bot.send_message.reset_mock()

    await _reply(mock_update, mock_context, game_prompt, "Twilight Struggle")

    (guest,) = await _guests()
    assert await _collection_names(guest.telegram_id) == ["Twilight Struggle"]
    assert "Carol Ann's collection" in mock_update.message.reply_text.call_args.args[0]
    assert mock_context.bot.send_message.call_args.kwargs["text"] == game_prompt


@pytest.mark.asyncio
async def test_guest_name_is_escaped_in_the_follow_up_prompt(mock_update, mock_context):
    await _seed()
    prompt = await _tap_button(mock_update, mock_context, "poll_guest")

    await _reply(mock_update, mock_context, prompt["text"], "<b>Dan")

    assert mock_context.bot.send_message.call_args.kwargs["text"].endswith("guest: &lt;b&gt;Dan")


@pytest.mark.asyncio
async def test_add_my_game_button_adds_to_the_repliers_collection(mock_update, mock_context):
    await _seed()
    prompt = await _tap_button(mock_update, mock_context, "poll_mygame")
    assert prompt["text"].endswith("reply to this message with a game to add to your collection.")

    # Name plus numbers is /addgame's manual form, which needs no BGG lookup.
    await _reply(mock_update, mock_context, prompt["text"], "Homebrew 2 6 3.2")

    assert "Homebrew" in await _collection_names(ALICE)
    # The poll picks the new game up without anyone refreshing it.
    assert "4 games" in mock_context.bot.edit_message_text.call_args.kwargs["text"]
    mock_update.message.reply_to_message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_reply_to_any_other_bot_message_does_nothing(mock_update, mock_context):
    await _seed()

    await _reply(mock_update, mock_context, "🎲 Game Night Started!", "Carol")

    assert await _guests() == []
    mock_update.message.reply_to_message.delete.assert_not_awaited()
    mock_context.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_reply_to_someone_elses_message_does_nothing(mock_update, mock_context):
    await _seed()
    prompt = await _tap_button(mock_update, mock_context, "poll_guest")
    mock_context.bot.send_message.reset_mock()
    mock_context.bot.id = 4242
    mock_update.effective_chat.id = CHAT_ID
    mock_update.message.reply_to_message.from_user.id = BOB
    mock_update.message.reply_to_message.text = prompt["text"]
    mock_update.message.text = "Carol"

    await prompt_reply(mock_update, mock_context)

    assert await _guests() == []


@pytest.mark.asyncio
async def test_manually_added_game_links_to_a_bgg_search(mock_context, ballot):
    """Manual games have made-up negative ids; only real BGG ids make a link."""
    await _seed()
    async with db.AsyncSessionLocal() as session:
        session.add(
            Game(
                id=-77, name="Homebrew", min_players=1, max_players=6, playing_time=60, complexity=2
            )
        )
        session.add(Collection(user_id=ALICE, game_id=-77))
        await session.commit()

    built, _ = await ballot.open(POLL_ID, ALICE)

    assert -77 in built.game_ids
    assert '"Homebrew"' in built.html and "-77" not in built.html
