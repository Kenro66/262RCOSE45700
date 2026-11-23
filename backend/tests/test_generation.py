import json
import os
import socket
import tempfile
import threading
import unittest
import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from app.adapters import config
from app.adapters.generation import GenerationError, generate, generate_text


TOOL = {"type": "function", "function": {"name": "lookup_policy", "description": "Look up a policy", "parameters": {"type": "object", "properties": {}}}}


class GenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.received.append((self.path, payload))
                if payload["model"] == "http-error":
                    self.send_error(503)
                    return
                self.send_response(200)
                self.end_headers()
                if payload["model"] == "bad-json":
                    self.wfile.write(b"invalid")
                    return
                if payload["model"] == "tools-only":
                    message = {"content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "lookup_policy", "arguments": "{}"}}]}
                else:
                    message = {"content": "" if payload["model"] == "empty" else "Test transport response"}
                self.wfile.write(json.dumps({"choices": [{"message": message}]}).encode())

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.base = f"http://127.0.0.1:{self.server.server_port}/v1"
        env = patch.dict(os.environ, {"GENERATION_PROFILE": "legacy", "GENERATION_MODEL": "test", "GENERATION_BASE_URL": self.base, "GENERATION_API_KEY": "", "GENERATION_TIMEOUT_SECONDS": "3"})
        env.start()
        self.addCleanup(env.stop)

    def test_real_http_transport_and_tools(self):
        self.assertTrue(generate_text("Example question", tools=[TOOL]))
        path, payload = self.received[-1]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(payload["tools"], [TOOL])

    def test_tool_calls_are_preserved(self):
        with patch.dict(os.environ, {"GENERATION_MODEL": "tools-only"}):
            self.assertEqual(generate("Question", tools=[TOOL]).tool_calls[0]["function"]["name"], "lookup_policy")
            with self.assertRaisesRegex(GenerationError, "tool calls only"):
                generate_text("Question", tools=[TOOL])

    def test_bad_responses_raise(self):
        for model in ("http-error", "empty", "bad-json"):
            with self.subTest(model=model), patch.dict(os.environ, {"GENERATION_MODEL": model}):
                with self.assertRaises(GenerationError):
                    generate_text("Question")

    def test_unavailable_endpoint_really_raises(self):
        # The port is bound but not listening, so no other service can claim it.
        with socket.socket() as blocked:
            blocked.bind(("127.0.0.1", 0))
            with patch.dict(os.environ, {"GENERATION_BASE_URL": f"http://127.0.0.1:{blocked.getsockname()[1]}/v1"}):
                with self.assertRaisesRegex(GenerationError, "connection failed"):
                    generate_text("Question")

    def test_only_env_edits_change_model_and_address(self):
        original = config.dotenv_values
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            with patch.dict(os.environ, {}, clear=True), patch.object(config, "dotenv_values", side_effect=lambda _: original(path)):
                for model, api_root in (("model-a", "/api-a"), ("model-b", "/api-b")):
                    base = f"http://127.0.0.1:{self.server.server_port}{api_root}"
                    path.write_text(f"GENERATION_MODEL={model}\nGENERATION_BASE_URL={base}\n", encoding="utf-8")
                    self.assertTrue(generate_text("Question"))
                    endpoint, body = self.received[-1]
                    self.assertEqual(body["model"], model)
                    self.assertEqual(endpoint, api_root + "/chat/completions")

    def test_configuration_errors(self):
        for values in ({"GENERATION_MODEL": ""}, {"GENERATION_BASE_URL": "file:///tmp"}, {"GENERATION_BASE_URL": "http://127.0.0.1:bad/v1"}, {"GENERATION_TIMEOUT_SECONDS": "nan"}):
            with self.subTest(values=values), patch.dict(os.environ, values):
                with self.assertRaises(GenerationError):
                    generate_text("Question")


# Mock transport only: no model Endpoint is contacted.
def test_optional_schema_parameters_and_explicit_provider(monkeypatch):
    import httpx
    from app.adapters import generation
    received = []
    client = httpx.Client
    def respond(request):
        received.append((str(request.url), json.loads(request.content), request.headers.get("Authorization")))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"ok","insufficient_evidence":false}'}}]})
    monkeypatch.setattr(generation.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(generation, "settings", lambda: {"GENERATION_MODEL": "school", "GENERATION_BASE_URL": "https://school.invalid/v1"})
    schema = {"type": "json_schema", "json_schema": {"name": "answer", "strict": True, "schema": {"type": "object"}}}
    provider = {"GENERATION_MODEL": "gemma", "GENERATION_BASE_URL": "https://gemma.invalid/v1", "GENERATION_API_KEY": "synthetic-key"}
    generate_text("test", response_format=schema, max_tokens=2048, temperature=0.6, provider_config=provider)
    url, body, auth = received[-1]
    assert url == "https://gemma.invalid/v1/chat/completions"
    assert body["model"] == "gemma" and body["response_format"] == schema
    assert body["max_tokens"] == 2048 and body["temperature"] == 0.6 and auth == "Bearer synthetic-key"
    generate_text("single")
    assert received[-1][1] == {"model": "school", "messages": [{"role": "user", "content": "single"}]}
    assert received[-1][2] is None


def test_optional_parameters_fail_before_network(monkeypatch):
    import pytest
    from app.adapters import generation
    monkeypatch.setattr(generation.httpx, "Client", lambda **kw: pytest.fail("must not send"))
    for options in ({"max_tokens": 0}, {"max_tokens": True}, {"temperature": float("nan")},
                    {"temperature": True}, {"temperature": 3}, {"response_format": []}):
        with pytest.raises(ValueError):
            generate_text("test", **options)


def test_schema_rejection_is_not_silently_retried_or_downgraded(monkeypatch):
    import httpx
    import pytest
    from app.adapters import generation
    calls = []
    client = httpx.Client
    def reject(request):
        calls.append(json.loads(request.content))
        return httpx.Response(400, json={"error": "unsupported schema"})
    monkeypatch.setattr(generation.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(reject), **kw))
    with pytest.raises(GenerationError, match="HTTP 400"):
        generate_text("test", response_format={"type": "json_schema"}, provider_config={
            "GENERATION_MODEL": "gemma", "GENERATION_BASE_URL": "https://gemma.invalid/v1"})
    assert len(calls) == 1 and calls[0]["response_format"] == {"type": "json_schema"}


