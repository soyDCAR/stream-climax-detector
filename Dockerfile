# ══════════════════════════════════════════════════════════════════
# Stage 1: builder — instala dependencias con uv
# ══════════════════════════════════════════════════════════════════
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder

WORKDIR /app

# Copiar archivos de dependencias primero (mejor uso de cache Docker)
COPY pyproject.toml .

# uv sync instala deps en .venv dentro de /app
# --no-dev excluye pytest, ruff, black (no necesarios en producción)
# UV_PROJECT_ENVIRONMENT apunta al venv que queremos crear
RUN uv venv .venv && \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    uv pip install --python /app/.venv/bin/python \
    "twitchio>=2.10" \
    "numpy>=1.26" \
    "structlog>=24.0" \
    "streamlit>=1.35" \
    "aiohttp>=3.0" \
    "websockets>=12.0"

# Instalar el paquete local
COPY src/ src/
RUN uv pip install --python /app/.venv/bin/python --no-deps -e .

# ══════════════════════════════════════════════════════════════════
# Stage 2: runtime — imagen final mínima
# ══════════════════════════════════════════════════════════════════
FROM python:3.11-slim-bookworm AS runtime

WORKDIR /app

# Hugging Face Spaces requiere usuario con UID=1000
RUN useradd --create-home --shell /bin/bash --uid 1000 appuser

# Copiar venv y código del stage builder
COPY --from=builder /app/.venv .venv
COPY --from=builder /app/src src/

# Dar permisos al usuario sobre el directorio
RUN chown -R appuser:appuser /app

# Python usa el venv sin necesidad de activarlo
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KICK_CHANNEL=""

# HF Spaces requiere puerto 7860
EXPOSE 7860

USER appuser

CMD ["streamlit", "run", "src/climax/dashboard.py", \
     "--server.port=7860", \
     "--server.address=0.0.0.0", \
     "--server.headless=true"]
