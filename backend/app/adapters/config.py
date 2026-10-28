"""Read backend/.env on each call; process environment takes precedence."""

import os
from pathlib import Path

from dotenv import dotenv_values


def settings() -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / ".env"
    return {**{k: v for k, v in dotenv_values(path).items() if v is not None}, **os.environ}


def required(config: dict[str, str], key: str) -> str:
    value = config.get(key, "").strip()
    if not value:
        raise ValueError(f"Set {key} in backend/.env")
    return value
