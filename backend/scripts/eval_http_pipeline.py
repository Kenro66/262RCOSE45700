#!/usr/bin/env python3
"""Pilot-stage HTTP evaluator for the COSE457 Chinese/Korean customs RAG.

Safety/contract boundaries:
- --check-only never contacts any server.
- --run-real and a small explicit case selection are required for HTTP calls.
- Reads fixed gold labels locally but never includes them in HTTP requests.
- At most one follow-up, using only explicitly reviewed Pilot input facts.
- Unknown fields/options are recorded as ADAPTER_UNMAPPED; never guess.
- No automatic retry or overwrite; transport/API failures are recorded.
- Python standard library only (works with Python 3.12+).
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

ACTIVE = {"RUNNING", "CANCELLING"}
SUCCESS_HTTP = {200, 202}
PILOT_SHA256 = "0f1ef047c6491e9ec25a8c8bed463d364825e8eb02129801c98e38426f431a29"
PIPELINE_VERSION = "4.0-pilot-r02-guarded"
from condition_adapter import adapt, ALIASES

# Exact r02 source files; do not regenerate or alter frozen inputs.
ADAPTER_SHA256 = "eeb876e55407bc8d7ec2621d540be081d6983c10a7adeef916df2b488214fab1"
FACTS_SHA256 = "b55d34e21322ee59c02f56c853e0f3de2d42663060b74ee759fa018b19a05e6e"
HERE = Path(__file__).resolve().parent


def load_facts():
    for name, expected in (("condition_adapter.py", ADAPTER_SHA256),
                           ("clarification-facts.json", FACTS_SHA256)):
        if hashlib.sha256((HERE / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"r02 frozen source hash mismatch: {name}")
    rows = json.loads((HERE / "clarification-facts.json").read_text(encoding="utf-8"))
    if set(rows) != {f"P{i:03d}" for i in range(1, 41)}:
        raise ValueError("r02 facts qid set mismatch")
    for qid, entry in rows.items():
        facts, provenance = entry["facts"], entry["fact_provenance"]
        if (entry.get("gold_answer_used") is not False or set(facts) != set(provenance)
            or any(not isinstance(v, str) or not v.strip() for v in facts.values())
            or any(not isinstance(v, str) or not v.strip() for v in provenance.values())):
            raise ValueError(f"r02 facts contract invalid: {qid}")
    return rows


R02_FACTS = load_facts()
# Additional prospective guard, NOT original r02 behavior. Pending joint approval.
# Keep the exact original facts and log a block instead of silently rewriting them.
REVIEW_HOLDS = {
    "P015": {"item_name": "Original topic is agricultural products and medicinal materials, not just 한약."},
    "P023": {"item_name": "Question specifies cigars rather than cigarettes; frozen fact is 香烟."},
    "P026": {"item_name": "Question asks pods/liquid; frozen fact also includes devices."},
    "P034": {"traveler_status": "Definition question does not establish user's traveler identity."},
}


def matches_input(case, entry):
    return (case.get("question") == entry["source_input_text"]["question"]
            and case.get("conditions") == entry["source_input_text"]["conditions"])


def map_conditions(case: dict, clarification: Any) -> tuple[dict | None, dict]:
    entry = R02_FACTS.get(case.get("qid"))
    questions = clarification.get("questions") if isinstance(clarification, dict) else None
    audit = {"status": "ADAPTER_UNMAPPED", "source": "r02_frozen_input_facts_guarded",
             "requested_fields": [], "submitted_conditions": None, "reason": None,
             "decisions": [], "unmapped": []}
    if not isinstance(questions, list) or not questions:
        audit["reason"] = "MISSING_CLARIFICATION_QUESTIONS"
        return None, audit
    audit["requested_fields"] = [q.get("field") if isinstance(q, dict) else None for q in questions]
    if not entry or not matches_input(case, entry):
        audit["reason"] = "NO_MATCHING_FROZEN_INPUT_FACTS"
        audit["unmapped"] = [{"question": q, "reason": audit["reason"]} for q in questions]
        return None, audit
    valid, seen = [], set()
    for q in questions:
        field = q.get("field") if isinstance(q, dict) else None
        kind = q.get("type") if isinstance(q, dict) else None
        options = q.get("options", []) if isinstance(q, dict) else None
        if (not isinstance(field, str) or not field or field in seen or kind not in {"select", "text"}
            or (kind == "select" and (not isinstance(options,list) or not options or any(not isinstance(v,str) for v in options)))
            or (kind == "text" and options not in (None, []))):
            audit["unmapped"].append({"question": q, "reason": "INVALID_OR_DUPLICATE_CLARIFICATION_FIELD"})
            continue
        seen.add(field)
        key = next((k for k in [field] + ALIASES.get(field, []) if k in entry["facts"]), None)
        hold = REVIEW_HOLDS.get(case["qid"], {}).get(key)
        if hold:
            audit["unmapped"].append({"question": q, "fact_key": key, "fact_value": entry["facts"][key],
                                       "reason": "PENDING_FACT_REVIEW: " + hold})
            continue
        valid.append(q)
    values, decisions, missing = adapt(valid, entry["facts"], entry["fact_provenance"])
    audit["decisions"] = decisions
    audit["unmapped"].extend(missing)
    if audit["unmapped"]:
        audit["reason"] = "ADAPTER_UNMAPPED"
        return None, audit
    audit.update(status="MAPPED", submitted_conditions=values, reason=None)
    return values, audit


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_questions(file: Path) -> tuple[list[dict[str, Any]], str]:
    content = file.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    data: list[dict[str, Any]] = []
    ids: set[str] = set()
    for line_no, line in enumerate(content.decode("utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"题库第 {line_no} 行不是合法 JSON: {e}") from e
        if not isinstance(item, dict):
            raise ValueError(f"题库第 {line_no} 行不是 JSON 对象")
        qid, question = item.get("qid"), item.get("question")
        if not isinstance(qid, str) or not qid.strip():
            raise ValueError(f"第 {line_no} 行缺少有效 qid")
        if qid in ids:
            raise ValueError(f"存在重复 qid: {qid}")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"第 {line_no} 行缺少有效 question")
        ids.add(qid)
        data.append(item)
    if not data:
        raise ValueError("题库为空")
    return data, sha


def http_json(method: str, url: str, payload: dict | None, timeout: float) -> tuple[int, Any]:
    """Return HTTP error JSON as data, not as a retryable exception."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = Request(url, data=body, method=method, headers={
        "Accept": "application/json", "Content-Type": "application/json"
    })
    try:
        with urlopen(req, timeout=timeout) as r:
            code = r.status
            raw = r.read()
    except HTTPError as e:
        code = e.code
        raw = e.read()
    except (URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"HTTP_TRANSPORT_ERROR: {type(e).__name__}: {e}") from e
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        # Preserve body for diagnostics; never represent it as a successful answer.
        obj = {"non_json_body": raw.decode("utf-8", errors="replace")[:20000]}
    return code, obj


