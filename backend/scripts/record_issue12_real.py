"""Opt-in real retrieval/metadata/generation trace. Reads DB; never builds or resets it."""

import argparse
import asyncio
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import sys
import time

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def source_hashes():
    paths = ("app/contracts.py", "app/qa/prompts.py", "app/qa/citation.py", "app/qa/orchestrator.py",
             "app/qa/retriever.py", "app/adapters/embedding.py", "app/adapters/generation.py",
             "app/storage/repo.py", "scripts/record_issue12_real.py")
    return {name: hashlib.sha256((BACKEND / name).read_bytes()).hexdigest() for name in paths}


async def record(args):
    if args.output.exists():
        raise ValueError("Refuse to overwrite a prior run")
    if args.env_file:
        from dotenv import dotenv_values
        for key, value in dotenv_values(args.env_file).items():
            if value is not None and key.startswith(("GENERATION_", "EMBEDDING_", "RETRIEVAL_", "DATABASE_URL")):
                os.environ[key] = value
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url.startswith(("postgresql://", "postgresql+asyncpg://")):
        raise ValueError("Explicit PostgreSQL DATABASE_URL required; no default DB is used")
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy.pool import NullPool
    from app.database import _normalize_database_url
    from app.contracts import EvalQuestion
    from app.adapters.generation import generate_text
    from app.qa.orchestrator import answer
    from app.qa.retriever import retrieve
    from app.storage.repo import get_documents, search_similar

    raw = args.questions.read_bytes()
    cases = [EvalQuestion.model_validate(json.loads(line)) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    if len({case.qid for case in cases}) != len(cases):
        raise ValueError("Duplicate question IDs")
    if args.question_id:
        requested = set(args.question_id)
        cases = [case for case in cases if case.qid in requested]
        if {case.qid for case in cases} != requested:
            raise ValueError("Unknown question ID")
    if not cases:
        raise ValueError("Question set is empty")
    engine = create_async_engine(_normalize_database_url(url), poolclass=NullPool,
                                 connect_args={"server_settings": {"default_transaction_read_only": "on"}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    search = partial(search_similar, session_factory=sessions)
    lookup = partial(get_documents, session_factory=sessions)
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "mode": "real DB retrieval + real embedding + real generation",
              "source_sha256": source_hashes(), "question_sha256": hashlib.sha256(raw).hexdigest(),
              "subset": bool(args.question_id), "index_provenance": "UNVERIFIED: record index generation/version separately",
              "gold_evidence_sent_to_model": False, "questions": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        for case in cases:
            started = time.monotonic()
            row = {"id": case.qid, "question": case.question,
                   "expected_status": "ANSWERED" if case.answerable else "INSUFFICIENT_EVIDENCE",
                   "hits": [], "documents": {}, "human_key_points": None, "human_citation_support": None}
            async def retrieval(question):
                hits = await retrieve(question, search_similar=search)
                row["hits"] = [hit.model_dump() for hit in hits]
                return hits
            async def metadata(ids):
                documents = await lookup(ids)
                row["documents"] = {key: value.model_dump(mode="json") for key, value in documents.items()}
                return documents
            def generation(messages):
                value = generate_text(messages)
                row["raw_generation"] = value
                return value
            try:
                result = await answer(case.question, retrieve_fn=retrieval, get_documents_fn=metadata, generate_fn=generation)
                row["response"] = result.model_dump(mode="json")
                row["status_match"] = result.status.value == row["expected_status"]
            except Exception as error:
                row["error_type"] = type(error).__name__
                row["status_match"] = False
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)
            report["questions"].append(row)
            save()
            print(f"{case.qid}: {row.get('response', {}).get('status', row.get('error_type'))}", flush=True)
    finally:
        await engine.dispose()
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=BACKEND.parent / "eval/questions.jsonl")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--question-id", action="append", help="Diagnostic subset only; omit for full same-run evaluation")
    parser.add_argument("--run-real", action="store_true", help="Acknowledge real model inference and possible gateway charges")
    args = parser.parse_args()
    if not args.run_real:
        parser.error("Real service calls require --run-real; use a dedicated test DB")
    asyncio.run(record(args))


if __name__ == "__main__":
    main()
