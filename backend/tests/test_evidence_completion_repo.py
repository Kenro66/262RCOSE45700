"""Narrow original-C2 lookup, isolated SQLite only through the shared fixture."""
from unittest.mock import AsyncMock

import pytest

from app.storage.models import Chunk, Document
from app.storage.repo import get_ready_chunk_sources


@pytest.mark.asyncio
async def test_only_ready_sources_return_original_c2_without_embedding(db):
    async with db() as session:
        for index, status in enumerate(('READY', 'PROCESSING', 'FAILED')):
            document_id = f'TEST{index}'
            session.add(Document(document_id=document_id, filename=f'{document_id}.pdf', status=status))
            session.add(Chunk(schema_version='1.0', chunk_id=f'{document_id}_C1', document_id=document_id,
                chunk_index=1, page_index=1, page_number=2, text='원문 그대로', section_title='제1조',
                language='ko', token_count=3, char_count=6, content_hash='sha256:test', embedding=[.1] * 1024))
        await session.commit()
    rows = await get_ready_chunk_sources(['TEST0', 'TEST1', 'TEST2', 'MISSING'], session_factory=db)
    assert len(rows) == 1
    assert (rows[0].document_id, rows[0].page_number, rows[0].text) == ('TEST0', 2, '원문 그대로')
    assert not hasattr(rows[0], 'embedding')
    assert not hasattr(rows[0], 'score')


@pytest.mark.asyncio
async def test_empty_ids_do_not_open_session():
    factory = AsyncMock()
    assert await get_ready_chunk_sources([], session_factory=factory) == []
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_flag_off_never_reads_completion_sources(monkeypatch):
    from app.chat.runtime import answer_question
    from app.contracts import SearchHit
    import app.storage.repo as repo
    hit = SearchHit(chunk_id='TEST_C0', document_id='TEST', page_number=1,
        text='만 19세 미만인 자가 반입하는 술 및 담배에 대해서는 관세를 면제하지 않는다.', score=.5)
    monkeypatch.setattr('app.qa.retriever.retrieve', AsyncMock(return_value=[hit]))
    monkeypatch.setattr(repo, 'get_documents', AsyncMock(return_value={
        'TEST': {'filename': 'test.pdf', 'status': 'READY'}}))
    load = AsyncMock()
    monkeypatch.setattr(repo, 'get_ready_chunk_sources', load)
    monkeypatch.setattr('app.qa.orchestrator.settings', lambda: {'EVIDENCE_COMPLETION_ENABLED': 'false'})
    monkeypatch.setattr('app.qa.orchestrator.generate_text', lambda messages:
        '{"answer":"근거에 따른 답변입니다.[E1]","insufficient_evidence":false}')
    result = await answer_question('담배 면세가 적용되나요?')
    assert result.status.value == 'ANSWERED'
    load.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_passes_original_question_to_crosspage_completion(monkeypatch):
    from app.chat.runtime import answer_question
    from app.contracts import SearchHit
    from tests.test_evidence_completion_crosspage import chain_chunks, chinese_fixture
    import app.storage.repo as repo

    fixture = chinese_fixture()
    initial = [SearchHit.model_validate(row) for row in fixture['initial_top5']]
    chunks = chain_chunks()
    monkeypatch.setattr('app.qa.retriever.retrieve', AsyncMock(return_value=initial))
    monkeypatch.setattr(repo, 'get_documents', AsyncMock(return_value={hit.document_id:
        {'filename': hit.document_id + '.pdf', 'status': 'READY'} for hit in initial}))
    load = AsyncMock(return_value=chunks)
    monkeypatch.setattr(repo, 'get_ready_chunk_sources', load)
    monkeypatch.setattr('app.qa.orchestrator.settings', lambda: {'EVIDENCE_COMPLETION_ENABLED': 'true'})
    monkeypatch.setattr('app.qa.orchestrator.generate_text', lambda messages:
        '{"answer":"测试原文引用。[E6]","insufficient_evidence":false}')
    result = await answer_question(fixture['question'])
    load.assert_awaited_once_with([chunks[0].document_id])
    assert result.citations[0].page == chunks[1].page_number
    assert result.citations[0].quote == chunks[1].text


@pytest.mark.asyncio
async def test_ready_chinese_chunks_work_with_read_only_dependency(db):
    from tests.test_evidence_completion_crosspage import chain_chunks, chinese_fixture
    from app.contracts import SearchHit
    from app.qa.evidence_completion import complete_evidence

    chunks = chain_chunks()
    async with db() as session:
        session.add(Document(document_id=chunks[0].document_id, filename='policy.pdf', status='READY'))
        session.add_all([Chunk(**chunk.model_dump(), embedding=None) for chunk in chunks])
        await session.commit()
    async def read_sources(ids):
        return await get_ready_chunk_sources(ids, session_factory=db)
    fixture = chinese_fixture()
    result = await complete_evidence([SearchHit.model_validate(row) for row in fixture['initial_top5']],
        question=fixture['question'], load_chunks_fn=read_sources)
    assert len(result) == 1
    assert result[0].chunk == chunks[1]
