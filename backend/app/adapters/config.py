"""Read backend/.env on each call; process environment takes precedence."""

import os
from pathlib import Path

from dotenv import dotenv_values

GEMMA_MODEL = "furiosa-ai/gemma-4-31B-it-FP8-block"
GEMMA_BASE_URL = "https://endpoint.access.furiosa.dev/v1"


def settings() -> dict[str, str]:
    path = Path(__file__).resolve().parents[2] / ".env"
    return {**{k: v for k, v in dotenv_values(path).items() if v is not None}, **os.environ}


def required(config: dict[str, str], key: str) -> str:
    value = config.get(key, "").strip()
    if not value:
        raise ValueError(f"Set {key} in backend/.env")
    return value


def generation_settings(config: dict[str, str]) -> dict[str, str]:
    """Resolve an explicit generation-only profile; never inherit another key.

    Raw settings and embedding/reranker configuration remain unchanged. Legacy
    providers stay available when the profile is disabled, not as auto fallback.
    """
    profile = config.get("GENERATION_PROFILE", "legacy").strip().lower()
    if profile == "legacy":
        return dict(config)
    if profile != "furiosa_gemma":
        raise ValueError("GENERATION_PROFILE must be legacy or furiosa_gemma")
    key = config.get("FURIOSA_API_KEY", "")
    if not key:
        path = Path(config.get("FURIOSA_API_KEY_FILE", "").strip()
                    or Path(__file__).resolve().parents[2] / ".secrets/furiosa.env")
        try:
            key = dotenv_values(path).get("FURIOSA_API_KEY", "") or ""
        except (OSError, UnicodeError):
            raise ValueError("Cannot read the configured Furiosa credential file") from None
    if not key or not key.isascii() or any(character.isspace() for character in key):
        raise ValueError("Configure a valid Furiosa credential; legacy credentials are never reused")
    result = dict(config)
    provider = {"MODEL": GEMMA_MODEL, "BASE_URL": GEMMA_BASE_URL, "API_KEY": key,
                "TIMEOUT_SECONDS": config.get("FURIOSA_GENERATION_TIMEOUT_SECONDS", "60")}
    for suffix, value in provider.items():
        result["GENERATION_" + suffix] = result["AGENT_GENERATION_" + suffix] = value
    for suffix, default in (("MAX_TOKENS", "2048"), ("TEMPERATURE", "0.6")):
        value = config.get("GENERATION_" + suffix, "").strip() or default
        result["GENERATION_" + suffix] = result["AGENT_GENERATION_" + suffix] = value
    result["AGENT_GENERATION_JSON_SCHEMA_ENABLED"] = "true"
    return result
