"""
dashboard.py — Streamlit dashboard en tiempo real del Climax Scorer.

Ejecución: streamlit run src/climax/dashboard.py

Muestra:
  - 3 métricas live: score actual, z-score, total picos
  - Timeline de climax_score en los últimos N minutos
  - Tabla de últimos picos detectados

El consumer (WebSocket → aggregator → scorer → DB) corre en un thread
dedicado gestionado desde el sidebar. El usuario puede escribir cualquier
canal de Kick y arrancar/parar el consumer sin reiniciar la app.

Modelo de ejecución de Streamlit:
  El script se re-ejecuta COMPLETO cada vez que hay una interacción
  o cuando st.rerun() es llamado. st.cache_resource garantiza que
  la conexión a SQLite se crea una sola vez, no en cada re-ejecución.
"""

import asyncio
import threading
import time

import pandas as pd
import streamlit as st

from climax.aggregator import run as aggregator_run
from climax.consumer import run as consumer_run
from climax.scorer import ClimaxScorer
from climax.storage import DEFAULT_DB_PATH, Storage

# ── Configuración de página ───────────────────────────────────────────────────

st.set_page_config(
    page_title="Stream Climax Detector",
    page_icon="🔥",
    layout="wide",
)

REFRESH_INTERVAL = 2  # segundos entre refrescos automáticos
DEFAULT_WINDOW = 10  # minutos de historial a mostrar por defecto


# ── Pipeline en thread separado ───────────────────────────────────────────────


async def _pipeline(
    channel: str,
    stop_event: threading.Event,
    chatroom_id: int | None = None,
) -> None:
    """
    Corrutina que orquesta consumer + aggregator + scorer + storage.
    Se ejecuta dentro de asyncio.run() en un thread dedicado.

    Por qué asyncio.run() en un thread y no en el event loop de Streamlit:
    - Streamlit no expone su event loop; crear uno propio en un thread es
      el patrón estándar para corrutinas de larga duración en Streamlit.
    - El stop_event de threading es visible desde ambos lados:
      el thread del consumer lo lee, el hilo de Streamlit lo escribe.

    Args:
        chatroom_id: si se pasa, el consumer se salta la llamada REST a Kick
                     (útil cuando la API está bloqueada por Cloudflare en Docker).
    """
    queue: asyncio.Queue = asyncio.Queue()
    scorer = ClimaxScorer()
    storage = Storage(db_path=DEFAULT_DB_PATH, channel=channel)
    storage.open()

    # Monkey-patch: interceptamos scorer.process para guardar en DB
    # igual que en run_consumer.py — sin modificar la clase scorer
    _original_process = scorer.process

    def _process_and_save(window):
        result = _original_process(window)
        storage.save(result)
        return result

    scorer.process = _process_and_save

    try:
        await asyncio.gather(
            consumer_run(
                queue=queue,
                channel=channel,
                stop_event=stop_event,
                chatroom_id=chatroom_id,
            ),
            aggregator_run(queue=queue, scorer=scorer),
        )
    finally:
        storage.close()


def _run_pipeline_in_thread(
    channel: str,
    stop_event: threading.Event,
    chatroom_id: int | None = None,
) -> None:
    """Función target del thread — crea un event loop propio y corre el pipeline."""
    asyncio.run(_pipeline(channel, stop_event, chatroom_id))


