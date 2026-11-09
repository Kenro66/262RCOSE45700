// Issue #48 §3.4/3.5; PR #58 85065a5 AgentChatResponse.
// Preview-only candidate types: the production HTTP contract is not yet integrated.
export type Citation = { evidence_id: string; filename: string; page: number; quote: string }
export type Question = {
  field: string; question: string; type: 'select' | 'text'; options: string[]
}
export type Clarification = { questions: Question[] }
export type Conflict = { reason: string; side_a: string[]; side_b: string[] }
export type AgentResponse = {
  status: 'ANSWERED' | 'INSUFFICIENT_EVIDENCE' | 'NEEDS_CLARIFICATION'
    | 'CONFLICTING_EVIDENCE' | 'NO_READY_DOCUMENTS' | 'SYSTEM_ERROR'
  answer: string | null; citations: Citation[]; message: string | null
  question_id?: string; clarification?: Clarification | null; conflict?: Conflict | null
}
export type ChatRequest = { message: string; session_id?: string; question_id?: string; conditions?: Record<string, string> }
export type FollowupRequest = {
  session_id: string; question_id: string; message: string; conditions: Record<string, string>
}

export function validConditions(questions: Question[], conditions: Record<string, string>) {
  const fields = questions.map(q => q.field)
  if (!fields.length || new Set(fields).size !== fields.length
    || Object.keys(conditions).some(field => !fields.includes(field))) return false
  return questions.every(q => {
    const value = conditions[q.field]?.trim()
    return !!value && (q.type === 'text' || q.options.includes(value))
  })
}

export function conflictGroups(response: AgentResponse): [Citation[], Citation[]] | null {
  const conflict = response.conflict
  if (response.status !== 'CONFLICTING_EVIDENCE' || !conflict?.reason.trim()) return null
  const ids = response.citations.map(c => c.evidence_id)
  const all = [...conflict.side_a, ...conflict.side_b]
  if (!conflict.side_a.length || !conflict.side_b.length
    || new Set(ids).size !== ids.length || new Set(all).size !== all.length
    || all.some(id => !/^E[1-9]\d*$/.test(id) || !ids.includes(id))
    || response.citations.some(c => !c.filename.trim() || !c.quote.trim() || !Number.isInteger(c.page) || c.page < 1)) return null
  const byId = new Map(response.citations.map(c => [c.evidence_id, c]))
  return [conflict.side_a.map(id => byId.get(id)!), conflict.side_b.map(id => byId.get(id)!)]
}

export function displayCitations(response: AgentResponse) {
  const byId = new Map(response.citations.map(c => [c.evidence_id, c]))
  const order = response.status === 'CONFLICTING_EVIDENCE' && response.conflict
    ? [...response.conflict.side_a, ...response.conflict.side_b]
    : [...(response.answer ?? '').matchAll(/\[(E\d+)\]/g)].map(match => match[1])
  const ids = [...new Set([...order, ...byId.keys()])].filter(id => byId.has(id))
  return ids.map((id, index) => ({ ...byId.get(id)!, displayNumber: index + 1 }))
}
