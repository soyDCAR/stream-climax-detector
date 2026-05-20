"""
consumer.py — Conecta al chat de Kick vía WebSocket (Pusher) y
emite cada mensaje recibido al logger estructurado.

Flujo:
  1. REST API de Kick  →  obtiene chatroom_id del canal
  2. WebSocket Pusher  →  subscribe a chatrooms.{chatroom_id}
  3. Loop              →  filtra ChatMessageSentEvent, loguea, ping/30s
"""

import asyncio
import json
import time

import aiohttp
import structlog
import websockets

from climax.config import get_kick_channel

log = structlog.get_logger()

# ── Constantes de Kick/Pusher ────────────────────────────────────────────────

KICK_API_BASE = "https://kick.com/api/v2"
PUSHER_APP_KEY = "32cbd69e4b950bf97679"
PUSHER_WS_URL = (
    f"wss://ws-us2.pusher.com/app/{PUSHER_APP_KEY}"
    "?protocol=7&client=js&version=8.4.0-rc2&flash=false"
)
CHAT_EVENT = "App\\Events\\ChatMessageSentEvent"
PING_INTERVAL = 30  # segundos


# ── Helpers ──────────────────────────────────────────────────────────────────


async def get_chatroom_id(channel_slug: str) -> int:
    """
    Llama a la API REST de Kick y devuelve el chatroom_id del canal.
    Lanza RuntimeError si el canal no existe o la API falla.
    """
    url = f"{KICK_API_BASE}/channels/{channel_slug}"
    headers = {
        # Kick a veces bloquea requests sin User-Agent de navegador
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                raise RuntimeError(f"Canal '{channel_slug}' no encontrado en Kick.")
            if resp.status != 200:
                raise RuntimeError(
                    f"Error al consultar API de Kick: HTTP {resp.status}"
                )
            data = await resp.json()

    chatroom_id: int = data["chatroom"]["id"]
    log.info(
        "chatroom_resuelto",
        channel=channel_slug,
        chatroom_id=chatroom_id,
    )
    return chatroom_id


def _subscribe_message(chatroom_id: int) -> str:
    """Genera el payload JSON que Pusher espera para suscribirse a un canal."""
    return json.dumps(
        {
            "event": "pusher:subscribe",
            "data": {"auth": "", "channel": f"chatrooms.{chatroom_id}"},
        }
    )


def _parse_chat_message(raw: str) -> dict | None:
    """
    Parsea un frame WebSocket crudo de Pusher.
    Devuelve un dict con {username, content, created_at} si es un mensaje
    de chat, o None si es otro tipo de evento (ping, suscripción, etc).
    """
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if outer.get("event") != CHAT_EVENT:
        return None

    # El campo "data" de Pusher viene como string JSON anidado
    try:
        inner = json.loads(outer["data"])
    except (json.JSONDecodeError, KeyError):
        return None

    msg = inner.get("message", {})
    sender = inner.get("sender", {})

    return {
        "username": sender.get("username", "unknown"),
        "content": msg.get("content", ""),
        "created_at": msg.get("created_at", ""),
    }


# ── Loop principal ───────────────────────────────────────────────────────────


async def run() -> None:
    """
    Punto de entrada del consumer. Se conecta al chat de Kick y loguea
    cada mensaje hasta que se interrumpa con Ctrl+C.
    Implementa reconexión automática con backoff exponencial.
    """
    channel = get_kick_channel()
    chatroom_id = await get_chatroom_id(channel)

    backoff = 1  # segundos de espera antes de reconectar

    log.info("consumer_iniciando", channel=channel, chatroom_id=chatroom_id)

    while True:
        try:
            await _connect_and_listen(chatroom_id)
            backoff = 1  # reset si la conexión fue exitosa
        except websockets.exceptions.ConnectionClosed as exc:
            log.warning("conexion_cerrada", code=exc.code, reason=exc.reason)
        except Exception as exc:  # noqa: BLE001
            log.error("error_inesperado", error=str(exc))

        log.info("reconectando", espera_segundos=backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)  # backoff exponencial, máximo 60s


async def _connect_and_listen(chatroom_id: int) -> None:
    """
    Abre una conexión WebSocket, se suscribe al chatroom y procesa
    mensajes hasta que la conexión se cierre.
    """
    async with websockets.connect(PUSHER_WS_URL) as ws:
        log.info("websocket_conectado", url=PUSHER_WS_URL)

        # Suscribirse al canal del chatroom
        await ws.send(_subscribe_message(chatroom_id))

        last_ping = time.monotonic()

        async for raw_message in ws:
            # Ping periódico para mantener la conexión viva
            now = time.monotonic()
            if now - last_ping >= PING_INTERVAL:
                await ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                last_ping = now

            parsed = _parse_chat_message(raw_message)
            if parsed is None:
                continue  # evento de sistema, ignorar

            log.info(
                "chat_message",
                channel_id=chatroom_id,
                username=parsed["username"],
                content=parsed["content"],
                created_at=parsed["created_at"],
            )
