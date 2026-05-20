"""
tests/test_aggregator.py — Tests unitarios de compute_features().

Testeamos la función pura compute_features() directamente, sin queue
ni event loop. Esto hace los tests rápidos y deterministas.
"""

from climax.aggregator import ChatMessage, compute_features


def _make_msg(username: str, content: str) -> ChatMessage:
    """Helper para crear ChatMessage sin preocuparnos del timestamp."""
    return ChatMessage(username=username, content=content)


# ── Tests ────────────────────────────────────────────────────────────────────


def test_empty_window_returns_all_zeros():
    """
    Sin mensajes, todos los features deben ser 0.0.
    Evita divisiones por cero en el Scorer.
    """
    result = compute_features([], window_seconds=5.0)

    assert result.msg_rate == 0.0
    assert result.unique_users == 0.0
    assert result.emote_ratio == 0.0
    assert result.caps_ratio == 0.0
    assert result.avg_msg_length == 0.0
    assert result.exclamation_ratio == 0.0
    assert result.link_ratio == 0.0


def test_msg_rate():
    """
    10 mensajes en una ventana de 5s → msg_rate = 2.0 msgs/s.
    """
    msgs = [_make_msg("user1", "hola") for _ in range(10)]
    result = compute_features(msgs, window_seconds=5.0)

    assert result.msg_rate == 2.0


def test_unique_users():
    """
    3 mensajes de 2 usuarios distintos → unique_users = 2.0.
    El tercer mensaje del mismo usuario no debe contarse doble.
    """
    msgs = [
        _make_msg("alice", "hola"),
        _make_msg("bob", "hey"),
        _make_msg("alice", "que tal"),
    ]
    result = compute_features(msgs, window_seconds=5.0)

    assert result.unique_users == 2.0


def test_emote_ratio():
    """
    "KEKW hola POGGERS" → 3 tokens, 2 son emotes → ratio = 2/3 ≈ 0.667.
    """
    msgs = [_make_msg("user1", "KEKW hola POGGERS")]
    result = compute_features(msgs, window_seconds=5.0)

    assert abs(result.emote_ratio - 2 / 3) < 1e-6


def test_emote_ratio_no_emotes():
    """Sin emotes conocidos → ratio = 0.0."""
    msgs = [_make_msg("user1", "hola como estas")]
    result = compute_features(msgs, window_seconds=5.0)

    assert result.emote_ratio == 0.0


def test_caps_ratio():
    """
    "HOLA mundo" → letras: H,O,L,A (caps=4) + m,u,n,d,o (lower=5) = 9 letras.
    caps_ratio = 4/9 ≈ 0.444.
    Números y símbolos no cuentan como letras.
    """
    msgs = [_make_msg("user1", "HOLA mundo")]
    result = compute_features(msgs, window_seconds=5.0)

    assert abs(result.caps_ratio - 4 / 9) < 1e-6


def test_caps_ratio_all_caps():
    """Todo en mayúsculas → caps_ratio = 1.0."""
    msgs = [_make_msg("user1", "VAMOS ARRIBA")]
    result = compute_features(msgs, window_seconds=5.0)

    assert result.caps_ratio == 1.0


def test_avg_msg_length():
    """
    Mensajes de longitud 4 y 6 → promedio = 5.0.
    """
    msgs = [
        _make_msg("user1", "hola"),   # len=4
        _make_msg("user2", "adios!"), # len=6
    ]
    result = compute_features(msgs, window_seconds=5.0)

    assert result.avg_msg_length == 5.0


def test_exclamation_ratio():
    """
    1 de 2 mensajes tiene '!' → exclamation_ratio = 0.5.
    """
    msgs = [
        _make_msg("user1", "vamos!"),
        _make_msg("user2", "que paso"),
    ]
    result = compute_features(msgs, window_seconds=5.0)

    assert result.exclamation_ratio == 0.5


def test_exclamation_multiple_in_one_msg():
    """
    Un mensaje con varios '!!!' cuenta como 1, no como 3.
    exclamation_ratio mide proporción de MENSAJES, no de símbolos.
    """
    msgs = [
        _make_msg("user1", "vamos!!!"),
        _make_msg("user2", "tranquilo"),
    ]
    result = compute_features(msgs, window_seconds=5.0)

    assert result.exclamation_ratio == 0.5


def test_link_ratio():
    """
    1 de 2 mensajes tiene una URL → link_ratio = 0.5.
    """
    msgs = [
        _make_msg("user1", "mira esto https://kick.com/nexxuz"),
        _make_msg("user2", "sin link"),
    ]
    result = compute_features(msgs, window_seconds=5.0)

    assert result.link_ratio == 0.5


def test_link_ratio_no_links():
    """Sin URLs → link_ratio = 0.0."""
    msgs = [
        _make_msg("user1", "hola"),
        _make_msg("user2", "que tal"),
    ]
    result = compute_features(msgs, window_seconds=5.0)

    assert result.link_ratio == 0.0


def test_feature_window_as_dict():
    """
    as_dict() debe devolver todos los 8 campos esperados por el Scorer.
    """
    msgs = [_make_msg("user1", "KEKW!")]
    result = compute_features(msgs, window_seconds=5.0)
    d = result.as_dict()

    expected_keys = {
        "timestamp", "msg_rate", "unique_users", "emote_ratio",
        "caps_ratio", "avg_msg_length", "exclamation_ratio", "link_ratio",
    }
    assert set(d.keys()) == expected_keys
