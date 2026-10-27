"""Issue #5: run real adapters inside a FastAPI request, in one process.

Run from backend/: python smoke_fastapi.py
The diagnostic route exists only in this script; production app.main is unchanged.
"""

import json
import math
import os
import platform
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version

from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

PACKAGES = (
    "fastapi", "starlette", "uvicorn", "asyncpg", "pydantic", "httpx",
    "python-dotenv", "torch", "FlagEmbedding", "transformers",
    "sentence-transformers", "sentencepiece", "peft",
)


def versions():
    result = {"python": platform.python_version()}
    for package in PACKAGES:
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = "NOT_INSTALLED"
    return result


def create_probe_app():
    # Import the actual backend app, not a replacement FastAPI application.
    from app.main import app
    from app.adapters.embedding import embed_texts
    from app.adapters.generation import generate_text

    def probe():
        report = {
            "passed": False,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "versions": versions(),
            "embedding": {},
            "generation": {},
        }
        stage = "embedding"
        try:
            vectors = embed_texts(["请说明什么是检索增强生成。", "검색 증강 생성은 무엇인가요?"])
            if not isinstance(vectors, list) or len(vectors) != 2:
                raise ValueError("Expected two vectors")
            for vector in vectors:
                if len(vector) != 1024 or any(
                    isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                    for x in vector
                ) or not any(x != 0 for x in vector):
                    raise ValueError("Expected finite nonzero 1024-dimensional vectors")
            report[stage] = {"passed": True, "pid": os.getpid(), "count": 2, "dimensions": [len(v) for v in vectors]}
            stage = "generation"
            text = generate_text("What is retrieval augmented generation? Reply in one short sentence.")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Expected non-empty generated text")
            report[stage] = {"passed": True, "pid": os.getpid(), "text_characters": len(text)}
            report["passed"] = True
        except Exception as exc:
            # Do not print exception messages/tracebacks, service responses, keys,
            # or generated content. Third-party exceptions can contain credentials.
            report[stage] = {"passed": False, "pid": os.getpid(), "error_type": type(exc).__name__}
            report[stage]["next_step"] = (
                "Check EMBEDDING_MODEL, CPU dependencies and local model/download access"
                if stage == "embedding" else
                "Check GENERATION_MODEL, GENERATION_BASE_URL and local credentials/service availability"
            )
            report["failed_stage"] = stage
        return JSONResponse(report, status_code=200 if report["passed"] else 503)

    # Avoid registering duplicate routes if invoked repeatedly by tests.
    path = "/_compat/rag"
    if any(getattr(route, "path", None) == path for route in app.routes):
        raise RuntimeError("Compatibility route already registered")
    app.add_api_route(path, probe, methods=["POST"], include_in_schema=False)
    return app


def main():
    try:
        app = create_probe_app()
        with TestClient(app) as client:
            response = client.post("/_compat/rag")
        report = response.json()
        report["driver_pid"] = os.getpid()
        report["same_process"] = all(
            report.get(stage, {}).get("pid") == os.getpid()
            for stage in ("embedding", "generation")
        )
        report["passed"] = response.status_code == 200 and report["passed"] and report["same_process"]
    except Exception as exc:
        report = {"passed": False, "failed_stage": "startup", "error_type": type(exc).__name__, "versions": versions()}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
