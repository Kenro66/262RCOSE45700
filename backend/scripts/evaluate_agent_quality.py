"""Stage-four opt-in real LLM experiments and matched Pilot comparison.

Synthetic conflict sources are explicitly TEST ONLY and never enter the database.
Gold answers are recorded for review, never included in generation inputs.
"""

import argparse
import asyncio
import hashlib
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate_agent_real import audit_citations, safe_error, decision_state
from app.adapters.config import settings
from app.adapters.generation import generate_text
from app.contracts import SearchHit
from app.qa.agent import AgentError, agent_answer
from app.qa.agent.nodes import Dependencies, Nodes
from app.qa.agent.runtime import RealRAG
from app.qa.agent.state import ConflictDecision
from app.qa.citation import Evidence
from app.qa.orchestrator import answer

ROOT = Path(__file__).resolve().parents[2]


async def execute(case, arm, record, save, *, completion=True):
    started = time.perf_counter()
    record.update(arm=arm, model_calls=[], retrieval_calls=[], completion_enabled=completion)
    async with RealRAG(case["question"]) as runtime:
        async def retrieve(query):
            row = {"query": query}
            record["retrieval_calls"].append(row)
            t = time.perf_counter()
            try:
                hits = await runtime.retrieve(query)
                row["hits"] = [h.model_dump(mode="json") for h in hits]
                return hits
            finally:
                row["seconds"] = round(time.perf_counter() - t, 3)
                save()
        def generate(messages):
            data = json.loads(messages[-1]["content"])
            row = {"task": data.get("task", "single_generate")}
            record["model_calls"].append(row)
            t = time.perf_counter()
            try:
                raw = generate_text(messages)
                row["raw_output"] = raw
                try:
                    row["output"] = json.loads(raw)
                except ValueError:
                    row["format_error"] = True
                return raw
            except Exception as exc:
                row.update(**safe_error(exc))
                raise
            finally:
                row["seconds"] = round(time.perf_counter() - t, 3)
                save()
        deps = runtime.dependencies()
        deps.update(retrieve_fn=retrieve, generate_fn=generate,
                    complete_evidence_fn=runtime.complete if completion else None)
        try:
            result = await (agent_answer(case["question"], **deps) if arm == "agent" else answer(case["question"], **deps))
            response = result.response if arm == "agent" else result
            record.update(status=response.status.value, response=response.model_dump(mode="json"))
            if arm == "agent":
                record.update(state=result.state.model_dump(mode="json"), trace=result.trace,
                              retrieval_count=result.state.retrieval_count,
                              rewrite_count=result.trace.count("rewrite_query"),
                              clarification_count=result.state.clarification_count)
                record["citation_audit"] = audit_citations(response.citations, ROOT / "data", result.state.evidence)
            else:
                record.update(retrieval_count=len(record["retrieval_calls"]), rewrite_count=0, clarification_count=0)
                # Original citations are checked against original uploaded PDFs by their trusted filenames.
                docs = await runtime.documents(list(dict.fromkeys(h["document_id"] for r in record["retrieval_calls"] for h in r.get("hits", []))))
                sources = []
                for c in response.citations:
                    ids = [docid for docid, d in docs.items() if d.filename == c.filename]
                    if len(ids) == 1:
                        sources.append(Evidence(c.evidence_id, SearchHit(chunk_id="audit_"+c.evidence_id, document_id=ids[0],
                                              page_number=c.page, text=c.quote, score=1.0)))
                if len(sources) == len(response.citations):
                    record["citation_audit"] = audit_citations(response.citations, ROOT / "data", sources)
        except Exception as exc:
            record.update(status="TECHNICAL_ERROR", **safe_error(exc))
            if isinstance(exc, AgentError) and exc.state is not None:
                record.update(state=exc.state.model_dump(mode="json"), trace=exc.trace,
                              retrieval_count=exc.state.retrieval_count,
                              rewrite_count=exc.trace.count("rewrite_query"),
                              clarification_count=exc.state.clarification_count)
    record["seconds"] = round(time.perf_counter() - started, 3)
    save()


