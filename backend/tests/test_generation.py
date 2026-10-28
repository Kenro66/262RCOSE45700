import json
import os
import socket
import tempfile
import threading
import unittest
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
        env = patch.dict(os.environ, {"GENERATION_MODEL": "test", "GENERATION_BASE_URL": self.base, "GENERATION_API_KEY": "", "GENERATION_TIMEOUT_SECONDS": "3"})
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
