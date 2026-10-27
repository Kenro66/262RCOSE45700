"""Real generation with synthetic retrieval/metadata; no database or secret output."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.adapters.config import settings
from app.adapters.generation import generate_text
from app.contracts import ChatStatus
from app.qa.orchestrator import answer


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, help="Optional existing private config; never copied or printed")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.env_file:
        if not args.env_file.is_file():
            raise ValueError("The specified private configuration file does not exist")
        private = dotenv_values(args.env_file)
        for name in ("GENERATION_MODEL", "GENERATION_BASE_URL", "GENERATION_API_KEY", "GENERATION_TIMEOUT_SECONDS"):
            if private.get(name) and not os.environ.get(name):
                os.environ[name] = private[name]
    config = settings()
    if not all(config.get(name, "").strip() for name in ("GENERATION_MODEL", "GENERATION_BASE_URL")):
        print("SKIP: existing generation configuration is unavailable")
        return 2
    report = {"real_generation": True, "retrieval": "synthetic fixture", "documents": "synthetic fixture",
              "real_database_integration": False, "cases": []}
    cases = [
        ("zh", "根据这份测试资料，携带品应如何处理？", "合成测试规定：旅客应当主动申报携带品。"),
        ("ko", "이 테스트 자료에 따르면 휴대품을 어떻게 처리해야 하나요?", "합성 테스트 규정: 여행자는 휴대품을 자진 신고해야 합니다."),
    ]
    for language, question, text in cases:
        case = {"language": language, "question": question, "synthetic_evidence": text}
        async def retrieve(question):
            return [{"chunk_id": "SMOKE_C001", "document_id": "SMOKE", "page_number": 1, "text": text, "score": .9}]
        async def documents(ids):
            return {"SMOKE": {"filename": "synthetic-policy.pdf", "status": "READY"}}
        def generate(messages):
            result = generate_text(messages)
            case["raw_generation"] = result
            return result
        try:
            result = await answer(question, retrieve_fn=retrieve, get_documents_fn=documents, generate_fn=generate)
            assert result.status == ChatStatus.ANSWERED
            assert len(result.citations) == 1 and result.citations[0].quote == text
            assert result.citations[0].filename == "synthetic-policy.pdf" and result.citations[0].page == 1
            case.update(passed=True, response=result.model_dump(mode="json"))
        except Exception as exc:
            # Diagnostic collection only: failures remain failures, never evidence insufficiency.
            case.update(passed=False, error_type=type(exc).__name__)
        report["cases"].append(case)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Real generation {language}: {'PASS' if case['passed'] else 'FAIL (' + case['error_type'] + ')'}", flush=True)
    return 0 if all(case["passed"] for case in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
