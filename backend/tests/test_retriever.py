import os
import unittest
from unittest.mock import Mock, patch

from app.contracts import SearchHit
from app.qa.retriever import RetrievalError, retrieve


def hits(count):
    return [SearchHit(chunk_id=f"DOC003_C{i:06d}", document_id="DOC003", page_number=i + 1, text="海关依法办理申报。", score=0.9 - i * .01) for i in range(count)]


class RetrieverTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"RETRIEVAL_TOP_K": "5"})
        env.start()
        self.addCleanup(env.stop)
        self.embed = Mock(return_value=[[.1] * 1024])

    def test_chinese_question_and_default_top5(self):
        search = Mock(return_value=hits(8))
        result = retrieve("中国入境行李应当如何申报？", search_similar=search, embedder=self.embed)
        self.embed.assert_called_once_with(["中国入境行李应当如何申报？"])
        search.assert_called_once_with([.1] * 1024, top_k=5)
        self.assertEqual(len(result), 5)
        self.assertTrue(all(isinstance(hit, SearchHit) and hit.document_id == "DOC003" for hit in result))

    def test_topk_config_and_override(self):
        search = Mock(return_value=hits(8))
        with patch.dict(os.environ, {"RETRIEVAL_TOP_K": "3"}):
            self.assertEqual(len(retrieve("问题", search_similar=search, embedder=self.embed)), 3)
            self.assertEqual(len(retrieve("问题", search_similar=search, embedder=self.embed, top_k=2)), 2)

    def test_empty_results_stay_empty(self):
        self.assertEqual(retrieve("问题", search_similar=Mock(return_value=[]), embedder=self.embed), [])

    def test_contract_dicts_preserve_metadata_and_backend_order(self):
        rows = [h.model_dump() for h in reversed(hits(3))]
        result = retrieve("问题", search_similar=Mock(return_value=rows), embedder=self.embed)
        self.assertEqual([h.model_dump() for h in result], rows)
        self.assertEqual(set(result[0].model_dump()),
                         {"chunk_id", "document_id", "page_number", "text", "score"})

    def test_tuple_of_search_hits_is_supported(self):
        rows = tuple(hits(2))
        self.assertEqual(retrieve("问题", search_similar=Mock(return_value=rows),
                                  embedder=self.embed), list(rows))

    def test_invalid_evidence_is_not_returned(self):
        for field, value in (("chunk_id", " "), ("document_id", ""),
                             ("text", "\n "), ("score", float("inf"))):
            row = hits(1)[0].model_dump()
            row[field] = value
            with self.subTest(field=field), self.assertRaises(RetrievalError):
                retrieve("问题", search_similar=Mock(return_value=[row]), embedder=self.embed)

    def test_async_search_requires_explicit_integration(self):
        async def search(query_vector, *, top_k):
            return []
        with self.assertRaisesRegex(ValueError, "synchronous"):
            retrieve("问题", search_similar=search, embedder=self.embed)
        self.embed.assert_not_called()

    def test_backend_failure_preserves_cause(self):
        with self.assertRaises(RetrievalError) as error:
            retrieve("问题", search_similar=Mock(side_effect=OSError("database unavailable")), embedder=self.embed)
        self.assertIsInstance(error.exception.__cause__, OSError)

    def test_embedding_failure_does_not_call_backend(self):
        search = Mock()
        with self.assertRaises(RetrievalError):
            retrieve("问题", search_similar=search, embedder=Mock(side_effect=RuntimeError("model unavailable")))
        search.assert_not_called()

    def test_bad_vectors_and_bad_rows(self):
        for vector in ([], [0.] * 1024, [float("nan")] * 1024, [1.] * 10):
            with self.subTest(size=len(vector)), self.assertRaises(RetrievalError):
                retrieve("问题", search_similar=Mock(return_value=[]), embedder=Mock(return_value=[vector]))
        for rows in ([{}], None, [{"chunk_id":"c", "document_id":"d", "page_number":0, "text":"x", "score":1}]):
            with self.subTest(rows=rows), self.assertRaises(RetrievalError):
                retrieve("问题", search_similar=Mock(return_value=rows), embedder=self.embed)

    def test_invalid_query_and_topk(self):
        for question in (None, "", " "):
            with self.assertRaises(ValueError):
                retrieve(question, search_similar=Mock(), embedder=self.embed)
        for top_k in (0, 101, True, 2.5):
            with self.assertRaises(ValueError):
                retrieve("问题", top_k=top_k, search_similar=Mock(), embedder=self.embed)
