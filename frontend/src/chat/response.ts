import { conflictGroups, displayCitations, type AgentResponse } from './contracts.ts'
export type ResponseStatus = Exclude<AgentResponse['status'], 'ANSWERED'> | 'ANSWER'
export function convertC5ChatResponse(data: AgentResponse) {
  if (data.status === 'NEEDS_CLARIFICATION') {
    const questions = data.clarification?.questions
    if (!data.question_id?.trim() || !questions?.length || new Set(questions.map(q => q.field)).size !== questions.length
      || questions.some(q => !q.field.trim() || !q.question.trim() || !['select', 'text'].includes(q.type)
        || (q.type === 'select' && (!q.options.length || q.options.some(o => !o.trim()))))) throw new Error('INVALID_CLARIFICATION')
  }
  if (data.status === 'CONFLICTING_EVIDENCE' && !conflictGroups(data)) throw new Error('INVALID_CONFLICT')
  const known = ['INSUFFICIENT_EVIDENCE', 'NO_READY_DOCUMENTS', 'SYSTEM_ERROR', 'NEEDS_CLARIFICATION', 'CONFLICTING_EVIDENCE']
  const status: ResponseStatus = data.status === 'ANSWERED' ? 'ANSWER' : known.includes(data.status) ? data.status : 'SYSTEM_ERROR'
  const citations = displayCitations(data)
  return {
    status, answer: data.answer ?? undefined, message: data.message ?? undefined,
    citations: citations.map(c => c.displayNumber),
    citationDetails: citations.map(c => ({ ...c, display_id: c.displayNumber })),
    clarification: data.clarification, question_id: data.question_id, conflict: data.conflict,
  }
}
