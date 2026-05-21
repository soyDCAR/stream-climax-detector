"""
tests/test_scorer.py — Tests unitarios del Climax Scorer.

Testeamos las funciones puras por separado y la clase ClimaxScorer
con historia controlada. Sin mocks, sin event loop.
"""

import time
from collections import deque

import pytest

from climax.aggregator import FeatureWindow
from climax.scorer import (
    ClimaxScorer,
    compute_climax_score,
    compute_raw_score,
    compute_z_score,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_window(
    msg_rate: float = 0.0,
    unique_users: float = 0.0,
    emote_ratio: float = 0.0,
    caps_ratio: float = 0.0,
    avg_msg_length: float = 0.0,
    exclamation_ratio: float = 0.0,
    link_ratio: float = 0.0,
) -> FeatureWindow:
    """Crea un FeatureWindow con valores controlados para tests."""
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


# ── Tests: compute_raw_score ─────────────────────────────────────────────────


def test_raw_score_empty_window_is_zero():
    """
    Todos los features en 0 → raw_score = 0.0.
    Evita que un chat muerto genere scores positivos.
    """
    window = _make_window()
    assert compute_raw_score(window) == 0.0


def test_raw_score_is_non_negative():
    """
    raw_score nunca puede ser negativo (aunque link_ratio sea alto).
    El max(0.0, ...) en compute_raw_score lo garantiza.
    """
    # Solo links — peso negativo dominante
    window = _make_window(link_ratio=1.0)
    assert compute_raw_score(window) >= 0.0


def test_raw_score_hype_window_is_high():
    """
    Chat en pleno hype (máximos en todos los features positivos) → score alto.
    No testeamos un valor exacto, sino que supere un umbral razonable.
    """
    window = _make_window(
        msg_rate=50.0,  # = referencia máxima
        unique_users=30.0,  # = referencia máxima
        emote_ratio=1.0,
        caps_ratio=1.0,
        exclamation_ratio=1.0,
    )
    score = compute_raw_score(window)
    assert score >= 80.0, f"Esperaba score >= 80 en hype máximo, got {score}"


def test_raw_score_link_spam_penalizes():
    """
    Mismo msg_rate con y sin spam de links — el spam debe dar score menor.
    """
    base = _make_window(msg_rate=20.0)
    with_spam = _make_window(msg_rate=20.0, link_ratio=1.0)
    assert compute_raw_score(base) > compute_raw_score(with_spam)


# ── Tests: compute_z_score ───────────────────────────────────────────────────


def test_z_score_empty_history():
    """Sin historia → z=0, mean=current, std=0."""
    history: deque = deque(maxlen=10)
    z, mean, std = compute_z_score(42.0, history)
    assert z == 0.0
    assert mean == 42.0
    assert std == 0.0


def test_z_score_single_element_history():
    """Con un solo elemento en historia → z=0 (no hay std)."""
    history: deque = deque([10.0], maxlen=10)
    z, mean, std = compute_z_score(50.0, history)
    assert z == 0.0


def test_z_score_flat_history():
    """Historia completamente plana (std=0) → z=0 para evitar división por cero."""
    history: deque = deque([20.0] * 10, maxlen=10)
    z, mean, std = compute_z_score(20.0, history)
    assert z == 0.0
    assert std == 0.0


def test_z_score_positive_spike():
    """
    Historia con media 10, std 2. Valor actual 14 → z = (14-10)/2 = 2.0.
    """
    # Historia: 10 valores = 10, con std=0. Agregamos varianza manualmente.
    history: deque = deque(
        [8.0, 10.0, 12.0, 10.0, 10.0, 8.0, 12.0, 10.0, 10.0, 10.0], maxlen=20
    )
    z, mean, std = compute_z_score(14.0, history)
    assert z > 1.5, f"Esperaba z > 1.5 para un pico, got {z}"


def test_z_score_negative_dip():
    """Un valor por debajo de la media → z negativo."""
    history: deque = deque([10.0, 12.0, 11.0, 10.0, 13.0], maxlen=10)
    z, _, _ = compute_z_score(5.0, history)
    assert z < 0.0


# ── Tests: compute_climax_score ───────────────────────────────────────────────


def test_climax_score_z_zero_is_50():
    """z=0 → climax_score = 50.0 (punto neutro de la sigmoid)."""
    score = compute_climax_score(0.0)
    assert abs(score - 50.0) < 0.01


def test_climax_score_range():
    """climax_score siempre está en [0, 100]."""
    for z in [-10.0, -2.0, -1.0, 0.0, 1.0, 2.0, 10.0]:
        score = compute_climax_score(z)
        assert 0.0 <= score <= 100.0, f"Score fuera de rango para z={z}: {score}"


def test_climax_score_monotonic():
    """A mayor z, mayor climax_score — la sigmoid es monotónica creciente."""
    scores = [compute_climax_score(z) for z in [-2.0, -1.0, 0.0, 1.0, 2.0]]
    assert scores == sorted(scores)


def test_climax_score_z2_is_above_88():
    """z=2 (umbral de pico) debe dar climax_score > 88."""
    score = compute_climax_score(2.0)
    assert score > 88.0, f"Esperaba > 88 para z=2, got {score}"


# ── Tests: ClimaxScorer (stateful) ────────────────────────────────────────────


def test_scorer_no_peak_without_history():
    """
    Sin historia suficiente, no debe detectar picos aunque el score sea alto.
    El scorer necesita al menos 2 ventanas para calcular z-score.
    """
    scorer = ClimaxScorer(z_threshold=2.0, cooldown_seconds=0.0)
    window = _make_window(msg_rate=50.0, emote_ratio=1.0, caps_ratio=1.0)
    result = scorer.process(window)
    assert result.is_peak is False  # no hay historia → z=0


def test_scorer_detects_peak_above_threshold():
    """
    Con historia de valores bajos y un spike alto → debe detectar pico.
    """
    scorer = ClimaxScorer(z_threshold=2.0, cooldown_seconds=0.0, min_score_for_peak=0.0)

    # Alimentamos historia con valores bajos
    low_window = _make_window(msg_rate=1.0)
    for _ in range(20):
        scorer.process(low_window)

    # Ahora un spike
    spike_window = _make_window(
        msg_rate=50.0,
        unique_users=30.0,
        emote_ratio=1.0,
        caps_ratio=1.0,
        exclamation_ratio=1.0,
    )
    result = scorer.process(spike_window)
    assert result.is_peak is True
    assert result.z_score >= 2.0


def test_scorer_cooldown_prevents_consecutive_peaks():
    """
    Dos spikes consecutivos → solo el primero debe ser pico.
    El cooldown de 30s bloquea el segundo (en el test usamos 999s).
    """
    scorer = ClimaxScorer(
        z_threshold=2.0, cooldown_seconds=999.0, min_score_for_peak=0.0
    )

    # Historia baja
    for _ in range(20):
        scorer.process(_make_window(msg_rate=1.0))

    spike = _make_window(
        msg_rate=50.0,
        unique_users=30.0,
        emote_ratio=1.0,
        caps_ratio=1.0,
    )

    result1 = scorer.process(spike)
    result2 = scorer.process(spike)

    assert result1.is_peak is True
    assert result2.is_peak is False  # bloqueado por cooldown


def test_scorer_is_warmed_up_after_10_windows():
    """is_warmed_up debe ser False antes de 10 ventanas y True después."""
    scorer = ClimaxScorer()
    window = _make_window(msg_rate=5.0)

    for i in range(9):
        scorer.process(window)
        assert (
            scorer.is_warmed_up is False
        ), f"No debería estar warmed up en ventana {i+1}"

    scorer.process(window)
    assert scorer.is_warmed_up is True


def test_scorer_result_has_all_fields():
    """ClimaxResult.as_dict() debe tener exactamente los 7 campos esperados."""
    scorer = ClimaxScorer()
    result = scorer.process(_make_window(msg_rate=5.0))
    d = result.as_dict()

    expected_keys = {
        "timestamp",
        "raw_score",
        "climax_score",
        "z_score",
        "is_peak",
        "history_mean",
        "history_std",
    }
    assert set(d.keys()) == expected_keys
