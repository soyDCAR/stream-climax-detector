"""
storage.py — Persistencia de ClimaxResult en SQLite.

Una sola tabla: climax_results.
Usa sqlite3 de stdlib — sin dependencias extra.

Schema:
    id            INTEGER PRIMARY KEY AUTOINCREMENT
    channel       TEXT    — slug del canal (ej: "nexxuz")
    timestamp     REAL    — Unix timestamp (time.time(), no monotonic)
    raw_score     REAL
    climax_score  REAL
    z_score       REAL
    is_peak       INTEGER — 0 o 1 (SQLite no tiene BOOLEAN nativo)
    history_mean  REAL
    history_std   REAL
"""

import sqlite3
import time
from pathlib import Path

import structlog

from climax.scorer import ClimaxResult

log = structlog.get_logger()

# Ruta por defecto de la base de datos — en la raíz del proyecto
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "climax.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS climax_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel      TEXT    NOT NULL,
    timestamp    REAL    NOT NULL,
    raw_score    REAL    NOT NULL,
    climax_score REAL    NOT NULL,
    z_score      REAL    NOT NULL,
    is_peak      INTEGER NOT NULL DEFAULT 0,
    history_mean REAL    NOT NULL,
    history_std  REAL    NOT NULL
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_climax_timestamp
ON climax_results (timestamp DESC);
"""

_INSERT = """
INSERT INTO climax_results
    (channel, timestamp, raw_score, climax_score,
     z_score, is_peak, history_mean, history_std)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?);
"""


class Storage:
    """
    Wrapper sobre sqlite3 para persistir ClimaxResults.

    Por qué una clase y no funciones sueltas:
    - La conexión a SQLite es un recurso que debe abrirse una vez
      y cerrarse limpiamente — encajarla en una clase con __enter__/__exit__
      garantiza que no dejamos conexiones colgando.
    - Usamos WAL (Write-Ahead Logging) para que el dashboard pueda leer
      mientras el consumer escribe sin bloqueos.
    """

    def __init__(
        self, db_path: Path = DEFAULT_DB_PATH, channel: str = "unknown"
    ) -> None:
        self.db_path = db_path
        self.channel = channel
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """Abre la conexión y crea la tabla si no existe."""
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        # WAL mode: permite lecturas concurrentes mientras se escribe
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(_CREATE_TABLE)
        self._conn.execute(_CREATE_INDEX)
        self._conn.commit()
        log.info("storage_abierto", db_path=str(self.db_path), channel=self.channel)

    def close(self) -> None:
        """Cierra la conexión limpiamente."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    def save(self, result: ClimaxResult) -> None:
        """
        Persiste un ClimaxResult.

        Nota: usamos time.time() (Unix timestamp) para el campo timestamp,
        no result.timestamp (que es time.monotonic() — relativo al inicio
        del proceso, no al tiempo real). El dashboard necesita timestamps
        reales para mostrar el eje X correctamente.
        """
        if self._conn is None:
            raise RuntimeError("Storage no abierto. Llama a open() primero.")

        self._conn.execute(
            _INSERT,
            (
                self.channel,
                time.time(),        # Unix timestamp real
                result.raw_score,
                result.climax_score,
                result.z_score,
                int(result.is_peak),
                result.history_mean,
                result.history_std,
            ),
        )
        self._conn.commit()

    def fetch_recent(self, limit: int = 120) -> list[dict]:
        """
        Devuelve las últimas `limit` filas ordenadas por timestamp ascendente.
        120 filas = 10 minutos de datos a 1 ventana/5s.

        Devuelve lista de dicts para facilitar la conversión a DataFrame
        en el dashboard.
        """
        if self._conn is None:
            raise RuntimeError("Storage no abierto.")

        cursor = self._conn.execute(
            """
            SELECT timestamp, raw_score, climax_score, z_score,
                   is_peak, history_mean, history_std
            FROM climax_results
            WHERE channel = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (self.channel, limit),
        )
        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        # Invertimos para orden cronológico (DESC → ASC)
        return list(reversed(rows))

    def fetch_peaks(self, limit: int = 20) -> list[dict]:
        """
        Devuelve los últimos `limit` picos detectados (is_peak=1).
        """
        if self._conn is None:
            raise RuntimeError("Storage no abierto.")

        cursor = self._conn.execute(
            """
            SELECT timestamp, climax_score, z_score, raw_score
            FROM climax_results
            WHERE channel = ? AND is_peak = 1
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (self.channel, limit),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
