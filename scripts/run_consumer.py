"""
scripts/run_consumer.py — Arranca el consumer de chat de Kick.
Uso: python scripts/run_consumer.py
"""

import asyncio

import structlog

from climax.consumer import run

# Configurar structlog para output legible en consola durante desarrollo
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ]
)

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nConsumer detenido.")