def get_git_sha(root: Path) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=3, check=True)
        value = p.stdout.strip()
        return value if len(value) == 40 else None
    except (OSError, subprocess.SubprocessError):
        return None


def session_counts(base: str, sid: str, qid: str, timeout: float) -> tuple[int | None, int | None, Any, str | None]:
    url = f"{base}/api/chat/sessions/{quote(sid, safe='')}"
    try:
        code, raw = http_json("GET", url, None, timeout)
        if code != 200 or not isinstance(raw, dict):
            return None, None, raw, f"SESSION_DETAIL_HTTP_{code}"
        questions = raw.get("questions", [])
        found = next((q for q in questions if isinstance(q, dict) and q.get("question_id") == qid), None)
        if found is None:
            return None, None, raw, "QUESTION_NOT_IN_SESSION_DETAIL"
        return found.get("retrieval_count"), found.get("clarification_count"), raw, None
    except RuntimeError as e:
        return None, None, None, str(e)


def extract_answer(body: Any) -> dict | None:
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("response"), dict):
        return body["response"]
    if isinstance(body.get("status"), str):
        return body
    return None


def run_turn(base: str, mode: str, question: str,
             req_timeout: float, poll_seconds: float, poll_timeout: float,
             *, previous: dict | None = None, conditions: dict | None = None) -> dict:
    """One HTTP turn; second turn must retain the original IDs and message."""
    sid = previous["session_id"] if previous is not None else "s_eval_" + uuid4().hex
    expected_qid = previous["question_id"] if previous is not None else None
    started = time.monotonic()
    http_trace: list[dict] = []
    first = {"session_id": sid, "message": question}  # NEVER pass gold answer/evidence.
    if previous is not None:
        if mode != "agent" or not expected_qid or not conditions:
            raise ValueError("INVALID_SECOND_TURN")
        first.update(question_id=expected_qid, conditions=conditions)
    output: dict[str, Any] = {
        "turn": 2 if previous is not None else 1, "session_id": sid, "question_id": expected_qid, "execution_status": None,
        "request_body": first, "request_url": base.rstrip("/") + "/api/chat?wait=false",
        "status": None, "answer": None, "message": None, "citations": [],
        "clarification": None, "raw_response": None, "http_trace": http_trace,
        "retrieval_count": None, "clarification_count": None,
        "latency_seconds": None, "error": None,
    }
    base = base.rstrip("/")
    try:
        # ?wait=false avoids a long synchronous wait for persistent Agent mode.
        code, raw = http_json("POST", f"{base}/api/chat?wait=false", first, req_timeout)
        http_trace.append({"method": "POST", "status_code": code, "body": raw})
        body: Any = raw
        if isinstance(body, dict):
            output["session_id"] = body.get("session_id") or sid
            output["question_id"] = body.get("question_id") or expected_qid
            output["execution_status"] = body.get("execution_status")
        def check_ids(value: Any) -> None:
            if not isinstance(value, dict) or mode != "agent":
                return
            for part in (value, value.get("response")):
                if not isinstance(part, dict):
                    continue
                if part.get("session_id") is not None and part["session_id"] != sid:
                    raise RuntimeError("PROTOCOL_SESSION_ID_MISMATCH")
                known_qid = expected_qid or output["question_id"]
                if known_qid and part.get("question_id") is not None and part["question_id"] != known_qid:
                    raise RuntimeError("PROTOCOL_QUESTION_ID_MISMATCH")
        check_ids(body)
        if code == 202:
            sid2, qid = output["session_id"], output["question_id"]
            if not (isinstance(sid2, str) and isinstance(qid, str) and qid):
                raise RuntimeError("ASYNC_RESPONSE_MISSING_IDS")
            poll_url = (f"{base}/api/chat/sessions/{quote(sid2, safe='')}"
                        f"/questions/{quote(qid, safe='')}/execution")
            deadline = time.monotonic() + poll_timeout
            while True:
                if time.monotonic() >= deadline:
                    raise RuntimeError("POLL_TIMEOUT: incomplete request kept; no automatic rerun")
                time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("POLL_TIMEOUT: incomplete request kept; no automatic rerun")
                code, body = http_json("GET", poll_url, None, min(req_timeout, remaining))
                http_trace.append({"method": "GET", "status_code": code, "body": body})
                check_ids(body)
                if isinstance(body, dict):
                    output["execution_status"] = body.get("execution_status")
                if code not in SUCCESS_HTTP:
                    raise RuntimeError(f"EXECUTION_QUERY_HTTP_{code}")
                if code != 202 and (not isinstance(body, dict) or body.get("execution_status") not in ACTIVE):
                    break
        elif code not in SUCCESS_HTTP:
            # Preserve HTTP error raw data; no retry.
            raise RuntimeError(f"CHAT_HTTP_{code}")
        if isinstance(body, dict):
            output["session_id"] = body.get("session_id") or output["session_id"]
            output["question_id"] = body.get("question_id") or output["question_id"]
            output["execution_status"] = body.get("execution_status", output["execution_status"])
        check_ids(body)
        answer = extract_answer(body)
        output["raw_response"] = body
        if answer is None:
            raise RuntimeError("TERMINAL_ANSWER_NOT_AVAILABLE")
        for key in ("status", "answer", "message", "citations", "clarification"):
            output[key] = answer.get(key)
        if output["citations"] is None:
            output["citations"] = []
        # The answer may include IDs even when the outer response doesn't.
        output["session_id"] = answer.get("session_id") or output["session_id"]
        output["question_id"] = answer.get("question_id") or output["question_id"]
        output["execution_status"] = answer.get("execution_status", output["execution_status"])
    except RuntimeError as e:
        output["error"] = str(e)
        output["raw_response"] = http_trace[-1]["body"] if http_trace else None
    finally:
        # End-to-end system wall clock for the observed turn, excluding user wait and state detail fetch.
        output["latency_seconds"] = round(time.monotonic() - started, 3)
    if output["error"] is None:
        if mode == "agent" and not output["question_id"]:
            output["error"] = "PROTOCOL_MODE_MISMATCH: expected persistent Agent response with question_id"
        if mode == "single" and output["question_id"]:
            output["error"] = "PROTOCOL_MODE_MISMATCH: expected legacy Single response without question_id"
    if mode == "agent" and output["error"] is None and output["execution_status"] != "SUCCEEDED":
        output["error"] = f"AGENT_EXECUTION_NOT_SUCCEEDED: {output['execution_status']}"
    if mode == "agent" and output["question_id"] and not str(output["error"]).startswith("PROTOCOL_"):
        rc, cc, raw_state, state_err = session_counts(base, output["session_id"],
                                                       output["question_id"], req_timeout)
        output["retrieval_count"], output["clarification_count"] = rc, cc
        output["session_detail_response"] = raw_state
        output["session_detail_error"] = state_err
    # For Single, unobserved counts remain null, NOT 0 or 1.
    return output


