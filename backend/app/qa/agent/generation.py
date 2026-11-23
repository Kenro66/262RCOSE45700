"""Agent native schemas; the optional shared profile unifies all providers."""

import json
from collections.abc import Callable
from math import isfinite

from pydantic import BaseModel

from ...adapters.generation import GenerationError, generate_text
from ...adapters.config import generation_settings
from .conditions import NEW_FIELD_PATTERN
from .state import (AnswerDecision, ConditionsDecision, ConflictDecision,
                    EvidenceDecision, RewriteDecision)


def agent_generator(config: dict[str, str], default: Callable) -> Callable:
    """Build one request-local callable without mutating process configuration."""
    try:
        config = generation_settings(config)
    except ValueError as exc:
        raise GenerationError(str(exc)) from None
    enabled = config.get("AGENT_GENERATION_JSON_SCHEMA_ENABLED", "false").strip().lower()
    if enabled not in ("true", "false"):
        raise GenerationError("AGENT_GENERATION_JSON_SCHEMA_ENABLED must be true or false")
    maximum = config.get("AGENT_GENERATION_MAX_TOKENS", "").strip()
    temp = config.get("AGENT_GENERATION_TEMPERATURE", "").strip()
    try:
        max_tokens = int(maximum) if maximum else None
        temperature = float(temp) if temp else None
        if max_tokens is not None and max_tokens <= 0:
            raise ValueError
        if temperature is not None and (not isfinite(temperature) or not 0 <= temperature <= 2):
            raise ValueError
    except ValueError:
        raise GenerationError("Invalid Agent generation token limit or temperature") from None

    # Provider overrides are atomic: never mix a Gemma endpoint with a school key.
    keys = ("MODEL", "BASE_URL", "API_KEY", "TIMEOUT_SECONDS")
    override = any(config.get("AGENT_GENERATION_" + key, "").strip() for key in keys)
    provider = None
    if override:
        if any(not config.get("AGENT_GENERATION_" + key, "").strip() for key in ("MODEL", "BASE_URL")):
            raise GenerationError("Agent provider requires both MODEL and BASE_URL")
        provider = {"GENERATION_" + key: config.get("AGENT_GENERATION_" + key, "") for key in keys}
        provider["GENERATION_TIMEOUT_SECONDS"] = provider["GENERATION_TIMEOUT_SECONDS"].strip() or "60"
    if enabled == "false" and max_tokens is None and temperature is None and provider is None:
        return default

    models: dict[str, type[BaseModel]] = {"check_conditions": ConditionsDecision, "check_applicability": ConditionsDecision,
              "check_evidence": EvidenceDecision, "rewrite_query": RewriteDecision,
              "check_conflict": ConflictDecision, "generate_answer": AnswerDecision}

    def generate(messages):
        response_format = None
        if enabled == "true":
            # Only parse the trusted node envelope, never instructions inside source text.
            try:
                data = json.loads(messages[-1]["content"])
                task = data["task"]
                schema = models[task].model_json_schema()
                evidence = data["evidence"]
                chunks = list(dict.fromkeys(item["source"]["chunk_id"] for item in evidence))
                evidence_ids = list(dict.fromkeys(item["evidence_id"] for item in evidence))
            except (ValueError, KeyError, TypeError, IndexError):
                raise GenerationError("Structured generation requires a valid Agent task envelope") from None
            if task in ("check_conditions", "check_applicability"):
                question = schema["$defs"]["ClarificationQuestion"]
                question["properties"]["field"]["pattern"] = NEW_FIELD_PATTERN
                question["required"] = list(dict.fromkeys([*question["required"], "type", "options"]))
                for name in ("type", "options"):
                    question["properties"][name].pop("default", None)
            if task == "check_evidence":
                if chunks:
                    schema["$defs"]["EvidenceSupport"]["properties"]["chunk_id"]["enum"] = chunks
                else:
                    schema["properties"]["core_support"]["maxItems"] = 0
                    schema["$defs"]["EvidenceCoverage"]["properties"]["support"]["maxItems"] = 0
            if task == "check_conflict":
                if evidence_ids:
                    for side in ("side_a", "side_b"):
                        schema["$defs"]["Conflict"]["properties"][side]["items"]["enum"] = evidence_ids
                else:
                    schema["properties"]["conflict"] = {"type": "null"}
            response_format = {"type": "json_schema", "json_schema": {
                "name": task, "strict": True, "schema": schema}}
        return generate_text(messages, response_format=response_format, max_tokens=max_tokens,
                             temperature=temperature, provider_config=provider)

    return generate
