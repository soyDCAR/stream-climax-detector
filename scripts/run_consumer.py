"""
scripts/run_consumer.py — Arranca consumer + aggregator + scorer en paralelo.
Uso: python scripts/run_consumer.py
"""

import asyncio

import structlog

from climax.aggregator import run as run_aggregator
from climax.consumer import run as run_consumer
from climax.scorer import ClimaxScorer

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ]
)


async def main() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    scorer = ClimaxScorer()

    await asyncio.gather(
        run_consumer(queue=queue),
        run_aggregator(queue=queue, scorer=scorer),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSistema detenido.")
