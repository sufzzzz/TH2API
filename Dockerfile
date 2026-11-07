FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    TH2API_HOME=/app \
    TH2API_HOST=0.0.0.0 \
    TH2API_PORT=8787

# Dependencies first so code edits do not bust the layer cache.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

# var/ is a volume: accounts.jsonl, th2api.db and admin_token live there.
RUN mkdir -p /app/var
VOLUME ["/app/var"]
EXPOSE 8787

CMD ["uv", "run", "--frozen", "th2api-serve"]