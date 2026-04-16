FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /uvx /bin/

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application code
COPY src/ ./src/

# Run the bot directly via the venv's python (avoids keeping uv resident)
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "src.bot.main"]
