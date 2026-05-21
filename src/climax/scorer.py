"""
scorer.py — Climax Scorer con z-score adaptativo y cooldown.

Recibe un FeatureWindow del aggregator, calcula un climax score [0-100],
y detecta picos cuando el score supera el umbral adaptativo.

Flujo:
    FeatureWindow → weighted_sum → normalize → z-score → peak detection
"""

import time
from collections import deque
from dataclasses import dataclass

import numpy as np
import structlog

from climax.aggregator import FeatureWindow

log = structlog.get_logger()

# ── Configuración ────────────────────────────────────────────────────────────

# Pesos de cada feature en la suma ponderada.
# link_ratio es negativo: penaliza spam de links (no es hype real).
# Suma de absolutos ≈ 1.0 para que raw_score sea interpretable.
WEIGHTS: dict[str, float] = {
    "msg_rate": 0.35,
    "unique_users": 0.20,
    "emote_ratio": 0.20,
    "caps_ratio": 0.10,
    "avg_msg_length": 0.05,
    "exclamation_ratio": 0.05,
    "link_ratio": -0.05,
}

# Valores de referencia para normalizar cada feature a [0, 1].
# Basados en un chat activo típico — ajustar con datos reales.
NORMALIZATION: dict[str, float] = {
    "msg_rate": 50.0,  # 50 msgs/s = chat muy activo
    "unique_users": 30.0,  # 30 usuarios únicos en 5s
    "emote_ratio": 1.0,  # ya es ratio [0-1]
    "caps_ratio": 1.0,  # ya es ratio [0-1]
    "avg_msg_length": 80.0,  # 80 chars = mensaje largo
    "exclamation_ratio": 1.0,  # ya es ratio [0-1]
    "link_ratio": 1.0,  # ya es ratio [0-1]
}

# Ventana histórica para z-score: 10 minutos / 5s por ventana = 120 entradas
HISTORY_SIZE = 120

# Z-score mínimo para declarar un pico
Z_THRESHOLD = 2.0

# Cooldown en segundos — no detectar otro pico hasta que pase este tiempo
COOLDOWN_SECONDS = 30.0

# Score mínimo absoluto para activar detección (evita alertas en chat muerto)
MIN_SCORE_FOR_PEAK = 10.0


# ── Estructuras de datos ─────────────────────────────────────────────────────


@dataclass
class ClimaxResult:
    """Resultado del scorer para una ventana de 5s."""

    timestamp: float
    raw_score: float  # suma ponderada sin normalizar por z-score
    climax_score: float  # score final [0-100]
    z_score: float  # desviaciones sobre la media histórica
    is_peak: bool  # True si se detectó un pico de hype
    history_mean: float  # media histórica (para debug/visualización)
    history_std: float  # std histórica (para debug/visualización)

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "raw_score": self.raw_score,
            "climax_score": self.climax_score,
            "z_score": self.z_score,
            "is_peak": self.is_peak,
            "history_mean": self.history_mean,
            "history_std": self.history_std,
        }


# ── Lógica pura ──────────────────────────────────────────────────────────────


def compute_raw_score(window: FeatureWindow) -> float:
    """
    Paso 1: suma ponderada de los 7 features.

    Cada feature se normaliza primero a [0, 1] dividiéndolo por su
    valor de referencia (NORMALIZATION), luego se multiplica por su peso.

    Devuelve un float en [0, ~1.0] en condiciones normales.
    Puede superar 1.0 en momentos de hype extremo — eso es intencional.
    """
    feature_values = {
        "msg_rate": window.msg_rate,
        "unique_users": window.unique_users,
        "emote_ratio": window.emote_ratio,
        "caps_ratio": window.caps_ratio,
        "avg_msg_length": window.avg_msg_length,
        "exclamation_ratio": window.exclamation_ratio,
        "link_ratio": window.link_ratio,
    }

    score = 0.0
    for feature, weight in WEIGHTS.items():
        norm_ref = NORMALIZATION[feature]
        # Clamp a [0, 1] antes de aplicar el peso para features de ratio
        # Para msg_rate y unique_users, permitimos > 1.0 (chat extremo)
        normalized = feature_values[feature] / norm_ref if norm_ref > 0 else 0.0
        score += weight * normalized

    # Llevamos a [0, 100]
    return max(0.0, score * 100)


