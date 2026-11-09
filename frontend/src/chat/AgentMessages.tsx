import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useI18n } from '../i18n/I18nContext'
import { conflictGroups, validConditions, type AgentResponse, type Clarification, type Citation } from './contracts'
import { previewMessages } from './messages'
import './agent-messages.css'

export function ClarificationForm({ clarification, resolved, busy, onSubmit }: {
  clarification: Clarification; resolved?: boolean; busy: boolean
  onSubmit: (conditions: Record<string, string>, signal: AbortSignal) => Promise<void>
}) {
  const { locale } = useI18n()
  const m = previewMessages[locale]
  const [values, setValues] = useState<Record<string, string>>({})
  const [stage, setStage] = useState<'pending' | 'sending' | 'error' | 'cancelled'>('pending')
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (controller.current || busy || resolved || !validConditions(clarification.questions, values)) return
    const active = new AbortController()
    controller.current = active; setStage('sending')
    try {
      await onSubmit({ ...values }, active.signal)
      if (!active.signal.aborted) setStage('pending')
    } catch {
      if (!active.signal.aborted) setStage('error')
    } finally { if (controller.current === active) controller.current = null }
  }
  return <section className="agent-followup" aria-label={m.need}>
    <p className="agent-hint">{m.hint}</p>
    <form onSubmit={submit}>
      <fieldset disabled={busy || resolved || stage === 'cancelled'}>
        {clarification.questions.map(question => <label className="agent-field" key={question.field}>{question.question}
          {question.type === 'select' ? <select required value={values[question.field] ?? ''} onChange={event => setValues(current => ({ ...current, [question.field]: event.target.value }))}>
            <option value="" disabled>{m.choose}</option>{question.options.map(value => <option key={value} value={value}>{value === 'CN' ? m.cn : value === 'KR' ? m.kr : value}</option>)}
          </select> : <input required maxLength={500} placeholder={m.placeholder} value={values[question.field] ?? ''} onChange={event => setValues(current => ({ ...current, [question.field]: event.target.value }))}/>}</label>)}
      </fieldset>
      <p className="agent-hint" role="status" aria-live="polite">{resolved ? `✓ ${m.submitted}` : stage === 'sending' ? m.sending : stage === 'cancelled' ? m.cancelled : ''}</p>
      {stage === 'error' && !resolved && <p role="alert" className="agent-error">{m.failed}</p>}
      {!resolved && stage !== 'cancelled' && <div className="agent-actions"><button className="button button-primary" type="submit" disabled={busy || !validConditions(clarification.questions, values)}>{stage === 'error' ? m.retry : m.submit}</button><button className="button button-ghost" type="button" onClick={() => { controller.current?.abort(); controller.current = null; setStage('cancelled') }}>{m.cancel}</button></div>}
    </form>
  </section>
}

export function ConflictEvidence({ response, onSelect }: { response: AgentResponse; onSelect: (citation: Citation) => void }) {
  const { locale } = useI18n()
  const m = previewMessages[locale]
  const groups = conflictGroups(response)
  return <section className="agent-conflict" aria-label={m.conflictTitle}>
    <p className="agent-hint">{response.conflict?.reason}</p>
    {groups ? groups.map((group, index) => <div className="agent-side" key={index}><h3>{index === 0 ? m.sideA : m.sideB}</h3>{group.map(citation => <button className="agent-evidence" key={citation.evidence_id} onClick={() => onSelect(citation)} aria-label={`${m.view}: ${citation.filename}`}><span><strong>{citation.filename}</strong><small>PDF p.{citation.page} ↗</small></span><span>{citation.quote}</span></button>)}</div>) : <p role="alert" className="agent-error">{m.invalid}</p>}
  </section>
}