def gemma_profile():
    return {"GENERATION_PROFILE": "furiosa_gemma", "FURIOSA_API_KEY": "synthetic-furiosa",
            "GENERATION_MODEL": "old-school", "GENERATION_BASE_URL": "https://school.invalid/v1",
            "GENERATION_API_KEY": "synthetic-school-must-not-be-used",
            "AGENT_GENERATION_MODEL": "old-agent", "AGENT_GENERATION_BASE_URL": "https://other.invalid/v1",
            "AGENT_GENERATION_API_KEY": "synthetic-other", "EMBEDDING_MODEL": "unchanged-bge",
            "RERANKER_ENABLED": "false"}


def test_shared_gemma_profile_is_atomic_and_generation_only():
    raw = gemma_profile()
    saved = dict(raw)
    value = config.generation_settings(raw)
    assert raw == saved
    for prefix in ("GENERATION_", "AGENT_GENERATION_"):
        assert value[prefix + "MODEL"] == config.GEMMA_MODEL
        assert value[prefix + "BASE_URL"] == config.GEMMA_BASE_URL
        assert value[prefix + "API_KEY"] == "synthetic-furiosa"
        assert value[prefix + "MAX_TOKENS"] == "2048"
    assert value["EMBEDDING_MODEL"] == raw["EMBEDDING_MODEL"]
    assert value["RERANKER_ENABLED"] == raw["RERANKER_ENABLED"]
    assert value["AGENT_GENERATION_JSON_SCHEMA_ENABLED"] == "true"
    legacy = {**raw, "GENERATION_PROFILE": "legacy"}
    assert config.generation_settings(legacy) == legacy


def test_gemma_profile_reads_local_key_without_overwriting_file(tmp_path):
    key = tmp_path / "furiosa.env"
    key.write_text("FURIOSA_API_KEY=synthetic-file-key\n", encoding="utf-8")
    raw = {**gemma_profile(), "FURIOSA_API_KEY": "", "FURIOSA_API_KEY_FILE": str(key)}
    assert config.generation_settings(raw)["GENERATION_API_KEY"] == "synthetic-file-key"
    assert key.read_text() == "FURIOSA_API_KEY=synthetic-file-key\n"


@pytest.mark.parametrize("values", [
    {"GENERATION_PROFILE": "unknown"},
    {"GENERATION_PROFILE": "furiosa_gemma", "FURIOSA_API_KEY": "bad key"},
    {"GENERATION_PROFILE": "furiosa_gemma", "FURIOSA_API_KEY": "非ASCII"},
])
def test_bad_profile_fails_before_network_without_exposing_key(monkeypatch, values):
    from app.adapters import generation
    monkeypatch.setattr(generation, "settings", lambda: values)
    monkeypatch.setattr(generation.httpx, "Client", lambda **kw: pytest.fail("must not dispatch"))
    with pytest.raises(GenerationError) as caught:
        generate_text("test")
    assert values.get("FURIOSA_API_KEY", "secret-not-present") not in str(caught.value)


