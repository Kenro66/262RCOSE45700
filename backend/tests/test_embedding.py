import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from app.adapters.embedding import EmbeddingError, embed_chunks, embed_texts
from app.contracts import Chunk


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"EMBEDDING_MODEL": "test-model", "EMBEDDING_BASE_URL": "", "EMBEDDING_DIMENSION": "1024", "EMBEDDING_BATCH_SIZE": "1", "EMBEDDING_MAX_LENGTH": "8192"})
        env.start()
        self.addCleanup(env.stop)

    def model(self, rows):
        model = Mock()
        model.encode.return_value = {"dense_vecs": np.asarray(rows)}
        return model

    def test_bilingual_order(self):
        model = self.model([[1.] * 1024, [2.] * 1024])
        with patch("app.adapters.embedding._model", return_value=model):
            result = embed_texts(["\u4e2d\u6587", "\ud55c\uad6d\uc5b4"])
        self.assertEqual([v[0] for v in result], [1., 2.])
        self.assertEqual([len(v) for v in result], [1024, 1024])
        self.assertFalse(model.encode.call_args.kwargs["return_sparse"])

    def test_bad_vectors(self):
        for rows in ([[1.]], [[float("nan")] * 1024], [[0.] * 1024], [[1.] * 1024] * 2):
            with self.subTest(rows=len(rows)), patch("app.adapters.embedding._model", return_value=self.model(rows)):
                with self.assertRaises(EmbeddingError):
                    embed_texts(["hello"])

    def test_invalid_input(self):
        for texts in ("hello", [""], ["  "], [None]):
            with self.subTest(texts=texts), self.assertRaises(ValueError):
                embed_texts(texts)
        with patch("app.adapters.embedding._model") as model:
            self.assertEqual(embed_texts([]), [])
            model.assert_not_called()

    def test_failure_is_not_silenced(self):
        with patch("app.adapters.embedding._model", side_effect=OSError("load failed")):
            with self.assertRaisesRegex(EmbeddingError, "inference failed") as error:
                embed_texts(["hello"])
            self.assertIsInstance(error.exception.__cause__, OSError)

    def test_remote_is_not_silently_ignored(self):
        with patch.dict(os.environ, {"EMBEDDING_BASE_URL": "http://127.0.0.1:1/v1"}):
            with self.assertRaisesRegex(EmbeddingError, "protocol is pending"):
                embed_texts(["hello"])

    def test_chunk_mapping_and_input(self):
        chunk = Chunk(chunk_id="c1", document_id="d1", chunk_index=0, page_index=0, page_number=1,
                      text="body", section_title="title", language="en", token_count=1, char_count=4, content_hash=None)
        with patch("app.adapters.embedding.embed_texts", return_value=[[1.] * 1024]) as embed:
            result = embed_chunks([chunk])
            embed.assert_called_once_with(["title\n\nbody"])
            self.assertEqual(result[0].chunk_id, "c1")
            self.assertEqual(chunk.text, "body")
        chunk.text = " "
        with self.assertRaises(ValueError):
            embed_chunks([chunk])

    def test_env_file_changes_model_without_code_changes(self):
        from app.adapters import config

        # Use the real dotenv parser while redirecting only the config file path.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            model = self.model([[1.] * 1024])
            original = config.dotenv_values
            with patch.dict(os.environ, {}, clear=True), patch.object(config, "dotenv_values", side_effect=lambda _: original(path)), patch("app.adapters.embedding._model", return_value=model) as load:
                for name in ("model-a", "model-b"):
                    path.write_text(f"EMBEDDING_MODEL={name}\n", encoding="utf-8")
                    embed_texts(["hello"])
                    self.assertEqual(load.call_args.args[0], name)


if __name__ == "__main__":
    unittest.main()
