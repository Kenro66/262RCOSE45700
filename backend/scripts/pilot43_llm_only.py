r"""Issue #43: run a real LLM-only Pilot baseline with no document retrieval.

Put this file at backend/scripts/pilot43_llm_only.py inside the project.
Examples from the repository root:

  .\.venv\Scripts\python.exe backend\scripts\pilot43_llm_only.py --check-only
  .\.venv\Scripts\python.exe backend\scripts\pilot43_llm_only.py --run-real --question-id P001 --output eval\results\pilot43_baseline_test_P001.json
  .\.venv\Scripts\python.exe backend\scripts\pilot43_llm_only.py --run-real --output eval\results\pilot43_baseline_all40.json

Calls the exact generation adapter used by the RAG project, with the same configured
GENERATION_MODEL and gateway; does not send gold answers or evidence to the model.
Real requests may incur charges. Do not commit backend/.env or API credentials.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

SCRIPT_PATH = Path(__file__).resolve()
# Expected location: <repository>/backend/scripts/pilot43_llm_only.py
REPO = SCRIPT_PATH.parents[2]
BACKEND = REPO / "backend"
DEFAULT_QUESTIONS = REPO / "eval" / "pilot43_questions_v1_40.jsonl"

SYSTEM_PROMPT = (
    "你是一名中韩海关政策问答助手。现在进行不使用检索的 LLM-only 对照测试。"
    "本轮没有提供政策 PDF、证据片段、数据库内容或搜索结果。"
    "只根据模型自身已有知识回答；不确定时明确说不确定，不能假装已经查阅任何文件或网站。"
    "不要编造法律条文、政策文件名、引用、页码或来源链接。"
    "认真注意国家、入境方向、旅客身份、日期、数额及例外条件。"
    "按提问语言回答（中文问题用中文、韩语问题用韩语），尽量简洁。"
    "直接给出回答正文，不要输出 Markdown 代码块。"
)


def load_questions(path: Path) -> tuple[list[dict], str]:
    data = path.read_bytes()
    questions = []
    seen = set()
    for lineno, line in enumerate(data.decode("utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as err:
            raise ValueError(f"Invalid JSONL at line {lineno}: {err}") from err
        if not isinstance(row, dict):
            raise ValueError(f"Line {lineno} must be a JSON object")
        qid, question = row.get("qid"), row.get("question")
        if not isinstance(qid, str) or not qid.strip():
            raise ValueError(f"Missing qid at line {lineno}")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"Missing question at line {lineno}")
        if qid in seen:
            raise ValueError(f"Duplicate question ID: {qid}")
        seen.add(qid)
        questions.append(row)
    if not questions:
        raise ValueError("No questions found")
    return questions, hashlib.sha256(data).hexdigest()


def save_json(path: Path, obj: dict) -> None:
    # Replace atomically after each item, so partial progress remains readable.
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Real LLM-only Pilot test; no RAG retrieval")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--output", type=Path, help="New output JSON file (never overwrites)")
    parser.add_argument("--question-id", action="append", help="Run a specific qid; may repeat")
    parser.add_argument("--check-only", action="store_true", help="Validate and count questions; do not call model")
    parser.add_argument("--run-real", action="store_true", help="Acknowledge real model calls and possible costs")
    args = parser.parse_args()

    try:
        questions, question_sha256 = load_questions(args.questions)
    except (OSError, UnicodeError, ValueError) as err:
        parser.error(str(err))
    if args.question_id:
        requested = set(args.question_id)
        questions = [row for row in questions if row["qid"] in requested]
        unknown = requested.difference(row["qid"] for row in questions)
        if unknown:
            parser.error("Unknown question ID(s): " + ", ".join(sorted(unknown)))

    print(f"Validated {len(questions)} question(s). Source: {args.questions}")
    print("Model will receive only question text, NOT standard answers or PDF evidence.")
    if args.check_only:
        if args.run_real:
            parser.error("Choose either --check-only or --run-real")
        print("CHECK OK: no model API call made.")
        return
    if not args.run_real:
        parser.error("To make real model calls, pass --run-real; first use --check-only")
    if args.output is None:
        parser.error("--output is required for real runs")
    if args.output.exists():
        parser.error(f"Refusing to overwrite existing output: {args.output}")
    if not (BACKEND / "app" / "adapters" / "generation.py").is_file():
        parser.error("Script must be saved in <repo>/backend/scripts/; backend adapter not found")

    sys.path.insert(0, str(BACKEND))
    from app.adapters.config import settings
    from app.adapters.generation import generate_text

    config = settings()
    if not config.get("GENERATION_MODEL", "").strip() or not config.get("GENERATION_BASE_URL", "").strip():
        parser.error("Set GENERATION_MODEL and GENERATION_BASE_URL in backend/.env")
    # Do not display/serialize API keys, database URLs, or other private configuration.
    report = {
        "mode": "LLM-only; model knowledge without PDF retrieval",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "completed_at_utc": None,
        "model": config["GENERATION_MODEL"],
        "questions_sha256": question_sha256,
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "gold_answers_sent_to_model": False,
        "retrieved_evidence_sent_to_model": False,
        "question_count_requested": len(questions),
        "questions": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    for row in questions:
        started = time.monotonic()
        record = {"id": row["qid"], "question": row["question"], "language": row.get("lang"),
                  "qtype": row.get("qtype"), "answerable": row.get("answerable")}
        try:
            answer = generate_text([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": row["question"]},
            ])
            record["response"] = {"answer": answer, "citations": []}
            record["outcome"] = "COMPLETED"
        except Exception as err:
            # Error types only: never leak gateway messages that could contain credentials.
            record["outcome"] = "ERROR"
            record["error_type"] = type(err).__name__
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["questions"].append(record)
        save_json(args.output, report)
        print(f"{record['id']}: {record['outcome']} ({record['elapsed_seconds']}s)", flush=True)

    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    save_json(args.output, report)
    completed = sum(r["outcome"] == "COMPLETED" for r in report["questions"])
    print(f"Saved {len(questions)} rows ({completed} completed). File: {args.output}")
    print("Human correctness and evidence assessment are NOT automated by this script.")


if __name__ == "__main__":
    main()
