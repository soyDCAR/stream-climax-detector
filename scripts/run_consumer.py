"""
scripts/run_consumer.py — Arranca consumer + aggregator + scorer + storage.
Uso: python scripts/run_consumer.py
"""

import asyncio

import structlog

from climax.aggregator import run as run_aggregator
from climax.config import get_kick_channel
from climax.consumer import run as run_consumer
from climax.scorer import ClimaxScorer
from climax.storage import Storage

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ]
)


async def main() -> None:
    channel = get_kick_channel()
    queue: asyncio.Queue = asyncio.Queue()
    scorer = ClimaxScorer()

    with Storage(channel=channel) as storage:
        # Monkey-patch: envolvemos scorer.process para guardar cada resultado
        _original_process = scorer.process

        def process_and_save(window):
            result = _original_process(window)
            storage.save(result)
            return result

        scorer.process = process_and_save  # type: ignore[method-assign]

        await asyncio.gather(
            run_consumer(queue=queue),
            run_aggregator(queue=queue, scorer=scorer),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSistema detenido.")