def run_first_turn(base: str, mode: str, question: str,
                   req_timeout: float, poll_seconds: float, poll_timeout: float) -> dict:
    return run_turn(base, mode, question, req_timeout, poll_seconds, poll_timeout)


def run_case(case: dict, args: argparse.Namespace) -> tuple[list[dict], dict]:
    first = run_first_turn(args.base_url, args.mode, case["question"],
                           args.request_timeout, args.poll_interval, args.poll_timeout)
    turns = [first]
    audit = {"status": "NOT_REQUIRED", "submitted_conditions": None, "reason": None}
    if args.mode == "agent" and first["status"] == "NEEDS_CLARIFICATION":
        if first["error"] or first.get("session_detail_error") or first["clarification_count"] != 1:
            audit.update(status="ADAPTER_BLOCKED", reason="FIRST_TURN_OR_SESSION_NOT_VERIFIED")
        else:
            mapped, audit = map_conditions(case, first["clarification"])
            if mapped is not None:
                second = run_turn(args.base_url, args.mode, case["question"],
                                  args.request_timeout, args.poll_interval, args.poll_timeout,
                                  previous=first, conditions=mapped)
                turns.append(second)
                if second["error"]:
                    audit.update(status="SECOND_TURN_ERROR", reason=second["error"])
                elif second["status"] == "NEEDS_CLARIFICATION":
                    audit.update(status="CLARIFICATION_LIMIT_REACHED", reason="NO_THIRD_TURN")
                else:
                    audit["status"] = "SECOND_TURN_RETURNED"
    return turns, audit