def compute_z_score(current: float, history: deque) -> tuple[float, float, float]:
    """
    Paso 2: z-score adaptativo sobre la historia reciente.

    Calculamos mean y std sobre la historia pasada (sin el valor actual),
    y luego medimos cuántas desviaciones está current sobre esa distribución.

    Esto es intencional: queremos saber si el valor ACTUAL es anómalo
    respecto al pasado, no si pertenece a la misma distribución.

    Args:
        current: raw_score actual (no incluido en la historia todavía)
        history: deque con los últimos HISTORY_SIZE raw_scores pasados

    Returns:
        (z_score, mean, std)
        Si no hay suficiente historia (< 5 elementos), devuelve (0.0, current, 0.0)
    """
    if len(history) < 5:
        # Sin historia mínima, z-score no es confiable
        return 0.0, current, 0.0

    arr = np.array(history, dtype=np.float64)
    mean = float(np.mean(arr))
    std = float(np.std(arr))

    if std < 1e-9:
        # Historia completamente plana — cualquier diferencia es un spike.
        # Usamos la diferencia absoluta escalada como proxy de z-score.
        diff = current - mean
        if abs(diff) < 1e-9:
            return 0.0, mean, std
        # Retornamos z proporcional a la magnitud del spike sobre la media
        # (arbitrario pero informativo para el logger)
        z = diff / max(abs(mean), 1.0) * 3.0
        return z, mean, std

    z = (current - mean) / std
    return z, mean, std


def compute_climax_score(z_score: float) -> float:
    """
    Paso 3: convierte el z-score a un climax score [0, 100].

    Usamos una sigmoid escalada para que:
    - z=0   → score=50 (normal)
    - z=2   → score≈88 (pico)
    - z=-2  → score≈12 (chat muerto)
    - z=±∞  → score→[0, 100]
    """
    # sigmoid: 1 / (1 + e^(-x))
    sigmoid = 1.0 / (1.0 + np.exp(-z_score))
    return float(sigmoid * 100)


# ── Clase Scorer (stateful) ──────────────────────────────────────────────────


class ClimaxScorer:
    """
    Scorer stateful: mantiene la historia de scores y el estado del cooldown.

    Por qué una clase y no funciones sueltas:
    - Necesitamos estado persistente entre llamadas (history, last_peak_time)
    - Las funciones puras (compute_raw_score, etc.) son testeables por separado
    - La clase solo maneja el estado — no hace I/O ni logging en sus métodos puros
    """

    def __init__(
        self,
        history_size: int = HISTORY_SIZE,
        z_threshold: float = Z_THRESHOLD,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        min_score_for_peak: float = MIN_SCORE_FOR_PEAK,
    ) -> None:
        self.history: deque[float] = deque(maxlen=history_size)
        self.z_threshold = z_threshold
        self.cooldown_seconds = cooldown_seconds
        self.min_score_for_peak = min_score_for_peak
        self._last_peak_time: float = 0.0  # monotonic timestamp del último pico

    def process(self, window: FeatureWindow) -> ClimaxResult:
        """
        Procesa una FeatureWindow y devuelve un ClimaxResult.
        Actualiza el estado interno (history, last_peak_time).
        """
        # Paso 1: raw score
        raw = compute_raw_score(window)

        # Paso 2: z-score sobre historia
        z, mean, std = compute_z_score(raw, self.history)

        # Paso 3: climax score [0-100]
        climax = compute_climax_score(z)

        # Paso 4: detección de pico
        now = time.monotonic()
        cooldown_ok = (now - self._last_peak_time) >= self.cooldown_seconds
        is_peak = (
            z >= self.z_threshold and raw >= self.min_score_for_peak and cooldown_ok
        )

        if is_peak:
            self._last_peak_time = now
            log.info(
                "peak_detected",
                climax_score=round(climax, 2),
                z_score=round(z, 2),
                raw_score=round(raw, 2),
            )

        # Actualizar historia DESPUÉS de calcular z-score
        # (no incluimos el valor actual en su propia media)
        self.history.append(raw)

        result = ClimaxResult(
            timestamp=window.timestamp,
            raw_score=raw,
            climax_score=climax,
            z_score=z,
            is_peak=is_peak,
            history_mean=mean,
            history_std=std,
        )

        log.info("score_window", **result.as_dict())
        return result

    @property
    def is_warmed_up(self) -> bool:
        """
        True cuando tenemos suficiente historia para z-scores confiables.
        Necesitamos al menos 10 ventanas (50s de datos).
        """
        return len(self.history) >= 10
