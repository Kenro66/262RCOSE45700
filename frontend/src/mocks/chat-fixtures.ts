import type { AgentResponse, FollowupRequest } from '../chat/contracts.ts'
import { validConditions } from '../chat/contracts.ts'

export const session_id = 'mock-session-issue49'
export function clarificationFixture(korean: boolean, multiple = false): AgentResponse {
  return {
    status: 'NEEDS_CLARIFICATION', answer: null, citations: [], message: null,
    question_id: multiple ? 'mock-question-multi' : 'mock-question-country',
    clarification: { questions: [
      { field: 'destination_country', question: korean ? '중국과 한국 중 어느 나라로 입국하시나요?' : '您准备入境中国还是韩国？', type: 'select', options: ['CN', 'KR'] },
      ...(multiple ? [{ field: 'goods', question: korean ? '휴대할 물품과 수량을 알려 주세요.' : '请补充携带物品及数量。', type: 'text' as const, options: [] }] : []),
    ] }, conflict: null,
  }
}
export const originalQuestion = (korean: boolean) => korean ? '담배를 얼마나 가지고 입국할 수 있나요?' : '我可以携带多少香烟入境？'

export function conflictFixture(korean: boolean): AgentResponse {
  return {
    status: 'CONFLICTING_EVIDENCE', answer: null, question_id: 'mock-question-conflict',
    message: korean ? '적용 규정 사이에 상충하는 근거가 있습니다.' : '检测到适用政策存在冲突。',
    clarification: null,
    // Intentionally nonconsecutive and out of array order. API IDs are never renumbered.
    citations: [
      { evidence_id: 'E2', filename: '示例文件 B.pdf', page: 5, quote: '【模拟原文】相同适用条件下，事项 X 不允许办理。' },
      { evidence_id: 'E5', filename: '示例文件 A.pdf', page: 3, quote: '【模拟原文】相同适用条件下，事项 X 允许办理。' },
    ],
    conflict: { reason: korean ? '동일한 적용 조건에서 두 근거의 결론이 서로 모순됩니다. 각각의 원문을 확인해 주세요.' : '相同适用条件下，两项规定的结论无法同时成立，请分别核对双方依据。', side_a: ['E5'], side_b: ['E2'] },
  }
}

export function answeredFixture(korean: boolean, request: FollowupRequest): AgentResponse {
  const country = request.conditions.destination_country === 'CN' ? (korean ? '중국' : '中国') : (korean ? '한국' : '韩国')
  return {
    status: 'ANSWERED', question_id: request.question_id, message: null, clarification: null, conflict: null,
    answer: korean ? `${country} 입국 조건을 반영했습니다. 아래는 조건 보충 후 답변과 출처를 확인하는 예시입니다. 실제 휴대 한도나 정책 안내가 아닙니다.[E7]` : `已根据“入境${country}”补充条件。这里展示补充后继续回答及查看出处的交互，不代表真实携带额度或政策。[E7]`,
    citations: [{ evidence_id: 'E7', filename: '示例政策.pdf', page: 2, quote: '【模拟原文】示例事项应当按适用国家及具体携带条件核对相关规定。本段仅用于页面演示。' }],
  }
}

// A deterministic asynchronous fake. No fetch, database, model or production client.
export function createMockTransport(delay = 650) {
  let failNext = false
  let calls = 0
  let lastRequest: FollowupRequest | null = null
  return {
    failNext() { failNext = true },
    get calls() { return calls },
    get lastRequest() { return lastRequest },
    async submit(request: FollowupRequest, pending: AgentResponse, question: string, korean: boolean, signal: AbortSignal) {
      if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
      if (request.session_id !== session_id || request.question_id !== pending.question_id || request.question !== question
        || !validConditions(pending.clarification?.questions ?? [], request.conditions)) throw new Error('INVALID_MOCK_REQUEST')
      calls += 1
      lastRequest = structuredClone(request)
      const shouldFail = failNext
      failNext = false
      await new Promise<void>((resolve, reject) => {
        const onAbort = () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')) }
        const timer = setTimeout(() => { signal.removeEventListener('abort', onAbort); resolve() }, delay)
        signal.addEventListener('abort', onAbort, { once: true })
      })
      if (shouldFail) throw new Error('MOCK_NETWORK_ERROR')
      return answeredFixture(korean, request)
    },
  }
}
