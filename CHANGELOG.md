# CHANGELOG

## [Unreleased]

### Added

- **Telegram Bot API 9.1 / 9.6 migration**: Adopted new poll API capabilities across both native and custom poll modes.
- **Max poll options raised to 12**: `split_games()` default and the truncation guard updated from 10 to 12 (API 9.1).
- **`allows_revoting=True`**: Native polls now explicitly enable vote changing.
- **Shuffle Options setting**: Randomizes option order to reduce positional bias. Native polls use the `shuffle_options` API parameter; custom polls randomize button order within each complexity group.
- **Hide Results setting**: Hides all vote counts and voter names until the poll is closed. Native polls use `hide_results_until_closes`; custom polls suppress the leaderboard, vote counts in text, and `(N)` labels on buttons.
- **Allow Suggestions setting**: Lets participants add games to the poll after creation. Native polls use `allow_adding_options`; custom polls show a ➕ Add button that presents a picker of games from the user's collection not already in the poll. Added `PollAddedGame` model to track user-added games.
- **Poll description**: Both native and custom polls display context metadata (player count, game count, active settings like weighted voting). Native polls use the new `description` parameter; custom polls show it as an italic header line.
- New Session model fields: `shuffle_options`, `hide_results`, `allow_adding_options`.
- 9 new tests covering all new settings, hide-results rendering, add-game flow, settings keyboard, and poll description.

### Migration

- **Existing databases** require migration: run `uv run python scripts/migrate_poll_settings.py` or restart the bot (auto-migration in `db.py` handles column additions; `create_all` handles the new `poll_added_games` table).

### Changed

- Bumped `python-telegram-bot` minimum from `>=21.10` to `>=22.5`.
- `toggle_weights_callback` refactored to use shared `_build_settings_keyboard()` and `POLL_SETTINGS_TEXT` instead of duplicating the keyboard inline.
- Poll settings UI now includes Shuffle Options, Hide Results, and Allow Suggestions toggles.
