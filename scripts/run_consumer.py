"""
scripts/run_consumer.py — Arranca consumer + aggregator en paralelo.
Uso: python scripts/run_consumer.py
"""

import asyncio

import structlog

from climax.aggregator import run as run_aggregator
from climax.consumer import run as run_consumer

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ]
)


async def main() -> None:
    # Cola compartida: el consumer produce, el aggregator consume
    queue: asyncio.Queue = asyncio.Queue()

    # asyncio.gather corre ambas corrutinas concurrentemente en el mismo
    # event loop — no son threads, son tareas cooperativas que se turnan.
    await asyncio.gather(
        run_consumer(queue=queue),
        run_aggregator(queue=queue),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSistema detenido.")
