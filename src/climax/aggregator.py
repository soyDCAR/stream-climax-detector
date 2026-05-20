"""
aggregator.py — Ventana deslizante de 5s sobre mensajes de chat.

Recibe mensajes desde una asyncio.Queue, los acumula en una deque,
y cada WINDOW_SIZE segundos calcula 7 features estadísticos.

Features calculados:
    msg_rate          — mensajes por segundo en la ventana
    unique_users      — usuarios distintos que hablaron
    emote_ratio       — proporción de tokens que son emotes de Kick
    caps_ratio        — proporción de letras en MAYÚSCULAS
    avg_msg_length    — longitud promedio de los mensajes
    exclamation_ratio — proporción de mensajes con al menos un '!'
    link_ratio        — proporción de mensajes con una URL
"""

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger()

# ── Configuración ────────────────────────────────────────────────────────────

WINDOW_SIZE = 5  # segundos por ventana
# Máximo de mensajes que guardamos en la deque.
# A ~200 msgs/s (chat muy activo) en 5s = 1000 msgs. 2000 es margen seguro.
MAX_MSGS = 2000

# Emotes comunes de Kick / globales. Esta lista crece en versiones futuras.
# Usamos un set para lookup O(1).
KICK_EMOTES: set[str] = {
    "KEKW", "POGGERS", "PogChamp", "Pog", "OMEGALUL", "LUL", "KEKLEO",
    "monkaS", "monkaHmm", "PauseChamp", "ICANT", "Clap", "EZ", "ez",
    "catJAM", "GIGACHAD", "Sadge", "pepeD", "pepeLaugh", "FeelsBadMan",
    "FeelsGoodMan", "HYPERS", "PogU", "WeirdChamp", "Copium", "Aware",
    "5Head", "EZY", "LULW", "BASED", "Prayge", "forsenCD", "D:",
}

# Regex para detectar URLs
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


# ── Estructuras de datos ─────────────────────────────────────────────────────

@dataclass
class ChatMessage:
    """Representa un mensaje de chat ya parseado por el consumer."""
    username: str
    content: str
    received_at: float = field(default_factory=time.monotonic)


@dataclass
class FeatureWindow:
    """
    Resultado de una ventana de 5s.
    Todos los valores son float para uniformidad con numpy en el Scorer.
    """
    timestamp: float          # tiempo de cierre de la ventana (monotonic)
    msg_rate: float           # msgs/segundo
    unique_users: float       # cantidad de usuarios únicos
    emote_ratio: float        # [0.0 – 1.0]
    caps_ratio: float         # [0.0 – 1.0]
    avg_msg_length: float     # caracteres promedio
    exclamation_ratio: float  # [0.0 – 1.0]
    link_ratio: float         # [0.0 – 1.0]

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "msg_rate": self.msg_rate,
            "unique_users": self.unique_users,
            "emote_ratio": self.emote_ratio,
            "caps_ratio": self.caps_ratio,
            "avg_msg_length": self.avg_msg_length,
            "exclamation_ratio": self.exclamation_ratio,
            "link_ratio": self.link_ratio,
        }


# ── Cálculo de features ──────────────────────────────────────────────────────

def compute_features(
    messages: list[ChatMessage], window_seconds: float
) -> FeatureWindow:
    """
    Dado una lista de ChatMessage y la duración de la ventana en segundos,
    devuelve un FeatureWindow con los 7 features calculados.

    Separamos esta función del loop async para poder testearla fácilmente
    sin necesitar una queue ni un event loop corriendo.
    """
    n = len(messages)

    if n == 0:
        return FeatureWindow(
            timestamp=time.monotonic(),
            msg_rate=0.0,
            unique_users=0.0,
            emote_ratio=0.0,
            caps_ratio=0.0,
            avg_msg_length=0.0,
            exclamation_ratio=0.0,
            link_ratio=0.0,
        )

    # 1. msg_rate — mensajes por segundo
    msg_rate = n / window_seconds

    # 2. unique_users — set de usernames únicos
    unique_users = float(len({m.username for m in messages}))

    # 3. emote_ratio — tokens que son emotes / total tokens
    total_tokens = 0
    emote_tokens = 0
    for msg in messages:
        tokens = msg.content.split()
        total_tokens += len(tokens)
        emote_tokens += sum(1 for t in tokens if t in KICK_EMOTES)
    emote_ratio = emote_tokens / total_tokens if total_tokens > 0 else 0.0

    # 4. caps_ratio — letras mayúsculas / total letras (ignora números y símbolos)
    total_letters = 0
    caps_letters = 0
    for msg in messages:
        for ch in msg.content:
            if ch.isalpha():
                total_letters += 1
                if ch.isupper():
                    caps_letters += 1
    caps_ratio = caps_letters / total_letters if total_letters > 0 else 0.0

    # 5. avg_msg_length — promedio de len(content)
    avg_msg_length = sum(len(m.content) for m in messages) / n

    # 6. exclamation_ratio — mensajes con al menos un '!'
    exclamation_ratio = sum(1 for m in messages if "!" in m.content) / n

    # 7. link_ratio — mensajes con al menos una URL
    link_ratio = sum(1 for m in messages if _URL_RE.search(m.content)) / n

    return FeatureWindow(
        timestamp=time.monotonic(),
        msg_rate=msg_rate,
        unique_users=unique_users,
        emote_ratio=emote_ratio,
        caps_ratio=caps_ratio,
        avg_msg_length=avg_msg_length,
        exclamation_ratio=exclamation_ratio,
        link_ratio=link_ratio,
    )


# ── Loop async ───────────────────────────────────────────────────────────────

async def run(
    queue: asyncio.Queue,
    window_seconds: float = WINDOW_SIZE,
    scorer=None,
) -> None:
    """
    Loop principal del aggregator.

    - Consume mensajes de la queue sin bloquearse.
    - Cada `window_seconds` calcula los features y los pasa al scorer.
    - La deque actúa como buffer: guarda solo los mensajes de la
      ventana actual. Al cerrar cada ventana, la vaciamos para empezar
      la siguiente ventana limpia.

    Args:
        queue:          asyncio.Queue alimentada por el consumer.
        window_seconds: duración de cada ventana en segundos (default 5).
        scorer:         instancia de ClimaxScorer. Si es None, solo loguea features.
    """
    buffer: deque[ChatMessage] = deque(maxlen=MAX_MSGS)
    window_start = time.monotonic()

    log.info("aggregator_iniciando", window_seconds=window_seconds)

    while True:
        # Intentamos sacar mensajes de la queue sin bloquear
        # el cierre de ventana. timeout=0.1s para no quemar CPU.
        try:
            raw = await asyncio.wait_for(queue.get(), timeout=0.1)
            buffer.append(
                ChatMessage(
                    username=raw["username"],
                    content=raw["content"],
                    received_at=time.monotonic(),
                )
            )
            queue.task_done()
        except asyncio.TimeoutError:
            pass  # no había mensajes, verificamos si toca cerrar ventana

        # ¿Cerramos la ventana?
        elapsed = time.monotonic() - window_start
        if elapsed >= window_seconds:
            features = compute_features(list(buffer), elapsed)
            log.info("feature_window", **features.as_dict())

            # Pasar al scorer si está disponible
            if scorer is not None:
                scorer.process(features)

            buffer.clear()
            window_start = time.monotonic()
