# Curio: one image with the API and the built web app.
# Build and run it with ./curio.sh (see README "Run in Podman").

# ---- web app -----------------------------------------------------------------
FROM docker.io/library/node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- API + analysis ------------------------------------------------------------
FROM docker.io/library/python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
COPY --from=web /web/dist ./web/dist

RUN useradd --create-home --uid 10001 curio && mkdir -p /app/data && chown curio /app/data
USER curio

# Ollama runs on the host; Podman resolves host.containers.internal to it.
ENV PATH=/app/.venv/bin:$PATH \
    CURIO_DATABASE_URL=sqlite:////app/data/curio.db \
    CURIO_OLLAMA_HOST=http://host.containers.internal:11434

VOLUME /app/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health')"
CMD ["curio", "api", "--host", "0.0.0.0", "--port", "8000"]
