import asyncio
import os
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.contracts import SearchHit
from app.qa.retriever import RetrievalError, retrieve


def hits(count):
    return [SearchHit(chunk_id=f"DOC003_C{i:06d}", document_id="DOC003", page_number=i + 1, text="海关依法办理申报。", score=0.9 - i * .01) for i in range(count)]


class RetrieverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"RETRIEVAL_TOP_K": "5"})
        env.start()
        self.addCleanup(env.stop)
        self.embed = Mock(return_value=[[.1] * 1024])

    async def test_chinese_question_and_default_top5(self):
        search = AsyncMock(return_value=hits(8))
        result = await retrieve("中国入境行李应当如何申报？", search_similar=search, embedder=self.embed)
        self.embed.assert_called_once_with(["中国入境行李应当如何申报？"])
        search.assert_awaited_once_with([.1] * 1024, top_k=5)
        self.assertEqual(len(result), 5)
        self.assertTrue(all(isinstance(hit, SearchHit) and hit.document_id == "DOC003" for hit in result))

    async def test_topk_config_and_override(self):
        search = AsyncMock(return_value=hits(8))
        with patch.dict(os.environ, {"RETRIEVAL_TOP_K": "3"}):
            self.assertEqual(len(await retrieve("问题", search_similar=search, embedder=self.embed)), 3)
            self.assertEqual(len(await retrieve("问题", search_similar=search, embedder=self.embed, top_k=2)), 2)

    async def test_empty_results_stay_empty(self):
        self.assertEqual(await retrieve("问题", search_similar=AsyncMock(return_value=[]), embedder=self.embed), [])

    async def test_contract_dicts_preserve_metadata_and_backend_order(self):
        rows = [h.model_dump() for h in reversed(hits(3))]
        result = await retrieve("问题", search_similar=AsyncMock(return_value=rows), embedder=self.embed)
        self.assertEqual([h.model_dump() for h in result], rows)
        self.assertEqual(set(result[0].model_dump()),
                         {"chunk_id", "document_id", "page_number", "text", "score"})

    async def test_tuple_of_search_hits_is_supported(self):
        rows = tuple(hits(2))
        self.assertEqual(await retrieve("问题", search_similar=AsyncMock(return_value=rows),
                                  embedder=self.embed), list(rows))

    async def test_invalid_evidence_is_not_returned(self):
        for field, value in (("chunk_id", " "), ("document_id", ""),
                             ("text", "\n "), ("score", float("inf"))):
            row = hits(1)[0].model_dump()
            row[field] = value
            with self.subTest(field=field), self.assertRaises(RetrievalError):
                await retrieve("问题", search_similar=AsyncMock(return_value=[row]), embedder=self.embed)

    async def test_async_search_is_awaited(self):
        calls = []
        async def search(query_vector, *, top_k):
            await asyncio.sleep(0)
            calls.append(top_k)
            return []
        self.assertEqual(await retrieve("问题", search_similar=search, embedder=self.embed), [])
        self.assertEqual(calls, [5])

    async def test_synchronous_search_is_not_silently_accepted(self):
        with self.assertRaises(RetrievalError) as error:
            await retrieve("问题", search_similar=Mock(return_value=[]), embedder=self.embed)
        self.assertIsInstance(error.exception.__cause__, TypeError)

    async def test_embedding_runs_off_event_loop_thread(self):
        event_thread = threading.get_ident()
        def embed(texts):
            self.assertNotEqual(threading.get_ident(), event_thread)
            return [[.1] * 1024]
        await retrieve("问题", search_similar=AsyncMock(return_value=[]), embedder=embed)

    async def test_cancellation_propagates(self):
        with self.assertRaises(asyncio.CancelledError):
            await retrieve("问题", search_similar=AsyncMock(side_effect=asyncio.CancelledError), embedder=self.embed)

    async def test_backend_failure_preserves_cause(self):
        with self.assertRaises(RetrievalError) as error:
            await retrieve("问题", search_similar=AsyncMock(side_effect=OSError("database unavailable")), embedder=self.embed)
        self.assertIsInstance(error.exception.__cause__, OSError)

    async def test_embedding_failure_does_not_call_backend(self):
        search = AsyncMock()
        with self.assertRaises(RetrievalError):
            await retrieve("问题", search_similar=search, embedder=Mock(side_effect=RuntimeError("model unavailable")))
        search.assert_not_awaited()

    async def test_bad_vectors_and_bad_rows(self):
        for vector in ([], [0.] * 1024, [float("nan")] * 1024, [1.] * 10):
            with self.subTest(size=len(vector)), self.assertRaises(RetrievalError):
                await retrieve("问题", search_similar=AsyncMock(return_value=[]), embedder=Mock(return_value=[vector]))
        for rows in ([{}], None, [{"chunk_id":"c", "document_id":"d", "page_number":0, "text":"x", "score":1}]):
            with self.subTest(rows=rows), self.assertRaises(RetrievalError):
                await retrieve("问题", search_similar=AsyncMock(return_value=rows), embedder=self.embed)

    async def test_invalid_query_and_topk(self):
        for question in (None, "", " "):
            with self.assertRaises(ValueError):
                await retrieve(question, search_similar=AsyncMock(), embedder=self.embed)
        for top_k in (0, 101, True, 2.5):
            with self.assertRaises(ValueError):
                await retrieve("问题", top_k=top_k, search_similar=AsyncMock(), embedder=self.embed)
