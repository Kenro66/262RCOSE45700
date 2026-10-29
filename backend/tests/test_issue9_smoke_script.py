"""Exercise acceptance-script cleanup using HTTP fixtures, not real models."""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "issue9_smoke", Path(__file__).resolve().parents[1] / "scripts/validate_issue9.py"
)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class API:
    def __init__(self, *, existing=False, model_error=False, fail_upload=False):
        self.docs = {"OLD": {}} if existing else {}
        self.count = 0
        self.deleted = []
        self.model_error = model_error
        self.fail_upload = fail_upload

    def __call__(self, request):
        path = request.url.path
        if path == "/api/documents":
            if request.method == "GET":
                return httpx.Response(200, json=list(self.docs.values()))
            self.count += 1
            if self.fail_upload and self.count == 2:
                return httpx.Response(500, json={"detail": "fixture failure"})
            doc = dict(document_id=f"DOC{self.count}", filename=f"{self.count}.pdf",
                       status="READY", error_message=None, page_count=1,
                       uploaded_at="2026-10-28T00:00:00Z")
            self.docs[doc["document_id"]] = doc
            return httpx.Response(201, json={**doc, "status": "PROCESSING"})
        if request.method == "DELETE":
            doc_id = path.rsplit("/", 1)[1]
            self.deleted.append(doc_id)
            self.docs.pop(doc_id)
            return httpx.Response(204)
        if not self.docs:
            return httpx.Response(200, json=dict(status="NO_READY_DOCUMENTS", answer=None,
                                                citations=[], message="当前没有可用文件"))
        if self.model_error:
            return httpx.Response(500, json=dict(status="SYSTEM_ERROR", answer=None,
                                                citations=[], message="系统暂时无法处理请求，请稍后重试。"))
        return httpx.Response(200, json=dict(status="ANSWERED", answer="fixture [E1]",
            citations=[dict(evidence_id="E1", filename="1.pdf", page=1, quote="fixture")], message=None))


def pdfs(tmp_path):
    result = [tmp_path / f"{i}.pdf" for i in range(3)]
    for path in result:
        path.write_bytes(b"%PDF-fixture")
    return result


@pytest.mark.parametrize("expected", ["ANSWERED", "SYSTEM_ERROR"])
def test_success_and_cleanup(tmp_path, expected):
    api = API(model_error=expected == "SYSTEM_ERROR")
    with httpx.Client(base_url="http://fixture", transport=httpx.MockTransport(api)) as client:
        result = smoke.run(client, pdfs(tmp_path), "question", expected)
    assert result["events"][-1] == "delete_all_then_no_ready"
    assert api.deleted == ["DOC1", "DOC2", "DOC3"]
    assert not api.docs


def test_existing_documents_are_never_deleted(tmp_path):
    api = API(existing=True)
    with httpx.Client(base_url="http://fixture", transport=httpx.MockTransport(api)) as client:
        with pytest.raises(AssertionError, match="dedicated empty"):
            smoke.run(client, pdfs(tmp_path), "question")
    assert api.deleted == [] and api.count == 0


def test_partial_upload_failure_still_cleans_its_own_ids(tmp_path):
    api = API(fail_upload=True)
    with httpx.Client(base_url="http://fixture", transport=httpx.MockTransport(api)) as client:
        with pytest.raises(AssertionError, match="201"):
            smoke.run(client, pdfs(tmp_path), "question")
    assert api.deleted == ["DOC1"] and not api.docs


def test_wrong_expected_result_is_a_failure_and_still_cleans_up(tmp_path):
    api = API(model_error=True)
    with httpx.Client(base_url="http://fixture", transport=httpx.MockTransport(api)) as client:
        with pytest.raises(AssertionError, match="HTTP status"):
            smoke.run(client, pdfs(tmp_path), "question")
    assert len(api.deleted) == 3 and not api.docs