def choose_cases(cases: list[dict], ids: list[str] | None, limit: int | None,
                 all_cases: bool) -> list[dict]:
    if not all_cases and not ids and limit is None:
        raise ValueError("为防止误调用整套题，请明确指定 --qid、--limit 或 --all")
    if ids:
        desired = set(ids)
        missing = desired - {c["qid"] for c in cases}
        if missing:
            raise ValueError("题库中不存在的 qid: " + ", ".join(sorted(missing)))
        cases = [c for c in cases if c["qid"] in desired]
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit 必须大于 0")
        cases = cases[:limit]
    return cases


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Pilot-stage eval via /api/chat, JSONL output")
    p.add_argument("--questions", type=Path, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--check-only", action="store_true")
    g.add_argument("--run-real", action="store_true")
    p.add_argument("--mode", choices=["single", "agent"])
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--output", type=Path)
    p.add_argument("--qid", action="append", help="重复参数可测试多题")
    p.add_argument("--limit", type=int)
    p.add_argument("--all", action="store_true")
    p.add_argument("--configured-model", help="仅记录配置模型名称，不冒充后端实际模型")
    p.add_argument("--generation-profile", help="仅记录配置 Profile")
    p.add_argument("--code-commit", help="可手动指定已核验的 40 位 commit SHA")
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    p.add_argument("--request-timeout", type=float, default=150)
    p.add_argument("--poll-timeout", type=float, default=300)
    p.add_argument("--poll-interval", type=float, default=1)
    args = p.parse_args(argv)
    try:
        questions, dataset_sha = read_questions(args.questions)
    except (OSError, ValueError, UnicodeError) as e:
        p.error(str(e))
    print(f"题库校验通过: {len(questions)} 题；sha256={dataset_sha}")
    print("题型分布:", dict(Counter(str(c.get("category", c.get("qtype", "UNKNOWN"))) for c in questions)))
    if args.check_only:
        print("PIPELINE VERSION:", PIPELINE_VERSION)
        if dataset_sha == PILOT_SHA256:
            matched = sum(1 for c in questions if (r := R02_FACTS.get(c["qid"])) and matches_input(c, r))
            print(f"Adapter 输入核对: {matched}/{len(questions)}；r02 冻结事实，4处争议字段阻塞保护")
        print("CHECK ONLY：未调用任何服务器，也未创建结果文件。")
        return 0
    if not args.mode or args.output is None:
        p.error("--run-real 必须指定 --mode 与 --output")
    if dataset_sha != PILOT_SHA256 or args.questions.name != "pilot43_questions_v1_40.jsonl":
        p.error("v4 真实调用仅允许已核验的冻结 Pilot 40题；禁止正式30题")
    if any(c["qid"] not in R02_FACTS or not matches_input(c, R02_FACTS[c["qid"]]) for c in questions):
        p.error("r02 facts 与题库输入不一致")
    if args.code_commit and not re.fullmatch(r"[0-9a-fA-F]{40}", args.code_commit):
        p.error("--code-commit 必须为40位十六进制 SHA")
    if any(not math.isfinite(x) or x <= 0 for x in (args.poll_timeout, args.poll_interval, args.request_timeout)):
        p.error("各超时与轮询间隔必须大于0")
    try:
        chosen = choose_cases(questions, args.qid, args.limit, args.all)
    except ValueError as e:
        p.error(str(e))
    if args.output.exists():
        p.error(f"输出文件已经存在，拒绝覆盖: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sha = args.code_commit or get_git_sha(args.repo_root)
    run_id = "eval_" + uuid4().hex
    # Exclusive file creation: never overwrite prior experiment results.
    with args.output.open("x", encoding="utf-8", newline="\n") as f:
        for index, case in enumerate(chosen, start=1):
            turns, mapping = run_case(case, args)
            final = turns[-1]
            raw_final = final["raw_response"] if isinstance(final["raw_response"], dict) else {}
            actual = extract_answer(raw_final) or raw_final
            actual_model = actual.get("model_name")  # null unless directly exposed by API
            fallback_flag = actual.get("fallback_triggered")  # null unless directly observed
            row = {
                "run_id": run_id, "recorded_at": iso_now(), "qid": case["qid"],
                "category": case.get("category", case.get("qtype")), "lang": case.get("lang"),
                "question": case["question"], "rag_mode": args.mode,
                "comparison_case": "pilot_original",
                "model_name": actual_model, "configured_model": args.configured_model,
                "fallback_triggered": fallback_flag, "fallback_model": actual.get("fallback_model"),
                "dataset_sha256": dataset_sha, "dataset_file": args.questions.name,
                "code_commit": sha, "pipeline_version": PIPELINE_VERSION,
                "pipeline_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "adapter_sha256": ADAPTER_SHA256, "facts_sha256": FACTS_SHA256,
                "adapter_policy": "r02_exact_logic_with_prospective_fact_holds",
                "config_snapshot": {
                    "rag_mode_requested": args.mode,
                    "api_base_url": args.base_url,
                    "configured_model": args.configured_model,
                    "generation_profile": args.generation_profile,
                },
                "turns": turns, "total_system_seconds": round(sum(t["latency_seconds"] for t in turns), 3),
                "final_status": final["status"], "final_execution_status": final["execution_status"],
                "final_error": final["error"],
                "retrieval_count": final["retrieval_count"], "clarification_count": final["clarification_count"],
                "mapping": mapping,
                "workflow_fallback_triggered": actual.get("workflow_fallback_triggered"),
                "model_fallback_triggered": actual.get("model_fallback_triggered"),
                "answer_correct": None, "citation_correct": None, "notes": None,
                "answer_complete": None, "clarification_correct": None, "refusal_correct": None,
                "task_success": None,
                "requires_mapping": mapping["status"] in {"ADAPTER_UNMAPPED", "ADAPTER_BLOCKED"},
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            print(f"[{index}/{len(chosen)}] {case['qid']} mode={args.mode} "
                  f"turns={len(turns)} status={final['status']} exec={final['execution_status']} "
                  f"time={row['total_system_seconds']}s mapping={mapping['status']} error={final['error']}")
    print("已保存结果:", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
