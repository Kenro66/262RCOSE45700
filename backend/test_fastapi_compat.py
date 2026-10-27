"""Orchestration tests with explicit test doubles; NOT real-model acceptance."""

import contextlib
import io
import json
import math
import os
import unittest
from unittest.mock import patch

from app.main import app
import smoke_fastapi


class FastAPICompatibilityTests(unittest.TestCase):
    def tearDown(self):
        app.router.routes[:] = [r for r in app.routes if getattr(r, "path", None) != "/_compat/rag"]

    def run_probe(self, vectors=None, text="Test response", embedding_error=None, generation_error=None):
        vectors = [[1.0] * 1024 for _ in range(2)] if vectors is None else vectors
        with patch("app.adapters.embedding.embed_texts", return_value=vectors, side_effect=embedding_error) as embed, patch(
            "app.adapters.generation.generate_text", return_value=text, side_effect=generation_error
        ) as generate, contextlib.redirect_stdout(io.StringIO()) as output:
            code = smoke_fastapi.main()
        return code, json.loads(output.getvalue()), embed, generate

    def test_success_uses_each_adapter_once_in_same_process(self):
        code, report, embed, generate = self.run_probe()
        self.assertEqual(code, 0)
        self.assertTrue(report["same_process"])
        self.assertEqual(report["embedding"]["pid"], os.getpid())
        self.assertEqual(report["generation"]["pid"], os.getpid())
        self.assertEqual(len(embed.call_args.args[0]), 2)
        embed.assert_called_once()
        generate.assert_called_once()

    def test_rejects_invalid_vectors(self):
        for vectors in ([], [[1.0] * 1023] * 2, [[math.nan] * 1024] * 2, [[0.0] * 1024] * 2, [[True] * 1024] * 2):
            with self.subTest(vectors_length=len(vectors)):
                code, report, _, generate = self.run_probe(vectors=vectors)
                self.assertEqual(code, 1)
                self.assertEqual(report["failed_stage"], "embedding")
                generate.assert_not_called()
                self.tearDown()

    def test_rejects_empty_generation(self):
        code, report, _, _ = self.run_probe(text="  ")
        self.assertEqual(code, 1)
        self.assertEqual(report["failed_stage"], "generation")

    def test_failure_does_not_leak_exception_details(self):
        for stage in ("embedding", "generation"):
            kwargs = {stage + "_error": RuntimeError("SECRET-key-and-private-response")}
            code, report, _, _ = self.run_probe(**kwargs)
            self.assertEqual(code, 1)
            self.assertEqual(report["failed_stage"], stage)
            self.assertNotIn("SECRET", json.dumps(report))
            self.tearDown()

    def test_startup_failure_is_nonzero(self):
        with patch.object(smoke_fastapi, "create_probe_app", side_effect=ImportError("SECRET")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(smoke_fastapi.main(), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["failed_stage"], "startup")
        self.assertNotIn("SECRET", output.getvalue())


if __name__ == "__main__":
    unittest.main()
