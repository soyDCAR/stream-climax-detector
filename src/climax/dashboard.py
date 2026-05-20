"""
dashboard.py — Streamlit dashboard en tiempo real del Climax Scorer.

Ejecución: streamlit run src/climax/dashboard.py

Muestra:
  - 3 métricas live: score actual, z-score, total picos
  - Timeline de climax_score en los últimos N minutos
  - Tabla de últimos picos detectados

Modelo de ejecución de Streamlit:
  El script se re-ejecuta COMPLETO cada vez que hay una interacción
  o cuando st.rerun() es llamado. st.cache_resource garantiza que
  la conexión a SQLite se crea una sola vez, no en cada re-ejecución.
"""

import time

import pandas as pd
import streamlit as st

from climax.config import get_kick_channel
from climax.storage import DEFAULT_DB_PATH, Storage

# ── Configuración de página ───────────────────────────────────────────────────

st.set_page_config(
    page_title="Stream Climax Detector",
    page_icon="🔥",
    layout="wide",
)

REFRESH_INTERVAL = 2   # segundos entre refrescos automáticos
DEFAULT_WINDOW   = 10  # minutos de historial a mostrar por defecto


# ── Conexión a SQLite (cacheada) ──────────────────────────────────────────────

@st.cache_resource
def get_storage() -> Storage:
    """
    Abre la conexión a SQLite una sola vez por sesión de Streamlit.
    st.cache_resource persiste el objeto entre re-ejecuciones del script.

    Por qué cache_resource y no cache_data:
    - cache_data serializa el resultado (útil para DataFrames, dicts)
    - cache_resource guarda el objeto vivo (conexiones, modelos ML, etc.)
    Una conexión SQLite no es serializable — necesitamos cache_resource.
    """
    try:
        channel = get_kick_channel()
    except RuntimeError:
        channel = "unknown"

    storage = Storage(db_path=DEFAULT_DB_PATH, channel=channel)
    storage.open()
    return storage


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_recent(storage: Storage, minutes: int) -> pd.DataFrame:
    """
    Carga las últimas `minutes` de datos como DataFrame.
    Filtra por timestamp para respetar el rango seleccionado en el sidebar.
    """
    # Calculamos cuántas filas necesitamos: 1 fila cada 5s
    limit = (minutes * 60) // 5
    rows = storage.fetch_recent(limit=limit)
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s")
    df["is_peak"] = df["is_peak"].astype(bool)
    return df


def load_peaks(storage: Storage) -> pd.DataFrame:
    """Carga los últimos 20 picos como DataFrame formateado."""
    rows = storage.fetch_peaks(limit=20)
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s")
    df["climax_score"] = df["climax_score"].round(1)
    df["z_score"] = df["z_score"].round(2)
    df["raw_score"] = df["raw_score"].round(1)
    return df[["datetime", "climax_score", "z_score", "raw_score"]]


# ── Layout ────────────────────────────────────────────────────────────────────

def render_sidebar() -> int:
    """Renderiza el sidebar y devuelve el rango de minutos seleccionado."""
    st.sidebar.title("⚙️ Configuración")

    minutes = st.sidebar.slider(
        "Ventana de tiempo",
        min_value=1,
        max_value=60,
        value=DEFAULT_WINDOW,
        step=1,
        format="%d min",
    )

    st.sidebar.divider()
    st.sidebar.markdown("**Umbrales**")

    # Mostramos los valores actuales como referencia (no editables en v0.1)
    st.sidebar.metric("Z-score umbral", "2.0")
    st.sidebar.metric("Cooldown", "30s")
    st.sidebar.metric("Refresco", f"{REFRESH_INTERVAL}s")

    st.sidebar.divider()

    try:
        channel = get_kick_channel()
        st.sidebar.success(f"📡 Canal: **{channel}**")
    except RuntimeError:
        st.sidebar.warning("⚠️ KICK_CHANNEL no configurado")

    db_exists = DEFAULT_DB_PATH.exists()
    if db_exists:
        size_kb = DEFAULT_DB_PATH.stat().st_size / 1024
        st.sidebar.info(f"💾 DB: {size_kb:.1f} KB")
    else:
        st.sidebar.warning("💾 DB: sin datos aún")

    return minutes


def render_metrics(df: pd.DataFrame) -> None:
    """Renderiza las 3 métricas en la fila superior."""
    col1, col2, col3, col4 = st.columns(4)

    if df.empty:
        col1.metric("Climax Score", "—")
        col2.metric("Z-Score", "—")
        col3.metric("Picos detectados", "—")
        col4.metric("Ventanas procesadas", "—")
        return

    latest = df.iloc[-1]
    prev   = df.iloc[-2] if len(df) > 1 else latest

    col1.metric(
        "🔥 Climax Score",
        f"{latest['climax_score']:.1f}",
        delta=f"{latest['climax_score'] - prev['climax_score']:+.1f}",
    )
    col2.metric(
        "📊 Z-Score",
        f"{latest['z_score']:.2f}",
        delta=f"{latest['z_score'] - prev['z_score']:+.2f}",
    )
    col3.metric(
        "⚡ Picos detectados",
        int(df["is_peak"].sum()),
    )
    col4.metric(
        "📦 Ventanas procesadas",
        len(df),
    )


def render_timeline(df: pd.DataFrame) -> None:
    """Renderiza el gráfico de línea del climax_score en el tiempo."""
    st.subheader("Timeline — Climax Score")

    if df.empty:
        st.info(
            "Sin datos todavía. "
            "Arranca el consumer con `python scripts/run_consumer.py`"
        )
        return

    # Preparamos el DataFrame para el gráfico
    chart_df = df.set_index("datetime")[["climax_score"]].rename(
        columns={"climax_score": "Climax Score"}
    )

    # Línea de referencia del umbral de pico (z=2 → climax≈88)
    PEAK_THRESHOLD_SCORE = 88.0

    st.line_chart(
        chart_df,
        color="#7c6af7",
        height=300,
    )

    # Marcamos los picos como puntos en un scatter aparte
    peaks_df = df[df["is_peak"]].copy()
    if not peaks_df.empty:
        st.caption(
            f"⚡ {len(peaks_df)} pico(s) en el rango mostrado "
            f"— umbral score ≈ {PEAK_THRESHOLD_SCORE:.0f}"
        )


def render_peaks_table(storage: Storage) -> None:
    """Renderiza la tabla de últimos picos detectados."""
    st.subheader("Últimos picos detectados")

    df = load_peaks(storage)

    if df.empty:
        st.info(
            "Aún no se han detectado picos. "
            "El scorer necesita ~50s para calibrarse."
        )
        return

    # Formateamos la columna datetime para legibilidad
    df["datetime"] = df["datetime"].dt.strftime("%H:%M:%S")
    df.columns = ["Hora", "Climax Score", "Z-Score", "Raw Score"]

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    st.title("🔥 Stream Climax Detector")
    st.caption("Detección de picos de hype en chat de Kick.com en tiempo real")

    storage = get_storage()
    minutes = render_sidebar()

    df = load_recent(storage, minutes=minutes)

    render_metrics(df)
    st.divider()
    render_timeline(df)
    st.divider()
    render_peaks_table(storage)

    # Auto-refresco: espera REFRESH_INTERVAL segundos y re-ejecuta el script
    time.sleep(REFRESH_INTERVAL)
    st.rerun()


if __name__ == "__main__":
    main()