def start_worker(
    channel: str,
    chatroom_id: int | None = None,
) -> tuple[threading.Thread, threading.Event]:
    """Crea y arranca un nuevo thread del pipeline para el canal dado."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run_pipeline_in_thread,
        args=(channel, stop_event, chatroom_id),
        daemon=True,  # muere automáticamente si Streamlit termina
        name=f"climax-worker-{channel}",
    )
    thread.start()
    return thread, stop_event


def stop_worker(
    thread: threading.Thread | None,
    stop_event: threading.Event | None,
    timeout: float = 5.0,
) -> None:
    """Señaliza stop_event y espera a que el thread termine (máx timeout segundos)."""
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)


# ── Conexión a SQLite (cacheada) ──────────────────────────────────────────────


@st.cache_resource
def get_storage(channel: str) -> Storage:
    """
    Abre la conexión a SQLite una sola vez por (sesión × canal).
    st.cache_resource persiste el objeto entre re-ejecuciones del script.

    Por qué cache_resource y no cache_data:
    - cache_data serializa el resultado (útil para DataFrames, dicts)
    - cache_resource guarda el objeto vivo (conexiones, modelos ML, etc.)
    Una conexión SQLite no es serializable — necesitamos cache_resource.
    """
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


def render_sidebar() -> tuple[int, str]:
    """
    Renderiza el sidebar con input de canal y controles.
    Devuelve (minutos_ventana, canal_activo).

    Gestión del worker:
    - Si el usuario cambia el canal → para el worker actual, arranca uno nuevo
    - El estado del worker vive en st.session_state para persistir entre reruns
    """
    st.sidebar.title("⚙️ Configuración")

    # ── Input de canal ────────────────────────────────────────────────────────
    channel_input = (
        st.sidebar.text_input(
            "Canal de Kick",
            value=st.session_state.get("channel_input", ""),
            placeholder="nexxuz, xqc, ibai...",
            help="Escribe el nombre del canal y presiona Enter para conectar",
        )
        .strip()
        .lower()
    )

    chatroom_id_raw = st.sidebar.text_input(
        "Chatroom ID (opcional)",
        value=st.session_state.get("chatroom_id_input", ""),
        placeholder="123456",
        help=(
            "Evita el bloqueo de Cloudflare en Docker/HF. "
            "Encuéntralo en: kick.com/api/v2/channels/CANAL → chatroom.id"
        ),
    ).strip()

    # Parseamos el chatroom_id — None si está vacío o no es número
    chatroom_id: int | None = None
    if chatroom_id_raw:
        try:
            chatroom_id = int(chatroom_id_raw)
        except ValueError:
            st.sidebar.error("Chatroom ID debe ser un número")

    # Guardamos los valores para que no se reseteen en reruns
    st.session_state["channel_input"] = channel_input
    st.session_state["chatroom_id_input"] = chatroom_id_raw

    active_channel = st.session_state.get("active_channel", "")

    # Si el usuario escribió un canal diferente al activo → cambiar worker
    if channel_input and channel_input != active_channel:
        # Para el worker anterior si existe
        stop_worker(
            st.session_state.get("worker_thread"),
            st.session_state.get("stop_event"),
        )

        # Arranca el nuevo worker (con chatroom_id si se proporcionó)
        thread, stop_event = start_worker(channel_input, chatroom_id=chatroom_id)
        st.session_state["worker_thread"] = thread
        st.session_state["stop_event"] = stop_event
        st.session_state["active_channel"] = channel_input
        active_channel = channel_input

        st.sidebar.success(f"📡 Conectando a **{channel_input}**...")
    elif active_channel:
        # Worker ya corriendo — mostramos estado
        thread = st.session_state.get("worker_thread")
        if thread and thread.is_alive():
            st.sidebar.success(f"📡 Conectado: **{active_channel}**")
        else:
            st.sidebar.error(f"❌ Worker caído para **{active_channel}**")
    else:
        st.sidebar.info("👆 Escribe un canal para empezar")

    # ── Botón de parar ────────────────────────────────────────────────────────
    if active_channel and st.sidebar.button("⏹ Parar consumer"):
        stop_worker(
            st.session_state.get("worker_thread"),
            st.session_state.get("stop_event"),
        )
        st.session_state["worker_thread"] = None
        st.session_state["stop_event"] = None
        st.session_state["active_channel"] = ""
        st.session_state["channel_input"] = ""
        st.sidebar.warning("Consumer detenido")

    st.sidebar.divider()

    # ── Ventana de tiempo ─────────────────────────────────────────────────────
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
    st.sidebar.metric("Z-score umbral", "2.0")
    st.sidebar.metric("Cooldown", "30s")
    st.sidebar.metric("Refresco", f"{REFRESH_INTERVAL}s")

    st.sidebar.divider()
    db_exists = DEFAULT_DB_PATH.exists()
    if db_exists:
        size_kb = DEFAULT_DB_PATH.stat().st_size / 1024
        st.sidebar.info(f"💾 DB: {size_kb:.1f} KB")
    else:
        st.sidebar.info("💾 DB: sin datos aún")

    return minutes, active_channel


def render_metrics(df: pd.DataFrame) -> None:
    """Renderiza las 4 métricas en la fila superior."""
    col1, col2, col3, col4 = st.columns(4)

    if df.empty:
        col1.metric("Climax Score", "—")
        col2.metric("Z-Score", "—")
        col3.metric("Picos detectados", "—")
        col4.metric("Ventanas procesadas", "—")
        return

    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest

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
            "Escribe un canal en el sidebar y presiona Enter para conectar."
        )
        return

    chart_df = df.set_index("datetime")[["climax_score"]].rename(
        columns={"climax_score": "Climax Score"}
    )

    PEAK_THRESHOLD_SCORE = 88.0

    st.line_chart(
        chart_df,
        color="#7c6af7",
        height=300,
    )

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
            "Aún no se han detectado picos. " "El scorer necesita ~50s para calibrarse."
        )
        return

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

    minutes, active_channel = render_sidebar()

    # Si hay canal activo usamos su storage; si no, usamos "unknown" para
    # mostrar pantalla vacía sin errores
    channel_for_storage = active_channel if active_channel else "unknown"
    storage = get_storage(channel_for_storage)

    df = load_recent(storage, minutes=minutes)

    render_metrics(df)
    st.divider()
    render_timeline(df)
    st.divider()
    render_peaks_table(storage)

    # Auto-refresco cada REFRESH_INTERVAL segundos
    time.sleep(REFRESH_INTERVAL)
    st.rerun()


if __name__ == "__main__":
    main()
