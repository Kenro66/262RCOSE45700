"""Run Issue #17 questions with real generation and preselected real chunks, not DB retrieval."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.adapters.generation import generate_text
from app.contracts import ChatStatus, Chunk
from app.qa.orchestrator import answer
from app.qa.prompts import detect_language


FILENAMES = {
    "DOC003": "中华人民共和国海关进出境行李物品监管办法.pdf",
    "DOC004": "《进境物品关税、增值税、消费税征收办法》.pdf",
    "DOC005": "여행자 및 승무원 휴대품 통관에 관한 고시.pdf",
}
# Oracle selection isolates answer/citation behavior from the unavailable search service.
SELECTION = {
    "Q001": ["DOC005_C000037", "DOC005_C000038", "DOC005_C000036"],
    "Q002": ["DOC005_C000013"],
    "Q003": ["DOC005_C000039", "DOC005_C000013"],
    "Q004": ["DOC005_C000011"],
    "Q005": ["DOC005_C000013", "DOC005_C000015"],
    "Q006": ["DOC004_C000027"],
    "Q007": ["DOC004_C000027", "DOC004_C000023"],
    "Q008": ["DOC003_C000013", "DOC003_C000017", "DOC003_C000012"],
    "Q009": ["DOC004_C000027", "DOC005_C000013"],
    "Q010": ["DOC004_C000027", "DOC005_C000013"],
}
TERMS = {
    "Q001": [r"담배", r"제외|아니", r"별도|별개|관계없이"],
    "Q002": [r"신고", r"해야|합니다|필요"],
    "Q003": [r"19", r"면제.*않|면제.*없|면제되지", r"1월\s*1일"],
    "Q004": [r"생략", r"요구", r"제출"],
    "Q005": [r"40", r"60"],
    "Q006": [r"(?<!\d)400(?!\d)", r"支"],
    "Q007": [r"(?<!\d)40(?!\d)", r"(?:一天|每日|每天).{0,4}一次"],
    "Q008": [r"红色通道", r"书面申报"],
}


def load_questions(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    cases = []
    for block in re.split(r"(?=^Q\d{3}\s*\|)", text, flags=re.MULTILINE):
        header = re.match(r"(Q\d{3})\s*\|[^\n]*lang=(zh|ko)", block)
        if not header:
            continue
        fields = {}
        for name in ("问题", "标准答案", "适用条件"):
            match = re.search(r"^" + name + r"：(.+)$", block, flags=re.MULTILINE)
            if not match:
                raise ValueError(f"Missing {name} for {header[1]}")
            fields[name] = match[1].strip()
        cases.append({"id": header[1], "lang": header[2], "question": fields["问题"],
                      "expected_answer": fields["标准答案"], "conditions": fields["适用条件"],
                      "expected_status": "INSUFFICIENT_EVIDENCE" if "应拒答" in header[0] else "ANSWERED",
                      "gold_evidence_lines": [part.strip() for part in re.findall(
                          r"^证据：(.*?)(?=^证据：|\Z)", block, flags=re.MULTILINE | re.DOTALL)]})
    if [case["id"] for case in cases] != list(SELECTION):
        raise ValueError("Expected the supplied Q001..Q010 question set, without duplicates")
    return cases


def render_report(report: dict) -> str:
    rows = ["# Issue #12 / Issue #17 冒烟复测", "",
            "真实生成 + 按题单预选的真实 Chunk + 合成文档元数据；不是数据库检索或 Top-k 验收。",
            "标准答案、适用条件和预期证据仅用于报告，不发送给模型；模型只收到原始问题和 Chunk 正文。",
            "自动检查为状态、语言、关键点和引用来源检查，不等于完整语义正确性证明。", "",
            f"运行时间（UTC）：{report['started_at_utc']}",
            f"结果：{report['passed']}/{len(report['cases'])} 通过。", "",
            "| 题号 | 预期 | 实际 | 自动检查 |", "|---|---|---|---|"]
    for case in report["cases"]:
        rows.append(f"| {case['id']} | {case['expected_status']} | {case.get('response', {}).get('status', case.get('error_type', 'ERROR'))} | {'PASS' if case['passed'] else 'FAIL'} |")
    rows += ["", "## 逐题记录"]
    for case in report["cases"]:
        rows += ["", f"### {case['id']}", case["question"], "",
                 "题单标准答案：" + case["expected_answer"], "",
                 "选取的真实 Chunk：" + ", ".join(case["selected_chunk_ids"]), "",
                 "实际回答：" + case.get("response", {}).get("answer", "未返回 C5"), "",
                 "检查：" + json.dumps(case.get("checks", {}), ensure_ascii=False)]
        if not case["passed"] and case.get("raw_generation"):
            rows += ["", "未通过时的模型原始输出（未作为最终回答返回）：", "```json", case["raw_generation"], "```"]
        for citation in case.get("response", {}).get("citations", []):
            rows += [f"- {citation['evidence_id']} / {citation['filename']} / 物理第{citation['page']}页",
                     "  原文：" + citation["quote"].replace("\n", " ")]
    return "\n".join(rows) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--chunks-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.env_file:
        if not args.env_file.is_file():
            raise ValueError("Existing private configuration file not found")
        private = dotenv_values(args.env_file)
        for key in ("GENERATION_MODEL", "GENERATION_BASE_URL", "GENERATION_API_KEY", "GENERATION_TIMEOUT_SECONDS"):
            if private.get(key) and not os.environ.get(key):
                os.environ[key] = private[key]
    cases = load_questions(args.questions)
    chunks = {}
    sources = {}
    for document_id in FILENAMES:
        path = args.chunks_dir / f"{document_id}-chunks.json"
        data = path.read_bytes()
        sources[document_id] = hashlib.sha256(data).hexdigest()
        for row in json.loads(data):
            chunk = Chunk.model_validate(row)
            chunks[chunk.chunk_id] = chunk
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
              "question_file_sha256": hashlib.sha256(args.questions.read_bytes()).hexdigest(),
              "chunk_source_sha256": sources, "real_generation": True,
              "retrieval": "oracle/preselected real chunks; NOT vector or database retrieval",
              "documents": "fixture metadata named from supplied question set; NOT real get_documents",
              "gold_answers_sent_to_model": False, "cases": [], "passed": 0}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for question in cases:
        selected = [chunks[key] for key in SELECTION[question["id"]]]
        case = {**question, "selected_chunk_ids": [chunk.chunk_id for chunk in selected]}
        async def retrieve(query):
            return [dict(chunk_id=c.chunk_id, document_id=c.document_id, page_number=c.page_number,
                         text=c.text, score=1.) for c in selected]
        async def documents(ids):
            return {key: {"filename": FILENAMES[key], "status": "READY"} for key in ids}
        def generate(messages):
            raw = generate_text(messages)
            case["raw_generation"] = raw
            return raw
        try:
            result = await answer(question["question"], retrieve_fn=retrieve, get_documents_fn=documents, generate_fn=generate)
            checks = {"status": result.status.value == question["expected_status"],
                      "language": detect_language(result.answer) == question["lang"]}
            if question["expected_status"] == "ANSWERED":
                checks["key_points"] = all(re.search(pattern, result.answer) for pattern in TERMS[question["id"]])
                mapping = {f"E{i}": chunk for i, chunk in enumerate(selected, 1)}
                checks["trusted_citations"] = bool(result.citations) and all(
                    citation.evidence_id in mapping and citation.quote == mapping[citation.evidence_id].text
                    and citation.page == mapping[citation.evidence_id].page_number
                    and citation.filename == FILENAMES[mapping[citation.evidence_id].document_id]
                    for citation in result.citations)
            else:
                checks["no_digits"] = not any(c.isdigit() for c in result.answer)
                checks["no_citations"] = not result.citations
            case.update(response=result.model_dump(mode="json"), checks=checks, passed=all(checks.values()))
        except Exception as exc:
            case.update(passed=False, error_type=type(exc).__name__)
        report["cases"].append(case)
        report["passed"] = sum(row["passed"] for row in report["cases"])
        (args.output_dir / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output_dir / "report.md").write_text(render_report(report), encoding="utf-8")
        print(f"{case['id']}: {'PASS' if case['passed'] else 'FAIL'}", flush=True)
    return 0 if report["passed"] == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
