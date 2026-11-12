import type { AgentResponse, ChatRequest } from '../chat/contracts.ts'
import { clarificationFixture, conflictFixture, createMockTransport } from './chat-fixtures.ts'

export function createChatMock(scenario = 'clarify', failOnce = false) {
  const pending = new Map<string, { response: AgentResponse; message: string }>()
  const transport = createMockTransport()
  let sequence = 0
  if (failOnce) transport.failNext()
  return async (request: ChatRequest, signal: AbortSignal, korean: boolean): Promise<AgentResponse> => {
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
    if (request.question_id === undefined) {
      if (scenario === 'conflict' || scenario === 'invalid') {
        const response = conflictFixture(korean)
        return scenario === 'invalid' ? { ...response, citations: response.citations.slice(0, 1) } : response
      }
      const response = { ...clarificationFixture(korean, scenario === 'multi'), question_id: `mock-q-${++sequence}` }
      pending.set(`${request.session_id}:${response.question_id}`, { response, message: request.message })
      return response
    }
    const entry = pending.get(`${request.session_id}:${request.question_id}`)
    if (!entry) throw new Error('CLARIFICATION_EXPIRED')
    if (entry.message !== request.message) throw new Error('INVALID_MOCK_REQUEST')
    // The lookup above verifies the real session; the fixture uses a fixed session.
    const payload = { ...request, session_id: 'mock-session-issue49' }
    return transport.submit(payload, entry.response, korean, signal)
  }
}

const params = typeof location === 'undefined' ? new URLSearchParams() : new URLSearchParams(location.search)
export const sendMockChat = createChatMock(params.get('scenario') ?? 'clarify', params.get('fail-once') === '1')