def test_missing_furiosa_key_never_inherits_school_key(monkeypatch, tmp_path):
    from app.adapters import generation
    raw = {**gemma_profile(), "FURIOSA_API_KEY": "", "FURIOSA_API_KEY_FILE": str(tmp_path / "missing.env")}
    monkeypatch.setattr(generation, "settings", lambda: raw)
    monkeypatch.setattr(generation.httpx, "Client", lambda **kw: pytest.fail("must not dispatch"))
    with pytest.raises(GenerationError, match="legacy credentials are never reused") as caught:
        generate_text("test")
    assert raw["GENERATION_API_KEY"] not in str(caught.value)


@pytest.mark.parametrize("override", [
    {"GENERATION_MAX_TOKENS": "0"}, {"GENERATION_MAX_TOKENS": "invalid"},
    {"GENERATION_TEMPERATURE": "nan"}, {"GENERATION_TEMPERATURE": "3"},
    {"FURIOSA_GENERATION_TIMEOUT_SECONDS": "0"},
])
def test_unified_profile_invalid_parameters_do_not_dispatch(monkeypatch, override):
    from app.adapters import generation
    monkeypatch.setattr(generation, "settings", lambda: {**gemma_profile(), **override})
    monkeypatch.setattr(generation.httpx, "Client", lambda **kw: pytest.fail("must not dispatch"))
    with pytest.raises(GenerationError):
        generate_text("test")


@pytest.mark.parametrize("outcome", ["success", "timeout", "connection", "429", "503", "bad-json"])
def test_unified_profile_adapter_never_dispatches_another_model(monkeypatch, outcome):
    import httpx
    from app.adapters import generation
    calls = []
    client = httpx.Client
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert str(request.url) == config.GEMMA_BASE_URL + "/chat/completions"
        assert request.headers["Authorization"] == "Bearer synthetic-furiosa"
        assert body["model"] == config.GEMMA_MODEL
        assert body["max_tokens"] == 2048 and body["temperature"] == 0.6
        assert "chat_template_kwargs" not in body  # No unverified Thinking parameter.
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        if outcome == "connection":
            raise httpx.ConnectError("synthetic", request=request)
        if outcome.isdigit():
            return httpx.Response(int(outcome), json={"error": "synthetic"})
        if outcome == "bad-json":
            return httpx.Response(200, content=b"invalid")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(generation, "settings", gemma_profile)
    monkeypatch.setattr(generation.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    if outcome == "success":
        assert generate_text("test") == "ok"
    else:
        with pytest.raises(GenerationError):
            generate_text("test")
    assert len(calls) == 1  # No network retry or school fallback.


@pytest.mark.asyncio
async def test_single_rag_uses_shared_gemma_with_original_two_field_answer_contract(monkeypatch):
    import httpx
    from unittest.mock import AsyncMock
    from app.adapters import generation
    from app.qa.orchestrator import answer
    from tests.test_agent_graph import hit, document
    client = httpx.Client
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["model"] == config.GEMMA_MODEL
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "answer": "测试资料规定物品需要申报。[E1]", "insufficient_evidence": False})}}]})
    monkeypatch.setattr(generation, "settings", gemma_profile)
    monkeypatch.setattr(generation.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    result = await answer("物品需要申报吗？", retrieve_fn=AsyncMock(return_value=[hit()]),
                          get_documents_fn=AsyncMock(return_value={"doc_1": document()}))
    assert result.status == "ANSWERED" and result.citations[0].evidence_id == "E1"
    assert len(calls) == 1 and "response_format" not in calls[0]  # Existing Single contract retained.

@pytest.mark.parametrize('returned,expected',[('actual-server','actual-server'),(None,'requested'),('','requested')])
def test_generation_preserves_response_model_or_actual_request(monkeypatch,returned,expected):
    import httpx
    from app.adapters import generation
    client=httpx.Client
    monkeypatch.setattr(generation,'settings',lambda:{'GENERATION_MODEL':'requested','GENERATION_BASE_URL':'https://synthetic.invalid/v1'})
    monkeypatch.setattr(generation.httpx,'Client',lambda **kw:client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'model':returned,'choices':[{'message':{'content':'text'}}]})),**kw))
    assert generation.generate('test').model_name==expected
