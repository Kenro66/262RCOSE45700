import test from 'node:test'
import assert from 'node:assert/strict'
import { conflictGroups, displayCitations, validConditions } from '../src/chat/contracts.ts'
import { clarificationFixture, conflictFixture, createMockTransport, originalQuestion, session_id } from '../src/mocks/chat-fixtures.ts'
import { previewMessages } from '../src/chat/messages.ts'
import { createChatMock } from '../src/mocks/chat-transport.ts'
import { convertC5ChatResponse } from '../src/chat/response.ts'

const pending = clarificationFixture(false)
const request = () => ({ session_id: session_id, question_id: pending.question_id, question: originalQuestion(false), conditions: { destination_country: 'CN' } })

test('conditions validate all select/text fields and reject blank/unknown options', () => {
  const questions = clarificationFixture(false, true).clarification.questions
  assert.equal(validConditions(questions, { destination_country: 'CN' }), false)
  assert.equal(validConditions(questions, { destination_country: 'US', goods: '香烟' }), false)
  assert.equal(validConditions(questions, { destination_country: 'KR', goods: '  ' }), false)
  assert.equal(validConditions(questions, { destination_country: 'KR', goods: '香烟 1 盒' }), true)
  assert.equal(validConditions(questions, { destination_country: 'CN', goods: '香烟', extra: 'x' }), false)
  assert.equal(validConditions([], {}), false)
})

test('select preserves the exact option code accepted by the Agent schema', () => {
  const questions = [{ field: 'code', question: '选择代码', type: 'select', options: [' CN ', 'KR'] }]
  assert.equal(validConditions(questions, { code: ' CN ' }), true)
  assert.equal(validConditions(questions, { code: 'CN' }), false)
  assert.equal(validConditions(questions, { code: '  ' }), false)
})

test('mock uses original question and correlation IDs, returns a source after followup', async () => {
  const transport = createMockTransport(1)
  const payload = request()
  const result = await transport.submit(payload, pending, originalQuestion(false), false, new AbortController().signal)
  assert.equal(result.status, 'ANSWERED')
  assert.equal(result.question_id, payload.question_id)
  assert.deepEqual(transport.lastRequest, payload)
  payload.conditions.destination_country = 'KR'
  assert.equal(transport.lastRequest.conditions.destination_country, 'CN')
  assert.equal(displayCitations(result)[0].displayNumber, 1)
})

test('one simulated failure preserves request for successful retry', async () => {
  const transport = createMockTransport(1)
  transport.failNext()
  const payload = request()
  await assert.rejects(transport.submit(payload, pending, payload.question, false, new AbortController().signal), /MOCK_NETWORK_ERROR/)
  const result = await transport.submit(payload, pending, payload.question, false, new AbortController().signal)
  assert.equal(transport.calls, 2)
  assert.equal(result.status, 'ANSWERED')
  assert.deepEqual(transport.lastRequest, payload)
})

test('cancellation rejects pending work instead of appending a late answer', async () => {
  const transport = createMockTransport(1000)
  const controller = new AbortController()
  const result = transport.submit(request(), pending, originalQuestion(false), false, controller.signal)
  controller.abort()
  await assert.rejects(result, { name: 'AbortError' })
})

test('wrong question/session/condition never reaches fake request', async () => {
  const transport = createMockTransport(1)
  for (const change of [{ question_id: 'wrong' }, { session_id: 'wrong' }, { question: 'new question' }, { conditions: { destination_country: 'US' } }]) {
    await assert.rejects(transport.submit({ ...request(), ...change }, pending, originalQuestion(false), false, new AbortController().signal), /INVALID_MOCK_REQUEST/)
  }
  assert.equal(transport.calls, 0)
})

test('conflict uses original IDs with shuffled citations and consecutive display labels', () => {
  const response = conflictFixture(false)
  const before = structuredClone(response)
  const groups = conflictGroups(response)
  assert.equal(groups[0][0].evidence_id, 'E5')
  assert.equal(groups[1][0].evidence_id, 'E2')
  assert.deepEqual(displayCitations(response).map(c => [c.evidence_id, c.displayNumber]), [['E5', 1], ['E2', 2]])
  assert.deepEqual(response, before)
})

test('incomplete or overlapping conflict references fail closed', () => {
  for (const mutate of [
    r => r.citations.pop(), r => r.conflict.side_b = ['E99'],
    r => r.conflict.side_b = ['E5'], r => r.conflict.side_a = [],
    r => r.citations.push(r.citations[0]), r => r.citations[0].page = 0,
  ]) {
    const response = conflictFixture(false)
    mutate(response)
    assert.equal(conflictGroups(response), null)
  }
})

test('both UI languages have identical keys; Chinese evidence remains original', () => {
  assert.deepEqual(Object.keys(previewMessages['zh-CN']).sort(), Object.keys(previewMessages['ko-KR']).sort())
  assert.equal(conflictFixture(true).citations[0].quote, conflictFixture(false).citations[0].quote)
})

test('production Chat converter preserves both new statuses and resolves numbered sources', () => {
  const clarification = convertC5ChatResponse(pending)
  assert.equal(clarification.status, 'NEEDS_CLARIFICATION')
  assert.deepEqual(clarification.clarification, pending.clarification)
  const conflict = convertC5ChatResponse(conflictFixture(false))
  assert.equal(conflict.status, 'CONFLICTING_EVIDENCE')
  assert.deepEqual(conflict.citations, [1, 2])
  assert.deepEqual(conflict.citationDetails.map(c => [c.evidence_id, c.display_id]), [['E5', 1], ['E2', 2]])
})

test('invalid new responses fail instead of showing a partial conflict or unusable form', () => {
  assert.throws(() => convertC5ChatResponse({ ...conflictFixture(false), citations: [] }), /INVALID_CONFLICT/)
  assert.throws(() => convertC5ChatResponse({ ...pending, question_id: undefined }), /INVALID_CLARIFICATION/)
  assert.throws(() => convertC5ChatResponse({ ...pending, clarification: { questions: [] } }), /INVALID_CLARIFICATION/)
})

test('all legacy statuses survive conversion', () => {
  for (const status of ['ANSWERED', 'INSUFFICIENT_EVIDENCE', 'NO_READY_DOCUMENTS', 'SYSTEM_ERROR']) {
    const result = convertC5ChatResponse({ status, answer: 'answer', citations: [], message: null })
    assert.equal(result.status, status === 'ANSWERED' ? 'ANSWER' : status)
  }
})

test('mock boundary isolates chats and keeps original question on followup', async () => {
  const send = createChatMock()
  const signal = new AbortController().signal
  const first = await send({ question: '原问题', session_id: 'session-A' }, signal, false)
  const second = await send({ question: '另一个问题', session_id: 'session-B' }, signal, false)
  assert.notEqual(first.question_id, second.question_id)
  const followup = { question: '原问题', session_id: 'session-A', question_id: first.question_id, conditions: { destination_country: 'CN' } }
  await assert.rejects(send({ ...followup, session_id: 'session-B' }, signal, false), /CLARIFICATION_EXPIRED/)
  await assert.rejects(send({ ...followup, question: '改成别的问题' }, signal, false), /INVALID_MOCK_REQUEST/)
  const answer = await send(followup, signal, false)
  assert.equal(answer.status, 'ANSWERED')
  assert.equal(answer.question_id, first.question_id)
})
