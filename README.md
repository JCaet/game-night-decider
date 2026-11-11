# Game Night Decider 🎲

A Telegram bot that helps your group decide what board game to play on game night.

## Features

- **BGG Integration**: Syncs your collection from BoardGameGeek (with force-refresh option)
- **Lobby System**: Create a lobby for players to join, with resume/restart if a night is already active
- **Smart Filtering**: Only shows games that support the current player count
- **Collection Management**: Three-state toggle per game (⬜ Included → 🌟 Starred → ❌ Excluded) plus custom max player overrides
- **Single Poll**: One pinned message with the live tally for every eligible game
- **Private Ballots**: Tap 🗳 Vote to get your own ballot (a Telegram HTML widget) in a private chat with the bot — games grouped by BGG complexity, starred first, paged per voter, each with a link to its BGG page, updated live
- **Poll Shortcuts**: 👤 Guest seats a guest and then asks for their games; 📚 Add my game adds a game to your own collection — both by replying to a prompt instead of typing a command
- **Live Refresh**: Joining, leaving, or changing a collection mid-poll updates the tally and every open ballot
- **Weighted Voting**: Starred (🌟) games get a +0.5 vote boost per player who starred them
- **Category Voting**: Vote for a complexity level instead of a specific game — resolved to a random game at close
- **Vote Limits**: Auto (scales with game count), fixed (3/5/7/10), or unlimited
- **Anonymous Voting**: Hide voter names in poll results
- **Shuffle Options**: Randomize option order to reduce positional bias
- **Hide Results**: Hide all vote counts and voter names until the poll is closed
- **Allow Suggestions**: Let players add games from their collection to the poll mid-vote
- **Poll Description**: Context metadata (player count, game count, active settings) shown on the poll
- **Poll Pinning**: Poll messages are automatically pinned for visibility and unpinned when closed (requires bot to have "Pin Messages" admin permission)
- **Guest Support**: Add guest players and assign games to them

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Show all available commands |
| `/setbgg <username> [force]` | Sync your BoardGameGeek collection (`force` re-fetches existing games) |
| `/addgame <name>` | Search BGG and add a game to your collection |
| `/manage` | Manage your collection: cycle game states and set custom max players (sent via DM in groups) |
| `/gamenight` | Start a new game night lobby (or resume/restart an existing one) |
| `/poll` | Generate voting polls from joined players' collections |
| `/addguest <name>` | Add a guest player to the current lobby |
| `/guestgame <name> <game>` | Add a game to a guest's collection |
| `/cancel` | Cancel the current game night |

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))

### Local Development

```bash
# Clone the repository
git clone https://github.com/JCaet/game-night-decider.git
cd game-night-decider

# Install dependencies
uv sync --group dev

# Create .env file
cp .env.example .env
# Edit .env and add your TELEGRAM_BOT_TOKEN

# Run the bot
uv run python -m src.bot.main
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `DATABASE_URL` | PostgreSQL connection string (optional, defaults to SQLite) |

## Deployment

The bot is designed to run on Google Cloud Run. See `.github/workflows/deploy.yml` for the CI/CD pipeline.

### Required GitHub Secrets

- `GCP_PROJECT_ID`
- `GCP_CREDENTIALS`
- `TELEGRAM_BOT_TOKEN`
- `DATABASE_URL` (Cloud SQL connection string)

## Development

```bash
# Run tests
uv run pytest

# Lint
uv run ruff check .

# Type check
uv run mypy .
```

## Attribution

Game data is sourced from [BoardGameGeek](https://boardgamegeek.com/) via their [XML API](https://boardgamegeek.com/wiki/page/BGG_XML_API2). Usage is subject to BGG's [API terms of use](https://boardgamegeek.com/xmlapi/termsofuse). "BoardGameGeek" is a trademark of BoardGameGeek, LLC.

## License

MIT
