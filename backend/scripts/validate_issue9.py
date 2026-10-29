"""Opt-in real HTTP acceptance against a dedicated empty test application.

Uses the configured real pipeline; never chooses gold evidence or substitutes models.
Only the document IDs uploaded by this run are deleted, including on failure.
"""
import argparse
import json
from pathlib import Path
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.contracts import ChatResponse, DocumentRecord


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def check_chat(response, expected):
    require(response.status_code == (500 if expected == "SYSTEM_ERROR" else 200),
            f"Unexpected HTTP status for {expected}")
    result = ChatResponse.model_validate(response.json())
    require(result.status.value == expected, f"Expected {expected}, got {result.status.value}")
    if expected == "ANSWERED":
        require(bool(result.answer and result.answer.strip() and result.citations),
                "ANSWERED must include text and citations")
        require(all(c.filename.strip() and c.quote.strip() for c in result.citations),
                "Missing citation source fields")
    else:
        require(result.answer is None and result.citations == [] and bool(result.message),
                "Error response must follow C5")
    return result


def run(client, pdfs, question, expected="ANSWERED", timeout=300):
    uploaded = []
    records = []
    events = []
    try:
        response = client.get("/api/documents")
        require(response.status_code == 200 and response.json() == [],
                "Use a dedicated empty test database; existing documents are never deleted")
        check_chat(client.post("/api/chat", json={"message": question}), "NO_READY_DOCUMENTS")
        events.append("empty_library_no_ready")
        for pdf in pdfs:
            with pdf.open("rb") as content:
                response = client.post("/api/documents", files={
                    "file": (pdf.name, content, "application/pdf")})
            require(response.status_code == 201, "Upload did not return 201")
            record = DocumentRecord.model_validate(response.json())
            uploaded.append(record.document_id)
            require(record.status.value == "PROCESSING", "Upload must return PROCESSING")
        require(len(set(uploaded)) == 3, "Three uploads must have distinct IDs")
        deadline = time.monotonic() + timeout
        while True:
            listing = client.get("/api/documents")
            require(listing.status_code == 200, "Document list failed")
            records = [DocumentRecord.model_validate(item) for item in listing.json()]
            current = {item.document_id: item for item in records if item.document_id in uploaded}
            require(len(current) == 3, "An uploaded document disappeared")
            require(all(item.status.value != "FAILED" for item in current.values()),
                    "A document failed processing; inspect server logs")
            if all(item.status.value == "READY" for item in current.values()):
                break
            require(time.monotonic() < deadline, "Timed out waiting for READY")
            time.sleep(0.5)
        events.append("three_pdfs_ready")
        result = check_chat(client.post("/api/chat", json={"message": question}), expected)
        if expected == "ANSWERED":
            names = {item.filename for item in current.values()}
            require(all(c.filename in names for c in result.citations),
                    "Citation filename is not one of the uploaded documents")
        events.append(expected)
        return {"events": events, "response": result.model_dump(mode="json"),
                "documents": [item.model_dump(mode="json") for item in records]}
    finally:
        cleanup_errors = []
        for document_id in uploaded:
            try:
                response = client.delete(f"/api/documents/{document_id}")
                require(response.status_code == 204, "Cleanup did not return 204")
            except Exception:
                cleanup_errors.append(document_id)
        require(not cleanup_errors, "Cleanup failed for this run's IDs: " + ", ".join(cleanup_errors))
        if uploaded:
            # The dedicated test DB must not be shared with another process.
            require(client.get("/api/documents").json() == [], "Test library is not empty after cleanup")
            check_chat(client.post("/api/chat", json={"message": question}), "NO_READY_DOCUMENTS")
            events.append("delete_all_then_no_ready")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--pdf", action="append", type=Path, required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--expected-status", choices=["ANSWERED", "SYSTEM_ERROR"], default="ANSWERED")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.pdf) != 3 or not all(p.is_file() for p in args.pdf):
        parser.error("Supply exactly three existing PDFs")
    result = {"passed": False, "mode": "real_http_configured_models"}
    try:
        with httpx.Client(base_url=args.base_url, timeout=120, trust_env=False) as client:
            result.update(run(client, args.pdf, args.question, args.expected_status))
        result["passed"] = True
    except AssertionError as exc:
        result["error_type"] = "AssertionError"
        result["failure"] = str(exc)  # Only explicit checks above, never dependency text.
    except Exception as exc:
        # Never dump a URL, credential, HTTP body, or arbitrary dependency exception.
        result["error_type"] = type(exc).__name__
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "error_type": result.get("error_type")}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
