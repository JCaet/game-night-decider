FROM python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97

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
