"""Real policy extracts plus synthetic scope counterexamples; no model calls."""
import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.contracts import Chunk, SearchHit
from app.qa.citation import assemble_evidence
from app.qa.evidence_completion import complete_evidence, find_supplemental
from app.qa.orchestrator import answer


def fixture_chunks():
    path = Path(__file__).parent / 'fixtures/evidence_completion_ko.json'
    return [Chunk.model_validate(c) for c in json.loads(path.read_text(encoding='utf-8'))['chunks']]


def source_hit(chunk):
    return SearchHit(chunk_id=chunk.chunk_id, document_id=chunk.document_id,
                     page_number=chunk.page_number, text=chunk.text, score=.5)


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.chunks = fixture_chunks()
        self.by_id = {c.chunk_id: c for c in self.chunks}
        self.source = self.by_id['DOC003_C000039']
        self.target = self.by_id['DOC003_C000013']
        self.initial = [source_hit(self.source)]

    def test_real_age_exception_and_original_metadata(self):
        result = find_supplemental(self.initial, self.chunks)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].chunk, self.target)
        self.assertEqual(result[0].related_chunk_id, self.source.chunk_id)

    def test_all_real_stress_counterexamples_are_rejected(self):
        for chunk in self.chunks:
            if chunk.chunk_id != self.target.chunk_id:
                with self.subTest(chunk_id=chunk.chunk_id):
                    self.assertEqual(find_supplemental(self.initial, [self.source, chunk]
                        if chunk.chunk_id != self.source.chunk_id else [self.source]), [])

    def test_duplicate_initial_is_not_added(self):
        self.assertEqual(find_supplemental([*self.initial, source_hit(self.target)], self.chunks), [])

    def test_two_valid_definitions_are_ambiguous(self):
        other = self.target.model_copy(update={'chunk_id': 'OTHER', 'chunk_index': 14})
        self.assertEqual(find_supplemental(self.initial, [*self.chunks, other]), [])

    def test_existing_definition_does_not_hide_ambiguity(self):
        other = self.target.model_copy(update={'chunk_id': 'OTHER', 'chunk_index': 14})
        self.assertEqual(find_supplemental([*self.initial, source_hit(self.target)], [*self.chunks, other]), [])

    def test_no_match_and_empty_input(self):
        self.assertEqual(find_supplemental(self.initial, [self.source]), [])
        self.assertEqual(find_supplemental([], self.chunks), [])

    def test_identity_or_local_scope_conflict_is_not_added(self):
        for prefix in ('승무원 중 ', '외국인 중 ', '특정 대상에 해당하는 ', '이 조에서만 '):
            with self.subTest(prefix=prefix):
                text = self.target.text.replace('다만, 만 19세', f'다만, {prefix}만 19세')
                negative = self.target.model_copy(update={'text': text})
                self.assertEqual(find_supplemental(self.initial, [self.source, negative]), [])

    def test_local_scope_inside_note_is_rejected(self):
        for scope in ('편', '장', '절', '관', '조', '항', '호', '목'):
            with self.subTest(scope=scope):
                negative = self.target.model_copy(update={'text': self.target.text.replace(
                    '이하 같다', f'이 {scope}에서만 이하 같다')})
                self.assertEqual(find_supplemental(self.initial, [self.source, negative]), [])

    def test_local_scope_in_source_or_candidate_title_is_rejected(self):
        for scope in ('편', '장', '절', '관', '조', '항', '호', '목'):
            for role in ('source', 'candidate'):
                with self.subTest(scope=scope, role=role):
                    title = f'이 {scope}에서만 적용'
                    source = self.source.model_copy(update={'section_title': title}) if role == 'source' else self.source
                    target = self.target.model_copy(update={'section_title': title}) if role == 'candidate' else self.target
                    self.assertEqual(find_supplemental(self.initial, [source, target]), [])

    def test_note_markers_must_be_inside_adjacent_parenthesis(self):
        text = self.target.text.replace('제외한다. 이하 같다)', '제외한다). 이하 같다')
        negative = self.target.model_copy(update={'text': text})
        self.assertEqual(find_supplemental(self.initial, [self.source, negative]), [])

    def test_source_identity_scope_is_not_assumed_universal(self):
        source = source_hit(self.source.model_copy(update={'text': self.source.text.replace(
            '만 19세 미만인 자가', '승무원 중 만 19세 미만인 자가')}))
        self.assertEqual(find_supplemental([source], self.chunks), [])

    def test_mismatched_source_snapshot_and_other_document_are_rejected(self):
        source = self.source.model_copy(update={'text': self.source.text + ' changed'})
        self.assertEqual(find_supplemental(self.initial, [source, self.target]), [])
        target = self.target.model_copy(update={'document_id': 'OTHER'})
        self.assertEqual(find_supplemental(self.initial, [self.source, target]), [])
        target = self.target.model_copy(update={'language': None})
        self.assertEqual(find_supplemental(self.initial, [self.source, target]), [])


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.chunks = fixture_chunks()
        self.source = next(c for c in self.chunks if c.chunk_id == 'DOC003_C000039')
        self.initial = [source_hit(self.source)]

    async def test_async_lookup_once(self):
        lookup = AsyncMock(return_value=self.chunks)
        result = await complete_evidence(self.initial, load_chunks_fn=lookup)
        lookup.assert_awaited_once_with(['DOC003'])
        self.assertEqual(len(result), 1)

    async def test_read_failure_falls_back(self):
        with self.assertLogs('app.qa.evidence_completion', level='WARNING'):
            self.assertEqual(await complete_evidence(self.initial,
                load_chunks_fn=AsyncMock(side_effect=OSError('unavailable'))), [])

    async def test_non_ready_or_missing_sources_are_empty(self):
        self.assertEqual(await complete_evidence(self.initial, load_chunks_fn=AsyncMock(return_value=[])), [])

    async def test_cancellation_propagates(self):
        with self.assertRaises(asyncio.CancelledError):
            await complete_evidence(self.initial, load_chunks_fn=AsyncMock(side_effect=asyncio.CancelledError))

    async def test_no_age_rule_does_not_query(self):
        lookup = AsyncMock()
        hit = self.initial[0].model_copy(update={'text': '신고서를 제출해야 한다.'})
        self.assertEqual(await complete_evidence([hit], load_chunks_fn=lookup), [])
        lookup.assert_not_called()


class OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.chunks = fixture_chunks()
        self.source = next(c for c in self.chunks if c.chunk_id == 'DOC003_C000039')
        self.initial = [source_hit(self.source)] + [SearchHit(chunk_id=f'BASE{i}', document_id='DOC003',
            page_number=9, text='기존 증거입니다.', score=.4 - i * .01) for i in range(4)]
        self.supplemental = find_supplemental(self.initial, self.chunks)
        self.retrieve = AsyncMock(return_value=self.initial)
        self.lookup = AsyncMock(return_value={'DOC003': {'filename': 'actual-policy.pdf', 'status': 'READY'}})

    async def run_answer(self, completion, text='근거에 따른 답변입니다.[E1]'):
        generator = Mock(return_value=json.dumps({'answer': text, 'insufficient_evidence': False}))
        result = await answer('담배 면세가 적용되나요?', retrieve_fn=self.retrieve,
            get_documents_fn=self.lookup, generate_fn=generator, complete_evidence_fn=completion)
        return result, generator

    async def test_separate_groups_original_top5_and_true_citation(self):
        original = [h.model_dump() for h in self.initial]
        completion = AsyncMock(return_value=self.supplemental)
        with patch('app.qa.orchestrator.settings', return_value={'EVIDENCE_COMPLETION_ENABLED': 'true'}), \
                patch('app.qa.orchestrator.assemble_evidence', wraps=assemble_evidence) as assembly:
            result, generator = await self.run_answer(completion, '예외도 확인해야 합니다.[E6]')
        initial, supplemental = assembly.call_args.args
        self.assertEqual([h.model_dump() for h in initial], original)
        self.assertEqual([h.model_dump() for h in self.initial], original)
        self.assertEqual(supplemental, self.supplemental)
        payload = json.loads(generator.call_args.args[0][1]['content'])
        self.assertEqual([e['evidence_id'] for e in payload['evidence']], ['E1', 'E2', 'E3', 'E4', 'E5', 'E6'])
        citation = result.citations[0]
        self.assertEqual((citation.filename, citation.page, citation.quote),
            ('actual-policy.pdf', self.supplemental[0].chunk.page_number, self.supplemental[0].chunk.text))
        self.assertFalse(hasattr(self.supplemental[0].chunk, 'score'))

    async def test_disabled_has_no_completion_call(self):
        completion = AsyncMock(return_value=self.supplemental)
        with patch('app.qa.orchestrator.settings', return_value={}):
            _, generator = await self.run_answer(completion)
        completion.assert_not_called()
        payload = json.loads(generator.call_args.args[0][1]['content'])
        self.assertEqual(len(payload['evidence']), 5)

    async def test_invalid_flag_stays_disabled(self):
        completion = AsyncMock(return_value=self.supplemental)
        with patch('app.qa.orchestrator.settings', return_value={'EVIDENCE_COMPLETION_ENABLED': 'yes'}):
            await self.run_answer(completion)
        completion.assert_not_called()

    async def test_completion_cannot_mutate_initial_hits(self):
        original = self.initial[0].text
        async def completion(hits):
            hits[0].text = 'changed by callback'
            return []
        with patch('app.qa.orchestrator.settings', return_value={'EVIDENCE_COMPLETION_ENABLED': 'true'}):
            await self.run_answer(completion)
        self.assertEqual(self.initial[0].text, original)

    async def test_empty_initial_never_calls_completion(self):
        self.retrieve.return_value = []
        completion = AsyncMock()
        with patch('app.qa.orchestrator.settings', return_value={'EVIDENCE_COMPLETION_ENABLED': 'true'}):
            result, generator = await self.run_answer(completion)
        self.assertEqual(result.status.value, 'INSUFFICIENT_EVIDENCE')
        completion.assert_not_called()
        generator.assert_not_called()

    async def test_invalid_completion_result_falls_back(self):
        with patch('app.qa.orchestrator.settings', return_value={'EVIDENCE_COMPLETION_ENABLED': 'true'}):
            for rows in (None, [object()], self.supplemental * 2):
                with self.subTest(rows=type(rows).__name__), self.assertLogs('app.qa.orchestrator', level='WARNING'):
                    result, generator = await self.run_answer(AsyncMock(return_value=rows))
                self.assertTrue(result.citations)
                self.assertEqual(len(json.loads(generator.call_args.args[0][1]['content'])['evidence']), 5)

    async def test_empty_completion_is_identical_context(self):
        with patch('app.qa.orchestrator.settings', return_value={}):
            first, gen1 = await self.run_answer(AsyncMock())
        with patch('app.qa.orchestrator.settings', return_value={'EVIDENCE_COMPLETION_ENABLED': 'true'}):
            second, gen2 = await self.run_answer(AsyncMock(return_value=[]))
        self.assertEqual(first, second)
        self.assertEqual(gen1.call_args, gen2.call_args)

    async def test_completion_error_falls_back_but_cancel_propagates(self):
        with patch('app.qa.orchestrator.settings', return_value={'EVIDENCE_COMPLETION_ENABLED': 'true'}):
            with self.assertLogs('app.qa.orchestrator', level='WARNING'):
                result, _ = await self.run_answer(AsyncMock(side_effect=OSError('unavailable')))
            self.assertTrue(result.citations)
            with self.assertRaises(asyncio.CancelledError):
                await self.run_answer(AsyncMock(side_effect=asyncio.CancelledError))

    def test_assembly_caps_and_deduplicates_without_scores(self):
        self.assertEqual(len(assemble_evidence(self.initial, self.supplemental * 2)), 5)
        target = source_hit(self.supplemental[0].chunk)
        self.assertEqual(len(assemble_evidence([target], self.supplemental)), 1)
