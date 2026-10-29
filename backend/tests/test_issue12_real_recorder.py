import argparse
import asyncio
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.record_issue12_real import main, record, source_hashes


class RealRecorderSafetyTests(unittest.TestCase):
    def test_no_real_calls_without_explicit_opt_in(self):
        with patch("sys.argv", ["record", "--output", "unused.json"]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 2)

    def test_rejects_missing_or_non_postgres_url_without_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            args = argparse.Namespace(output=path, env_file=None)
            for value in ("", "sqlite+aiosqlite:///:memory:"):
                with self.subTest(url=value), patch.dict(os.environ, {"DATABASE_URL": value}):
                    with self.assertRaises(ValueError):
                        asyncio.run(record(args))
            self.assertFalse(path.exists())

    def test_existing_run_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text("existing evidence", encoding="utf-8")
            with self.assertRaises(ValueError):
                asyncio.run(record(argparse.Namespace(output=path)))
            self.assertEqual(path.read_text(encoding="utf-8"), "existing evidence")

    def test_source_fingerprints_exclude_private_config(self):
        hashes = source_hashes()
        self.assertIn("app/qa/orchestrator.py", hashes)
        self.assertTrue(all(len(value) == 64 for value in hashes.values()))
        self.assertFalse(any(name.endswith(".env") for name in hashes))
