"""Recorded Q008 inputs and constructed counterexamples; no real model calls."""
import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.contracts import Chunk, SearchHit
from app.qa.evidence_completion import complete_evidence, find_supplemental
from app.qa.orchestrator import answer
from tests.test_evidence_completion import fixture_chunks, source_hit


def chinese_fixture():
    return json.loads((Path(__file__).parent / 'fixtures/evidence_completion_zh.json').read_text(encoding='utf-8'))


def chain_chunks():
    # Statistics are test placeholders, not a new token-count or PDF acceptance.
    return [Chunk.model_validate({'schema_version': '1.0', 'language': 'zh', 'token_count': 0,
        'char_count': len(row['text']), 'content_hash': None, **row}) for row in chinese_fixture()['chain_chunks']]


class CrosspageRuleTests(unittest.TestCase):
    def setUp(self):
        fixture = chinese_fixture()
        self.question = fixture['question']
        self.initial = [SearchHit.model_validate(row) for row in fixture['initial_top5']]
        self.chunks = chain_chunks()
        self.source, self.target, self.channel = self.chunks

    def find(self, chunks=None, initial=None, question=None):
        return find_supplemental(self.initial if initial is None else initial,
            self.chunks if chunks is None else chunks, question=self.question if question is None else question)

    def reject_target(self, **changes):
        target = self.target.model_copy(update=changes)
        self.assertEqual(self.find([self.source, target, self.channel]), [])

    def test_recorded_question_finds_only_original_continuation(self):
        before = [hit.model_dump() for hit in self.initial]
        result = self.find()
        self.assertEqual([item.chunk.chunk_id for item in result], [self.target.chunk_id])
        self.assertEqual(result[0].chunk.text, self.target.text)
        self.assertEqual(result[0].related_chunk_id, self.source.chunk_id)
        self.assertIn('question_condition_overlap', result[0].signals)
        self.assertFalse(hasattr(result[0].chunk, 'score'))
        self.assertEqual([hit.model_dump() for hit in self.initial], before)

    def test_other_document_article_pages_and_condition_use_same_rule(self):
        source = self.source.model_copy(update={'chunk_id': 'GENERIC_MAIN', 'document_id': 'GENERIC',
            'chunk_index': 40, 'page_number': 5, 'page_index': 4, 'section_title': '第十七条',
            'text': self.source.text.replace('第九条', '第十七条')})
        target = self.target.model_copy(update={'chunk_id': 'GENERIC_NEXT', 'document_id': 'GENERIC',
            'chunk_index': 41, 'page_number': 6, 'page_index': 5, 'section_title': '第十七条',
            'text': self.target.text.replace('免税数额或者限量要求', '电池容量')})
        result = self.find([source, target], [source_hit(source)], '携带超过电池容量的设备时怎么办？')
        self.assertEqual([item.chunk.chunk_id for item in result], ['GENERIC_NEXT'])

    def test_adjacent_page_different_article_is_rejected(self):
        self.reject_target(section_title='第十条')

    def test_consecutive_chunk_different_article_is_rejected(self):
        self.reject_target(section_title='第十一条', page_number=3, page_index=2)

    def test_same_article_irrelevant_condition_is_rejected(self):
        self.reject_target(text=self.target.text.replace('免税数额或者限量要求', '规定数额'))

    def test_ordinary_crosspage_prose_is_rejected(self):
        self.reject_target(text='超过规定免税限量的物品，按照其他条款办理。')

    def test_source_without_enumeration_introduction_is_rejected(self):
        source = self.source.model_copy(update={'text': self.source.text.replace('有下列情形之一的', '办理手续时')})
        self.assertEqual(self.find([source, self.target], [source_hit(source)]), [])

    def test_closed_source_list_is_not_extended(self):
        source = self.source.model_copy(update={'text': self.source.text.rstrip('；') + '。'})
        self.assertEqual(self.find([source, self.target], [source_hit(source)]), [])

    def test_reset_skipped_or_broken_item_numbers_are_rejected(self):
        for text in (self.target.text.replace('（六）', '（一）'),
                     self.target.text.replace('（六）', '（七）'),
                     self.target.text.replace('（八）', '（九）')):
            with self.subTest(text=text[:20]):
                self.reject_target(text=text)

    def test_nonconsecutive_chunk_or_physical_page_is_rejected(self):
        for changes in ({'chunk_index': 15}, {'page_number': 4}, {'page_index': 3},
                        {'page_number': 2, 'page_index': 1}):
            with self.subTest(changes=changes):
                self.reject_target(**changes)

    def test_different_document_and_unknown_language_are_rejected(self):
        self.reject_target(document_id='UNRELATED')
        self.reject_target(language=None)

    def test_metadata_article_must_match_source_heading(self):
        source = self.source.model_copy(update={'section_title': '第十条'})
        target = self.target.model_copy(update={'section_title': '第十条'})
        self.assertEqual(self.find([source, target]), [])

    def test_multiple_valid_continuations_are_ambiguous(self):
        other = self.target.model_copy(update={'chunk_id': 'AMBIGUOUS'})
        self.assertEqual(self.find([*self.chunks, other]), [])

    def test_already_initial_continuation_is_not_added(self):
        self.assertEqual(self.find(initial=[*self.initial, source_hit(self.target)]), [])

    def test_new_heading_inside_continuation_is_rejected(self):
        self.reject_target(text=self.target.text + '\n第十条其他规定。')

    def test_missing_question_or_unrelated_condition_is_rejected(self):
        self.assertEqual(find_supplemental(self.initial, self.chunks), [])
        self.assertEqual(self.find(question='携带超过电池容量的设备怎么办？'), [])
        self.assertEqual(self.find(question='免税政策包括哪些物品？'), [])

    def test_negated_question_and_negated_item_are_not_equivalent(self):
        self.assertEqual(self.find(question=self.question.replace('超过免税限量', '不超过免税限量')), [])
        self.reject_target(text=self.target.text.replace('超过规定免税数额', '未超过规定免税数额'))

    def test_age_rule_unchanged_with_chinese_question(self):
        korean = fixture_chunks()
        main = next(c for c in korean if c.chunk_id == 'DOC003_C000039')
        result = self.find(korean, [source_hit(main)])
        self.assertEqual([r.chunk.chunk_id for r in result], ['DOC003_C000013'])

    def test_both_rule_types_compete_under_same_one_source_cap(self):
        korean = fixture_chunks()
        main = next(c for c in korean if c.chunk_id == 'DOC003_C000039')
        self.assertEqual(self.find([*self.chunks, *korean], [*self.initial, source_hit(main)]), [])


class CrosspageIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = chinese_fixture()
        self.question = self.fixture['question']
        self.initial = [SearchHit.model_validate(row) for row in self.fixture['initial_top5']]
        self.chunks = chain_chunks()

    async def test_async_read_dependency_scopes_only_relevant_document(self):
        load = AsyncMock(return_value=self.chunks)
        result = await complete_evidence(self.initial, question=self.question, load_chunks_fn=load)
        load.assert_awaited_once_with([self.chunks[0].document_id])
        self.assertEqual(result[0].chunk, self.chunks[1])

    async def test_irrelevant_question_does_not_read_extra_chunks(self):
        load = AsyncMock()
        self.assertEqual(await complete_evidence(self.initial, question='你好', load_chunks_fn=load), [])
        load.assert_not_called()

    async def test_read_failure_and_cancel(self):
        with self.assertLogs('app.qa.evidence_completion', level='WARNING'):
            self.assertEqual(await complete_evidence(self.initial, question=self.question,
                load_chunks_fn=AsyncMock(side_effect=OSError('unavailable'))), [])
        with self.assertRaises(asyncio.CancelledError):
            await complete_evidence(self.initial, question=self.question,
                load_chunks_fn=AsyncMock(side_effect=asyncio.CancelledError))

    async def test_enabled_preserves_top5_and_maps_original_supplemental_quote(self):
        load = AsyncMock(return_value=self.chunks)
        async def completion(hits):
            return await complete_evidence(hits, question=self.question, load_chunks_fn=load)
        docs = {hit.document_id: {'filename': hit.document_id + '.pdf', 'status': 'READY'} for hit in self.initial}
        generate = Mock(return_value=json.dumps({'answer': '测试证据要求书面申报。[E6]', 'insufficient_evidence': False}))
        original = [h.model_dump() for h in self.initial]
        with patch('app.qa.orchestrator.settings', return_value={'EVIDENCE_COMPLETION_ENABLED': 'true'}):
            result = await answer(self.question, retrieve_fn=AsyncMock(return_value=self.initial),
                get_documents_fn=AsyncMock(return_value=docs), generate_fn=generate, complete_evidence_fn=completion)
        payload = json.loads(generate.call_args.args[0][1]['content'])
        self.assertEqual([e['text'] for e in payload['evidence'][:5]], [h.text for h in self.initial])
        self.assertEqual(payload['evidence'][5]['evidence_id'], 'E6')
        self.assertEqual([h.model_dump() for h in self.initial], original)
        self.assertEqual((result.citations[0].filename, result.citations[0].page, result.citations[0].quote),
            (self.chunks[1].document_id + '.pdf', self.chunks[1].page_number, self.chunks[1].text))

    async def test_disabled_keeps_context_and_performs_no_extra_lookup(self):
        load = AsyncMock(return_value=self.chunks)
        async def completion(hits):
            return await complete_evidence(hits, question=self.question, load_chunks_fn=load)
        docs = {hit.document_id: {'filename': hit.document_id + '.pdf', 'status': 'READY'} for hit in self.initial}
        generate = Mock(return_value=json.dumps({'answer': '测试回答。[E1]', 'insufficient_evidence': False}))
        with patch('app.qa.orchestrator.settings', return_value={'EVIDENCE_COMPLETION_ENABLED': 'false'}):
            await answer(self.question, retrieve_fn=AsyncMock(return_value=self.initial),
                get_documents_fn=AsyncMock(return_value=docs), generate_fn=generate, complete_evidence_fn=completion)
        load.assert_not_called()
        self.assertEqual(len(json.loads(generate.call_args.args[0][1]['content'])['evidence']), 5)

    async def test_disabled_context_is_identical_to_no_completion_dependency(self):
        completion = AsyncMock()
        docs = {hit.document_id: {'filename': hit.document_id + '.pdf', 'status': 'READY'} for hit in self.initial}
        generator = Mock(return_value=json.dumps({'answer': '测试回答。[E1]', 'insufficient_evidence': False}))
        dependencies = dict(retrieve_fn=AsyncMock(return_value=self.initial),
            get_documents_fn=AsyncMock(return_value=docs), generate_fn=generator)
        with patch('app.qa.orchestrator.settings', return_value={'EVIDENCE_COMPLETION_ENABLED': 'false'}):
            first = await answer(self.question, **dependencies)
            old_context = generator.call_args
            second = await answer(self.question, complete_evidence_fn=completion, **dependencies)
        self.assertEqual(first, second)
        self.assertEqual(old_context, generator.call_args)
        completion.assert_not_called()