async def conflict_cases(record, save):
    scenarios = [("same_scope_conflict", "CN", "INBOUND"), ("different_country", "KR", "INBOUND"),
                 ("opposite_direction", "CN", "OUTBOUND"), ("unknown_scope", None, None)]
    nodes = Nodes(Dependencies(None, None, generate_text))
    for name, country, direction in scenarios:
        metadata = lambda index, c, d: dict(document_id=f"TEST{index}", filename=f"TEST_ONLY_{index}.pdf", status="READY",
                                           country=c, direction=d, effective_date="2026-01-01", applicable_persons=["GENERAL_TRAVELER"])
        a = "仅用于软件验收的虚构条款，不是真实政策：自2026年1月1日起，中国入境一般旅客允许携带测试物品X，允许量为每人一个。"
        b = "仅用于软件验收的虚构条款，不是真实政策：自2026年1月1日起，" + ("韩国" if country == "KR" else "中国") + ("出境" if direction == "OUTBOUND" else "入境") + "一般旅客禁止携带测试物品X，允许量为零。"
        if name == "unknown_scope":
            b = "仅用于软件验收的虚构条款，不是真实政策：本规定禁止携带测试物品X；国家、方向、生效时间和适用对象尚未登记，条款正文也未明确。"
        sources = [Evidence(f"E{i}", SearchHit(chunk_id=f"TEST_C{i}", document_id=f"TEST{i}", page_number=1, text=t, score=.9))
                   for i, t in enumerate((a,b), 1)]
        docs = {"TEST1": metadata(1,"CN","INBOUND"), "TEST2": metadata(2,country,direction)}
        if name == "unknown_scope":
            docs["TEST2"].update(effective_date=None, applicable_persons=None)
        state = dict(question="中国入境一般旅客在2026年7月能否携带测试物品X？", conditions={"destination_country":"CN","direction":"INBOUND"},
                     current_query="测试物品X", retrieval_count=1, evidence=sources, documents=docs, evidence_check_result={})
        row = {"scenario": name, "synthetic_test_only": True, "sources": [e.hit.model_dump(mode="json") for e in sources],
               "documents": docs}
        record["cases"].append(row)
        t = time.perf_counter()
        try:
            decision = await nodes.decide("check_conflict", state, ConflictDecision)
            row["llm_decision"] = decision.model_dump(mode="json")
            async def fixed_decision(*args):
                return decision
            # Run the production deterministic guards on exactly this real LLM output.
            original = nodes.decide
            nodes.decide = fixed_decision
            try:
                guarded = await nodes.check_conflict(state)
            finally:
                nodes.decide = original
            row["guarded_result"] = {k: v.model_dump(mode="json") if hasattr(v,"model_dump") else v for k,v in guarded.items()}
            expected = name == "same_scope_conflict"
            row["expected_conflict"] = expected
            row["passed"] = ((decision.conflict is not None) == expected
                             and (guarded.get("status") == "CONFLICTING_EVIDENCE") == expected)
            row["status"] = "VERIFIED" if row["passed"] else "CHECK_FAILED"
        except Exception as exc:
            row.update(status="TECHNICAL_ERROR", **safe_error(exc))
        row["seconds"] = round(time.perf_counter()-t,3)
        save()
        print(json.dumps({"scenario":name,"status":row["status"]}),flush=True)


async def definition_cases(args, record, save):
    prior = json.loads(Path(args.retrieval_input).read_text(encoding="utf-8"))
    for case in prior["cases"]:
        if case["qid"] not in args.cases:
            continue
        for initial_only in (True, True, False):
            state = decision_state(case, case)
            if initial_only:
                ids = {h["chunk_id"] for h in case["hits"]}
                state["evidence"] = [e for e in state["evidence"] if e.hit.chunk_id in ids]
            row = {"qid":case["qid"],"initial_only":initial_only,"source_ids":[e.hit.chunk_id for e in state["evidence"]]}
            record["cases"].append(row)
            def generate(messages):
                raw = generate_text(messages)
                row["raw_llm_output"] = raw
                return raw
            async with RealRAG(case["question"]) as runtime:
                t = time.perf_counter()
                try:
                    nodes = Nodes(Dependencies(runtime.retrieve,runtime.documents,generate))
                    result = await nodes.check_evidence(state)
                    row.update(status="VERIFIED",workflow_sufficient=result["sufficient"],
                               assessment=result["evidence_check_result"])
                except Exception as exc:
                    row.update(status="TECHNICAL_ERROR",**safe_error(exc))
                row["seconds"] = round(time.perf_counter()-t,3)
            save()
            print(json.dumps({"qid":case["qid"],"initial_only":initial_only,"status":row["status"],
                              "accepted":row.get("workflow_sufficient")}),flush=True)


async def main(args):
    path = Path(args.output).resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    config = settings()
    record = dict(experiment=args.mode, started_at_utc=datetime.now(timezone.utc).isoformat(), model=config.get("GENERATION_MODEL"),
                  embedding_model=config.get("EMBEDDING_MODEL"), top_k=config.get("RETRIEVAL_TOP_K","5"), cases=[])
    record["source_sha256"] = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in (ROOT/"backend/app/qa/agent").glob("*.py")}
    def save():
        path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
    save()
    if args.mode == "definitions":
        await definition_cases(args,record,save)
        return
    if args.mode == "conflicts":
        await conflict_cases(record,save)
        return
    fixture = ROOT / "eval" / ("questions.jsonl" if args.mode == "rewrite" else "pilot43_questions_v1_40.jsonl")
    cases = [json.loads(line) for line in fixture.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    # Warm-up outside measured arms; avoids charging only the first arm for CPU model load.
    from app.adapters.embedding import embed_texts
    await asyncio.to_thread(embed_texts,["模型预热"])
    record["embedding_warmed_before_timing"] = True
    for index, case in enumerate(c for c in cases if c["qid"] in args.cases):
        row = {"qid":case["qid"],"question":case["question"],"gold_answer":case.get("gold_answer"),
               "answerable":case["answerable"],"gold_evidence":case.get("gold_evidence",[]),"runs":[]}
        record["cases"].append(row)
        arms = ["agent"] if args.mode == "rewrite" else (["agent","single"] if index%2==0 else ["single","agent"])
        for arm in arms:
            run = {}
            row["runs"].append(run)
            await execute(case,arm,run,save,completion=args.mode != "rewrite")
            print(json.dumps({"qid":case["qid"],"arm":arm,"status":run.get("status"),"retrieval":run.get("retrieval_count"),"seconds":run.get("seconds")}),flush=True)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    sys.stdout.reconfigure(encoding="utf-8")
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode",choices=("rewrite","conflicts","pilot","definitions"),required=True)
    parser.add_argument("--output",required=True)
    parser.add_argument("--cases",nargs="+",default=["Q003"])
    parser.add_argument("--retrieval-input")
    options = parser.parse_args()
    if options.mode == "definitions" and not options.retrieval_input:
        parser.error("definitions requires --retrieval-input")
    asyncio.run(main(options))
