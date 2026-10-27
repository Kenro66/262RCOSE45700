import unittest
from unittest.mock import Mock, patch

import numpy as np

from app.adapters.embedding import EmbeddingError, embed_chunks
from app.contracts import Chunk, EmbeddingRecord


class IngestionEmbeddingContractTests(unittest.TestCase):
    def chunks(self):
        return [Chunk(chunk_id=f"DOC001_C{i:06d}", document_id="DOC001", chunk_index=i,
                      page_index=i, page_number=i + 1, text="测试正文", section_title=None,
                      language="zh", token_count=4, char_count=4, content_hash=None) for i in range(2)]

    def test_wrapper_reuses_embed_texts_and_keeps_order(self):
        chunks = self.chunks()
        with patch("app.adapters.embedding.embed_texts", return_value=[[.1] * 1024, [.2] * 1024]) as embed:
            records = embed_chunks(chunks)
        embed.assert_called_once_with([chunk.text for chunk in chunks])
        self.assertEqual(len(chunks), len(records))
        self.assertEqual([r.chunk_id for r in records], [c.chunk_id for c in chunks])
        self.assertTrue(all(isinstance(r, EmbeddingRecord) and len(r.vector) == 1024 for r in records))
        self.assertEqual([r.vector[0] for r in records], [.1, .2])

    def test_wrong_count_from_embed_texts_raises(self):
        with patch("app.adapters.embedding.embed_texts", return_value=[[.1] * 1024]):
            with self.assertRaises(ValueError):
                embed_chunks(self.chunks())

    def test_actual_adapter_path_rejects_wrong_dimensions_and_counts(self):
        environment = {"EMBEDDING_MODEL": "unit-model", "EMBEDDING_BASE_URL": "", "EMBEDDING_DIMENSION": "1024"}
        for vectors in ([[1.] * 1023] * 2, [[1.] * 1024], [[1.] * 1024] * 3):
            model = Mock()
            model.encode.return_value = {"dense_vecs": np.asarray(vectors)}
            with self.subTest(count=len(vectors)), patch.dict("os.environ", environment), \
                    patch("app.adapters.embedding._model", return_value=model), self.assertRaises(EmbeddingError):
                embed_chunks(self.chunks())

    def test_empty_input_remains_empty(self):
        with patch("app.adapters.embedding._model") as model:
            self.assertEqual(embed_chunks([]), [])
            model.assert_not_called()
