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

# Creamos usuario no-root por seguridad.
# Correr como root en un contenedor es un anti-pattern —
# si hay una vulnerabilidad, el atacante tiene acceso root al host.
RUN useradd --create-home --shell /bin/bash appuser

# Copiamos el venv construido en el stage anterior
COPY --from=builder /app/.venv .venv
COPY --from=builder /app/src src/

# Variables de entorno para que Python use el venv sin activarlo
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# KICK_CHANNEL debe pasarse en runtime:
# docker run -e KICK_CHANNEL=nexxuz ...
ENV KICK_CHANNEL=""

# Puerto del dashboard Streamlit
EXPOSE 8501

# Cambiamos al usuario no-root antes de ejecutar
USER appuser

# Comando por defecto: dashboard Streamlit
# Para correr el consumer en su lugar:
# docker run ... python -m climax.consumer
CMD ["streamlit", "run", "src/climax/dashboard.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true"]
