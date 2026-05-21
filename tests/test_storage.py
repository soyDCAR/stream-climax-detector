"""
tests/test_storage.py — Tests unitarios de Storage (SQLite).

Usamos una DB en memoria (:memory:) para que los tests sean rápidos
y no dejen archivos en disco. Cada test recibe una instancia nueva
de Storage a través del fixture `storage`.
"""

import time
from pathlib import Path

import pytest

from climax.scorer import ClimaxResult
from climax.storage import Storage

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def storage(tmp_path: Path):
    """
    Crea una instancia de Storage en un archivo temporal por test.
    tmp_path es un fixture de pytest que crea un directorio único por test.

    Por qué tmp_path y no :memory::
    sqlite3 en modo :memory: no comparte la conexión entre llamadas
    cuando usamos check_same_thread=False. tmp_path es más robusto.
    """
    db_path = tmp_path / "test_climax.db"
    s = Storage(db_path=db_path, channel="test_channel")
    s.open()
    yield s
    s.close()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_result(
    climax_score: float = 50.0,
    z_score: float = 0.0,
    is_peak: bool = False,
    raw_score: float = 10.0,
) -> ClimaxResult:
    """Crea un ClimaxResult con valores controlados para tests."""
    return ClimaxResult(
        timestamp=time.monotonic(),
        raw_score=raw_score,
        climax_score=climax_score,
        z_score=z_score,
        is_peak=is_peak,
        history_mean=10.0,
        history_std=2.0,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_creates_table_on_open(storage: Storage):
    """
    Después de open(), la tabla climax_results debe existir.
    Verificamos consultando sqlite_master — la tabla de metadatos de SQLite.
    """
    cursor = storage._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='climax_results'"
    )
    assert cursor.fetchone() is not None, "Tabla climax_results no fue creada"


def test_save_increments_row_count(storage: Storage):
    """Cada save() agrega exactamente una fila a la tabla."""
    result = _make_result()

    storage.save(result)
    count1 = storage._conn.execute("SELECT COUNT(*) FROM climax_results").fetchone()[0]

    storage.save(result)
    count2 = storage._conn.execute("SELECT COUNT(*) FROM climax_results").fetchone()[0]

    assert count1 == 1
    assert count2 == 2


def test_fetch_recent_returns_saved_data(storage: Storage):
    """
    fetch_recent() debe devolver el resultado guardado con los valores correctos.
    """
    result = _make_result(climax_score=75.5, z_score=1.8, is_peak=False)
    storage.save(result)

    rows = storage.fetch_recent(limit=10)

    assert len(rows) == 1
    assert abs(rows[0]["climax_score"] - 75.5) < 0.01
    assert abs(rows[0]["z_score"] - 1.8) < 0.01
    assert rows[0]["is_peak"] == 0


def test_fetch_recent_chronological_order(storage: Storage):
    """
    fetch_recent() debe devolver filas en orden cronológico (más viejo primero).
    Guardamos con scores diferentes para identificarlos.
    """
    for score in [10.0, 20.0, 30.0]:
        storage.save(_make_result(climax_score=score))
        time.sleep(0.01)  # Garantizamos timestamps distintos

    rows = storage.fetch_recent(limit=10)
    scores = [r["climax_score"] for r in rows]

    assert scores == sorted(scores), f"No están en orden cronológico: {scores}"


def test_fetch_recent_respects_limit(storage: Storage):
    """
    fetch_recent(limit=N) devuelve como máximo N filas.
    Aunque haya más datos en la DB.
    """
    for _ in range(10):
        storage.save(_make_result())

    rows = storage.fetch_recent(limit=3)
    assert len(rows) == 3


def test_fetch_recent_empty_db(storage: Storage):
    """Sin datos, fetch_recent devuelve lista vacía — no lanza excepción."""
    rows = storage.fetch_recent(limit=10)
    assert rows == []


def test_fetch_peaks_returns_only_peaks(storage: Storage):
    """
    fetch_peaks() debe devolver solo filas con is_peak=True.
    Las ventanas normales (is_peak=False) no deben aparecer.
    """
    storage.save(_make_result(is_peak=False, climax_score=50.0))
    storage.save(_make_result(is_peak=True, climax_score=92.0))
    storage.save(_make_result(is_peak=False, climax_score=55.0))
    storage.save(_make_result(is_peak=True, climax_score=95.0))

    peaks = storage.fetch_peaks(limit=10)

    assert len(peaks) == 2
    assert all(r["climax_score"] >= 90.0 for r in peaks)


def test_fetch_peaks_empty_when_no_peaks(storage: Storage):
    """Si no hay picos, fetch_peaks devuelve lista vacía."""
    for _ in range(5):
        storage.save(_make_result(is_peak=False))

    peaks = storage.fetch_peaks(limit=10)
    assert peaks == []


def test_storage_filters_by_channel(tmp_path: Path):
    """
    Dos Storage distintos con channels distintos no se ven los datos entre sí.
    fetch_recent filtra por canal — preparación para multi-canal.
    """
    db_path = tmp_path / "shared.db"

    with Storage(db_path=db_path, channel="canal_a") as s_a:
        s_a.save(_make_result(climax_score=10.0))

    with Storage(db_path=db_path, channel="canal_b") as s_b:
        s_b.save(_make_result(climax_score=99.0))
        rows = s_b.fetch_recent(limit=10)

    # canal_b solo debe ver su propio dato
    assert len(rows) == 1
    assert abs(rows[0]["climax_score"] - 99.0) < 0.01


def test_save_without_open_raises(tmp_path: Path):
    """
    Llamar save() sin open() debe lanzar RuntimeError con mensaje claro.
    Falla explícita > falla silenciosa.
    """
    s = Storage(db_path=tmp_path / "x.db", channel="test")
    with pytest.raises(RuntimeError, match="Storage no abierto"):
        s.save(_make_result())


def test_context_manager_closes_connection(tmp_path: Path):
    """
    Al salir del bloque with, la conexión debe estar cerrada.
    """
    db_path = tmp_path / "ctx.db"
    with Storage(db_path=db_path, channel="test") as s:
        s.save(_make_result())

    # Después del with, _conn debe ser None
    assert s._conn is None


def test_result_dict_has_expected_keys(storage: Storage):
    """
    Las filas de fetch_recent deben tener exactamente los campos
    que el dashboard espera para construir el DataFrame.
    """
    storage.save(_make_result())
    rows = storage.fetch_recent(limit=1)

    expected_keys = {
        "timestamp",
        "raw_score",
        "climax_score",
        "z_score",
        "is_peak",
        "history_mean",
        "history_std",
    }
    assert set(rows[0].keys()) == expected_keys
