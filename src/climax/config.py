"""
config.py — Lee variables de entorno para el proyecto.

No usamos python-dotenv: cargamos el .env manualmente con _load_env()
para no agregar dependencias fuera del stack acordado.
"""

import os
from pathlib import Path


def _load_env() -> None:
    """
    Lee el archivo .env de la raíz del proyecto e inyecta cada línea
    como variable de entorno, solo si la variable no existe ya.
    (Si ya existe en el entorno del sistema, ese valor tiene prioridad.)
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return

    with open(env_path) as f:
        for line in f:
            line = line.strip()
            # Ignorar comentarios y líneas vacías
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # No sobreescribir variables ya definidas en el sistema
            if key not in os.environ:
                os.environ[key] = value


# Cargamos .env al importar el módulo
_load_env()


def get_kick_channel() -> str:
    """
    Devuelve el slug del canal de Kick.
    Falla explícitamente si no está configurado.
    """
    channel = os.environ.get("KICK_CHANNEL", "").strip()
    if not channel:
        raise RuntimeError(
            "Variable de entorno KICK_CHANNEL no definida. "
            "Copia .env.example a .env y rellena el valor."
        )
    # Tolerancia: si alguien pone la URL completa, extraemos solo el slug
    if channel.startswith("http"):
        channel = channel.rstrip("/").split("/")[-1]
    return channel.lower()
