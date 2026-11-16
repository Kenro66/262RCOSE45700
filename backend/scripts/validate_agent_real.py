"""Explicit read-only real-service validation; outputs never include credentials.

Run inventory/retrieval before decisions/full. No document ingestion or DB writes.
Decision probes reuse retrieved evidence; gold answers are never sent to the LLM.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.adapters.config import settings
from app.adapters.generation import GenerationError, generate_text
from app.qa.agent import AgentError, agent_answer
from app.qa.agent.nodes import Dependencies, Nodes
from app.qa.agent.runtime import RealRAG
from app.qa.agent.state import (AnswerDecision, ConditionsDecision, ConflictDecision,
                                EvidenceDecision, RewriteDecision)
from app.qa.citation import Evidence, assemble_evidence, resolve_citations


def safe_error(exc):
    chain = []
    current = exc
    while current is not None and len(chain) < 5:
        chain.append(type(current).__name__)
        current = current.__cause__
    result = {"error_types": chain}
    if isinstance(exc, GenerationError):
        # Existing adapter emits sanitized messages, never raw HTTP bodies/headers.
        result["message"] = str(exc)
    return result


def audit_citations(citations, data_dir, evidence):
    import pymupdf
    import re
    def normalized(text):
        return re.sub(r"\s+", "", text)
    checks = []
    sources = {e.evidence_id: e.hit for e in evidence}
    for citation in citations:
        source = sources[citation.evidence_id]
        upload = BACKEND / "uploads" / f"{source.document_id}.pdf"
        path = upload if upload.is_file() else data_dir / citation.filename
        row = {"evidence_id": citation.evidence_id, "filename": citation.filename,
               "page": citation.page, "pdf_exists": path.is_file()}
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            row.update(pdf_path=str(path), sha256=digest,
                       identical_repository_pdfs=[p.name for p in data_dir.glob("*.pdf")
                                                   if hashlib.sha256(p.read_bytes()).hexdigest() == digest])
            with pymupdf.open(path) as pdf:
                row["page_exists"] = 1 <= citation.page <= len(pdf)
                row["quote_on_pdf_page"] = row["page_exists"] and normalized(citation.quote) in normalized(pdf[citation.page - 1].get_text())
        checks.append(row)
    return checks


async def inventory(record):
    from sqlalchemy import text
    async with RealRAG("inventory") as runtime:
        async with runtime.session_factory() as session:
            rows = (await session.execute(text("SELECT document_id,filename,status FROM documents ORDER BY document_id"))).mappings().all()
            count = (await session.execute(text("SELECT count(*) FROM chunks"))).scalar()
            readonly = (await session.execute(text("SHOW transaction_read_only"))).scalar()
        record.update(database="available", documents=[dict(r) for r in rows], chunks=count, transaction_read_only=readonly)


async def retrieve_case(case, record):
    started = time.perf_counter()
    async with RealRAG(case["question"]) as runtime:
        t = time.perf_counter()
        hits = await runtime.retrieve(case["question"])
        record["retrieval_seconds"] = round(time.perf_counter() - t, 3)
        record["hits"] = [h.model_dump(mode="json") for h in hits]
        t = time.perf_counter()
        supplemental = await runtime.complete(hits)
        record["completion_seconds"] = round(time.perf_counter() - t, 3)
        record["supplemental"] = [s.chunk.model_dump(mode="json") for s in supplemental]
        evidence = assemble_evidence(hits, supplemental)
        documents = await runtime.documents(list(dict.fromkeys(e.hit.document_id for e in evidence)))
        record["documents"] = {k: v.model_dump(mode="json") if hasattr(v, "model_dump") else v for k, v in documents.items()}
        _, citations = resolve_citations("".join(f"[{e.evidence_id}]" for e in evidence), evidence, documents)
        record["citations"] = [c.model_dump(mode="json") for c in citations]
        record["citation_audit"] = audit_citations(citations, BACKEND.parent / "data", evidence)
        record["evidence"] = [{"evidence_id": e.evidence_id, "hit": e.hit.model_dump(mode="json")} for e in evidence]
        record["status"] = "RETRIEVAL_VERIFIED" if citations and all(
            c.get("quote_on_pdf_page") for c in record["citation_audit"]) else "CITATION_AUDIT_FAILED"
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)


def decision_state(case, retrieval):
    from app.contracts import SearchHit, Chunk
    sources = []
    for row in retrieval["evidence"]:
        model = SearchHit if "score" in row["hit"] else Chunk
        sources.append(Evidence(row["evidence_id"], model.model_validate(row["hit"])))
    return dict(question=case["question"], session_id=None, question_id=case["qid"], conditions={},
                current_query=case["question"], retrieval_count=1, clarification_count=0,
                evidence=sources, documents=retrieval["documents"], sufficient=False,
                missing_conditions=[], status=None, answer=None, clarification=None, conflict=None, trace=[])


async def decisions_case(case, record, retrieval, save, selected_tasks=None, initial_only=False):
    nodes = Nodes(Dependencies(None, None, generate_text))
    state = decision_state(case, retrieval)
    if initial_only:
        initial_ids = {h["chunk_id"] for h in retrieval["hits"]}
        state["evidence"] = [e for e in state["evidence"] if e.hit.chunk_id in initial_ids]
    record["evidence_mode"] = "initial_only_ablation" if initial_only else "retrieval_plus_completion"
    tasks = [("check_conditions", ConditionsDecision), ("check_evidence", EvidenceDecision),
             ("rewrite_query", RewriteDecision), ("check_applicability", ConditionsDecision),
             ("check_conflict", ConflictDecision), ("generate_answer", AnswerDecision)]
    record["tasks"] = []
    for task, schema in tasks:
        if selected_tasks and task not in selected_tasks:
            continue
        row = {"task": task}
        started = time.perf_counter()
        try:
            decision = await nodes.decide(task, state, schema)
            row.update(status="JSON_VALID", output=decision.model_dump(mode="json"))
        except Exception as exc:
            row.update(status="FAILED", **safe_error(exc))
        row["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        record["tasks"].append(row)
        save()
        # A transport/config outage affects every task; stop without repeated calls.
        if row["status"] == "FAILED" and "GenerationError" in row.get("error_types", []):
            record["remaining_tasks"] = "NOT_RUN_SERVICE_UNAVAILABLE"
            break


async def single_case(case, record):
    from app.qa.orchestrator import answer
    started = time.perf_counter()
    async with RealRAG(case["question"]) as runtime:
        result = await answer(case["question"], **runtime.dependencies())
        record.update(status=result.status.value, response=result.model_dump(mode="json"),
                      retrieval_count=1, rewrite_count=0)
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)


async def full_case(case, record, save):
    started = time.perf_counter()
    record["model_calls"] = []
    record["retrieval_calls"] = []
    async with RealRAG(case["question"]) as runtime:
        async def retrieve(query):
            item = {"query": query}
            record["retrieval_calls"].append(item)
            t = time.perf_counter()
            try:
                hits = await runtime.retrieve(query)
                item["chunk_ids"] = [h.chunk_id for h in hits]
                return hits
            finally:
                item["elapsed_seconds"] = round(time.perf_counter() - t, 3)
                save()
        def generate(messages):
            data = json.loads(messages[-1]["content"])
            item = {"task": data["task"]}
            record["model_calls"].append(item)
            t = time.perf_counter()
            try:
                raw = generate_text(messages)
                # Policy output only; never request configuration or HTTP metadata.
                item["output"] = raw
                return raw
            except Exception as exc:
                item.update(**safe_error(exc))
                raise
            finally:
                item["elapsed_seconds"] = round(time.perf_counter() - t, 3)
                save()
        deps = runtime.dependencies()
        deps.update(retrieve_fn=retrieve, generate_fn=generate)
        try:
            result = await agent_answer(case["question"], **deps)
            record.update(status=result.response.status.value, result=result.model_dump(mode="json"),
                          retrieval_count=result.state.retrieval_count, rewrite_count=result.trace.count("rewrite_query"))
            record["citation_audit"] = audit_citations(result.response.citations, BACKEND.parent / "data", result.state.evidence)
        except Exception as exc:
            record.update(status="TECHNICAL_ERROR", **safe_error(exc))
            if isinstance(exc, AgentError) and exc.state is not None:
                record.update(state=exc.state.model_dump(mode="json"), trace=exc.trace,
                              retrieval_count=exc.state.retrieval_count,
                              rewrite_count=exc.trace.count("rewrite_query"))
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)


async def main(args):
    config = settings()
    url = urlsplit(config.get("GENERATION_BASE_URL", ""))
    record = {"phase": args.phase, "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "model": config.get("GENERATION_MODEL"),
              "generation_endpoint": f"{url.scheme}://{url.netloc.split('@')[-1]}{url.path}",
              "embedding_model": config.get("EMBEDDING_MODEL"), "cases": []}
    path = Path(args.output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    def save():
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    if args.phase == "inventory":
        try:
            await inventory(record)
        except Exception as exc:
            record.update(database="unavailable", **safe_error(exc))
        save()
        print(json.dumps({k: v for k, v in record.items() if k != "cases"}, ensure_ascii=False))
        return
    cases = [json.loads(line) for line in (BACKEND.parent / "eval/questions.jsonl").read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    cached = json.loads(Path(args.retrieval_input).read_text(encoding="utf-8")) if args.retrieval_input else None
    for case in cases:
        if case["qid"] not in args.cases:
            continue
        row = {"qid": case["qid"], "question": case["question"]}
        record["cases"].append(row)
        try:
            if args.phase == "audit":
                from app.contracts import Citation
                prior = next(c for c in cached["cases"] if c["qid"] == case["qid"])
                row.update(prior)
                sources = decision_state(case, prior)["evidence"]
                row["citation_audit"] = audit_citations([Citation.model_validate(c) for c in prior["citations"]],
                                                         BACKEND.parent / "data", sources)
                row["status"] = "RETRIEVAL_VERIFIED" if row["citations"] and all(
                    c.get("quote_on_pdf_page") for c in row["citation_audit"]) else "CITATION_AUDIT_FAILED"
            elif args.phase == "retrieval":
                await retrieve_case(case, row)
            elif args.phase == "decisions":
                prior = next(c for c in cached["cases"] if c["qid"] == case["qid"])
                await decisions_case(case, row, prior, save, args.tasks, args.initial_only)
            elif args.phase == "single":
                await single_case(case, row)
            else:
                await full_case(case, row, save)
        except Exception as exc:
            row.update(status="TECHNICAL_ERROR", **safe_error(exc))
        save()
        print(json.dumps({k: v for k, v in row.items() if k in ("qid", "status", "elapsed_seconds", "retrieval_count", "rewrite_count", "error_types")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    # Only sanitized JSON is persisted. Do not print dependency tracebacks or HTTP bodies.
    logging.disable(logging.CRITICAL)
    sys.stdout.reconfigure(encoding="utf-8")
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("inventory", "retrieval", "audit", "decisions", "full", "single"), required=True)
    parser.add_argument("--cases", nargs="+", default=["Q003", "Q008"])
    parser.add_argument("--output", required=True)
    parser.add_argument("--retrieval-input")
    parser.add_argument("--tasks", nargs="+", choices=("check_conditions", "check_evidence", "rewrite_query",
                                                       "check_applicability", "check_conflict", "generate_answer"))
    parser.add_argument("--initial-only", action="store_true", help="Explicit evidence ablation for decision probes")
    options = parser.parse_args()
    if options.phase in ("decisions", "audit") and not options.retrieval_input:
        parser.error("decisions/audit requires --retrieval-input")
    asyncio.run(main(options))
