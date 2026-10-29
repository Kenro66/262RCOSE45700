import asyncio
import json
import re
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.adapters.generation import GenerationError
from app.contracts import ChatResponse, ChatStatus
from app.qa.orchestrator import AnswerError, answer


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Synthetic evidence, not an assertion of real customs allowances.
        self.retrieve = AsyncMock(return_value=[dict(chunk_id="DOC001_C001", document_id="DOC001",
                         page_number=13, text="测试资料：示例香烟限量为400支。", score=.91)])
        self.documents = AsyncMock(return_value={"DOC001": {"filename": "test-policy.pdf", "status": "READY"}})
        self.generate = Mock(return_value=self.output("测试资料中的示例限量为400支。[E1]"))

    def output(self, text, insufficient=False):
        return json.dumps({"answer": text, "insufficient_evidence": insufficient}, ensure_ascii=False)

    async def run_answer(self, question="香烟可以带多少？", **overrides):
        dependencies = dict(retrieve_fn=self.retrieve, get_documents_fn=self.documents, generate_fn=self.generate)
        return await answer(question, **{**dependencies, **overrides})

    async def test_chinese_answer_c5_and_single_retrieval(self):
        result = await self.run_answer()
        self.assertIsInstance(result, ChatResponse)
        self.assertEqual(result.status, ChatStatus.ANSWERED)
        self.assertEqual(result.citations[0].page, 13)
        self.retrieve.assert_awaited_once_with("香烟可以带多少？")
        self.documents.assert_awaited_once_with(["DOC001"])
        self.generate.assert_called_once()

    async def test_korean_answer(self):
        self.generate.return_value = self.output("테스트 자료의 예시 한도는 400개입니다.[E1]")
        self.assertEqual((await self.run_answer("담배는 얼마나 가져갈 수 있나요?")).status, ChatStatus.ANSWERED)

    async def test_period_before_digit_rejects_uncited_second_sentence(self):
        self.generate.return_value = self.output("必须申报[E1].1件物品无需申报。")
        with self.assertRaisesRegex(AnswerError, "Every generated sentence"):
            await self.run_answer()

    async def test_period_before_digit_accepts_cited_sentences_without_rewriting(self):
        self.retrieve.return_value[0]["text"] = "测试资料：必须申报，示例数量为1件。"
        text = "必须申报[E1].1件物品应当申报。[E1]"
        self.generate.return_value = self.output(text)
        result = await self.run_answer()
        self.assertEqual(result.status, ChatStatus.ANSWERED)
        self.assertEqual(result.answer, text)

    async def test_decimal_answer_keeps_displayed_text(self):
        self.retrieve.return_value[0]["text"] = "测试资料：示例数量为1.5件。"
        for amount in ("1.5", "１．５"):
            with self.subTest(amount=amount):
                text = f"示例数量为{amount}件。[E1]"
                self.generate.return_value = self.output(text)
                result = await self.run_answer()
                self.assertEqual(result.status, ChatStatus.ANSWERED)
                self.assertEqual(result.answer, text)

    async def test_unknown_e9_removed(self):
        self.generate.return_value = self.output("测试示例为400支。[E1][E9]")
        result = await self.run_answer()
        self.assertEqual([c.evidence_id for c in result.citations], ["E1"])
        self.assertNotIn("E9", result.answer)

    async def test_only_invalid_or_no_references_returns_safe_refusal(self):
        for generated in ("臆造限量9999。[E9]", "臆造限量9999。"):
            with self.subTest(generated=generated):
                self.generate.return_value = self.output(generated)
                result = await self.run_answer()
                self.assertEqual(result.status, ChatStatus.INSUFFICIENT_EVIDENCE)
                self.assertEqual(result.citations, [])
                self.assertIsNone(re.search(r"\d", result.answer))

    async def test_no_alcohol_evidence_does_not_generate_numbers(self):
        self.retrieve.return_value = []
        result = await self.run_answer("带酒入境可以带多少")
        self.assertEqual(result.status, ChatStatus.INSUFFICIENT_EVIDENCE)
        self.assertIsNone(re.search(r"\d", result.answer))
        self.generate.assert_not_called()
        self.documents.assert_not_awaited()

    async def test_model_refusal_with_unrelated_hits_discards_generated_numbers(self):
        self.generate.return_value = self.output("无法支持，猜测9999。", True)
        result = await self.run_answer("带酒入境可以带多少")
        self.assertEqual(result.status, ChatStatus.INSUFFICIENT_EVIDENCE)
        self.assertNotIn("9999", result.answer)

    async def test_dependencies_propagate_original_errors(self):
        for function in (self.retrieve, self.documents, self.generate):
            error = GenerationError("dependency failed")
            function.side_effect = error
            with self.subTest(function=function), self.assertRaises(GenerationError) as caught:
                await self.run_answer()
            self.assertIs(caught.exception, error)
            function.side_effect = None

    async def test_malformed_model_output_raises(self):
        for raw in (None, "", "not JSON", "[]", '{}', self.output(" "),
                    '{"answer":"说明[E1]","insufficient_evidence":"false"}'):
            with self.subTest(raw=raw), self.assertRaises(AnswerError):
                await self.run_answer(generate_fn=Mock(return_value=raw))

    async def test_wrong_language_raises(self):
        self.generate.return_value = self.output("한국어 답변입니다.[E1]")
        with self.assertRaises(AnswerError):
            await self.run_answer()

    async def test_missing_source_or_nonready_source_skips_model(self):
        for docs in ({}, {"DOC001": {"filename": "x.pdf", "status": "FAILED"}}):
            self.documents.return_value = docs
            result = await self.run_answer()
            self.assertEqual(result.status, ChatStatus.INSUFFICIENT_EVIDENCE)
        self.generate.assert_not_called()

    async def test_default_generation_reuses_issue10_adapter(self):
        with patch("app.qa.orchestrator.generate_text", return_value=self.output("示例说明。[E1]")) as generate:
            await self.run_answer(generate_fn=None)
            generate.assert_called_once()

    async def test_missing_dependencies_and_sync_results_are_errors(self):
        with self.assertRaises(AnswerError):
            await answer("问题是什么？")
        with self.assertRaises(TypeError):
            await self.run_answer(retrieve_fn=lambda question: [])

    async def test_session_does_not_enable_history_and_ids_restart(self):
        first = await self.run_answer(session_id="first")
        second = await self.run_answer(session_id="second")
        self.assertEqual(first.citations[0].evidence_id, "E1")
        self.assertEqual(second.citations[0].evidence_id, "E1")

    async def test_invalid_document_metadata_is_error(self):
        self.documents.return_value = {"DOC001": {"file_name": "x.pdf", "status": "READY"}}
        with self.assertRaises(ValueError):
            await self.run_answer()
        self.generate.assert_not_called()

    async def test_uncited_conclusion_is_rejected_even_with_valid_e1(self):
        for text in ("示例为400支。[E1] 无需申报。", "示例为400支。[E1] 限额9999。[E9]"):
            self.generate.return_value = self.output(text)
            with self.subTest(text=text), self.assertRaisesRegex(AnswerError, "Every generated sentence"):
                await self.run_answer()

    async def test_duplicate_json_fields_raise(self):
        raw = '{"answer":"说明[E1]","answer":"篡改[E1]","insufficient_evidence":false}'
        with self.assertRaises(AnswerError):
            await self.run_answer(generate_fn=Mock(return_value=raw))

    async def test_later_usable_evidence_keeps_its_request_local_id(self):
        self.retrieve.return_value.append(dict(chunk_id="c2", document_id="DOC002", page_number=2,
                                              text="其他测试说明。", score=.8))
        self.documents.return_value = {"DOC002": {"filename": "other.pdf", "status": "READY"}}
        self.generate.return_value = self.output("其他测试说明。[E2]")
        result = await self.run_answer()
        payload = json.loads(self.generate.call_args.args[0][1]["content"])
        self.assertEqual([row["evidence_id"] for row in payload["evidence"]], ["E2"])
        self.assertEqual(result.citations[0].filename, "other.pdf")

    async def test_shared_document_is_looked_up_once(self):
        self.retrieve.return_value.append(dict(chunk_id="c2", document_id="DOC001", page_number=14,
                                              text="第二页示例。", score=.8))
        self.generate.return_value = self.output("两页测试说明。[E1][E2]")
        result = await self.run_answer()
        self.documents.assert_awaited_once_with(["DOC001"])
        self.assertEqual([c.page for c in result.citations], [13, 14])

    async def test_korean_empty_retrieval_refusal_has_no_digits(self):
        self.retrieve.return_value = []
        result = await self.run_answer("술은 얼마나 반입할 수 있나요?")
        self.assertEqual(result.status, ChatStatus.INSUFFICIENT_EVIDENCE)
        self.assertIn("증거", result.answer)
        self.assertIsNone(re.search(r"\d", result.answer))

    async def test_dependency_objects_can_be_replaced_without_core_changes(self):
        class Retrieval:
            async def __call__(self, question):
                from app.contracts import SearchHit
                return (SearchHit(chunk_id="replacement", document_id="new", page_number=4,
                                  text="替换来源测试。", score=.5),)
        result = await self.run_answer(retrieve_fn=Retrieval(),
                                 get_documents_fn=AsyncMock(return_value={"new": {"filename": "new.pdf", "status": "READY"}}),
                                 generate_fn=lambda messages: self.output("替换来源测试。[E1]"))
        self.assertEqual(result.citations[0].filename, "new.pdf")
        self.assertEqual(result.citations[0].quote, "替换来源测试。")

    async def test_async_dependency_result_is_awaited(self):
        async def asynchronous_result():
            return []
        result = await self.run_answer(retrieve_fn=lambda question: asynchronous_result())
        self.assertEqual(result.status, ChatStatus.INSUFFICIENT_EVIDENCE)

    async def test_invalid_sessions_raise(self):
        for session in ("", " ", 3):
            with self.subTest(session=session), self.assertRaises(ValueError):
                await self.run_answer(session_id=session)

    async def test_nonready_sources_are_not_sent_to_model(self):
        self.documents.return_value["DOC001"]["status"] = "PROCESSING"
        result = await self.run_answer()
        self.assertEqual(result.status, ChatStatus.INSUFFICIENT_EVIDENCE)
        self.generate.assert_not_called()

    async def test_async_generation_is_supported(self):
        generator = AsyncMock(return_value=self.output("示例说明。[E1]"))
        result = await self.run_answer(generate_fn=generator)
        generator.assert_awaited_once()
        self.assertEqual(result.status, ChatStatus.ANSWERED)

    async def test_synchronous_generation_runs_off_event_loop(self):
        event_thread = threading.get_ident()
        def generate(messages):
            self.assertNotEqual(threading.get_ident(), event_thread)
            return self.output("示例说明。[E1]")
        self.assertEqual((await self.run_answer(generate_fn=generate)).status, ChatStatus.ANSWERED)

    async def test_real_retriever_contract_can_be_injected(self):
        from app.qa.retriever import retrieve
        search = AsyncMock(return_value=self.retrieve.return_value)
        async def bound_retriever(question):
            return await retrieve(question, search_similar=search, embedder=Mock(return_value=[[.1] * 1024]))
        result = await self.run_answer(retrieve_fn=bound_retriever)
        search.assert_awaited_once_with([.1] * 1024, top_k=5)
        self.assertEqual(result.status, ChatStatus.ANSWERED)
        self.documents.assert_awaited_once_with(["DOC001"])

    async def test_cancellation_propagates(self):
        self.documents.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.run_answer()
