# ══════════════════════════════════════════════════════════════════
# Stage 1: builder — instala dependencias con uv
# Usamos la imagen oficial de uv que ya trae Python 3.11 + uv listo
# ══════════════════════════════════════════════════════════════════
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder

WORKDIR /app

# Copiamos solo los archivos de dependencias primero.
# Docker cachea cada capa — si pyproject.toml no cambia,
# no reinstala deps aunque cambie el código fuente.
COPY pyproject.toml .

# Instalamos dependencias de producción en /app/.venv
# --no-dev: excluye pytest, ruff, black — no los necesitamos en producción
# --frozen: usa el lock file si existe, falla si hay inconsistencias
RUN uv venv .venv && \
    uv pip install --python .venv/bin/python -e "." --no-dev

# Copiamos el código fuente DESPUÉS de las deps (mejor uso de cache)
COPY src/ src/

# ══════════════════════════════════════════════════════════════════
# Stage 2: runtime — imagen final mínima
# Solo copiamos el venv y el código — sin uv, sin compiladores
# ══════════════════════════════════════════════════════════════════
FROM python:3.11-slim-bookworm AS runtime

WORKDIR /app

# Hugging Face Spaces requiere un usuario con UID=1000.
# useradd --uid 1000 lo garantiza explícitamente.
# En local puedes correr con cualquier usuario — esto no rompe nada.
RUN useradd --create-home --shell /bin/bash --uid 1000 appuser

# Copiamos el venv construido en el stage anterior
COPY --from=builder /app/.venv .venv
COPY --from=builder /app/src src/

# Damos permisos al usuario sobre el directorio de trabajo
# (necesario para que Streamlit pueda escribir archivos temporales)
RUN chown -R appuser:appuser /app

# Variables de entorno para que Python use el venv sin activarlo
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# KICK_CHANNEL debe pasarse en runtime:
#   Local:      docker run -e KICK_CHANNEL=nexxuz ...
#   HF Spaces:  configurar en Settings > Variables
ENV KICK_CHANNEL=""

# HF Spaces requiere el puerto 7860.
# En local también funciona — solo cambia la URL de acceso.
EXPOSE 7860

# Cambiamos al usuario no-root antes de ejecutar
USER appuser

# Comando por defecto: dashboard Streamlit
# Puerto 7860 = requerido por Hugging Face Spaces
CMD ["streamlit", "run", "src/climax/dashboard.py", \
     "--server.port=7860", \
     "--server.address=0.0.0.0", \
     "--server.headless=true"]
