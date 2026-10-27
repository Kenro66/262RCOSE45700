import { useEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from './i18n/I18nContext'
import type { TranslationKey } from './i18n/zh-CN'

import {
  createMockSystemError,
  getApiErrorData,
  isSystemErrorResponse,
} from './api/client'
type Page = 'documents' | 'chat'
type Status = 'Ready' | 'Processing' | 'Failed'
type ChatMode = 'NORMAL' | 'CLARIFICATION' | 'INSUFFICIENT_EVIDENCE' | 'CONFLICTING_EVIDENCE' | 'SYSTEM_ERROR'
type ResponseStatus = 'ANSWER' | 'INSUFFICIENT_EVIDENCE' | 'NO_READY_DOCUMENTS' | 'SYSTEM_ERROR'

type ChatResponse = {
  status: ResponseStatus
  answer?: string
  citations?: number[]
  citationDetails?: C5Citation[]
  message?: string
}

type C5Citation = {
  evidence_id: string
  filename: string
  page: number
  quote: string
}

type C5ChatResponse = {
  status:
    | 'ANSWERED'
    | 'INSUFFICIENT_EVIDENCE'
    | 'NO_READY_DOCUMENTS'
    | 'SYSTEM_ERROR'
  answer: string | null
  citations: C5Citation[]
  message: string | null
}

type RecommendedScenario = {
  question: string
  status: ResponseStatus
}

 type DocumentItem = {
  id: number
  documentId?: string
  name: string
  date: string
  status: Status
  size: string
  pages: number | null
  country: '中国' | '韩国'
  errorMessage?: string
}

type Citation = {
  id: number
  document: string
  reference: string
  text: string
}

type Message = {
  id: number
  role: 'user' | 'assistant'
  body: string
  citations?: number[]
  citationDetails?: C5Citation[]
  kind?: ChatMode
  responseStatus?: ResponseStatus
  request?: string
}

type Conversation = {
  id: number
  title: string
  updatedAt: string
  messages: Message[]
  isUntitled?: boolean
}

type C1Document = {
  document_id: string
  filename: string
  status: 'READY' | 'PROCESSING' | 'FAILED'
  error_message: string | null
  page_count: number | null
  uploaded_at: string
}

function convertC1Document(
  doc: C1Document,
  id: number,
  size: string,
  country: '中国' | '韩国'
): DocumentItem {
  return {
    id,
    documentId: doc.document_id,
    name: doc.filename,
    date: doc.uploaded_at,
    status:
      doc.status === 'READY'
        ? 'Ready'
        : doc.status === 'PROCESSING'
          ? 'Processing'
          : 'Failed',
    size,
    pages: doc.page_count,
    country,
    errorMessage: doc.error_message ?? undefined
  }
}

// C1 模拟文件列表：按照 Issue #8 的后端 JSON 示例
// 包含 FAILED、PROCESSING、READY 三种状态
const mockC1Documents: C1Document[] = [
  {
    document_id: 'doc_003',
    filename: '扫描件.pdf',
    status: 'FAILED',
    error_message: '暂不支持扫描型PDF',
    page_count: null,
    uploaded_at: '2026-10-27T10:30:00+09:00',
  },
  {
    document_id: 'doc_002',
    filename: '旅客申报指南.pdf',
    status: 'PROCESSING',
    error_message: null,
    page_count: null,
    uploaded_at: '2026-10-27T10:20:00+09:00',
  },
  {
    document_id: 'doc_001',
    filename: '携带物品政策.pdf',
    status: 'READY',
    error_message: null,
    page_count: 12,
    uploaded_at: '2026-10-27T10:00:00+09:00',
  },
]

const initialDocuments: DocumentItem[] = mockC1Documents.map((doc, index) =>
  convertC1Document(
    doc,
    index + 1,
    ['2.8 MB', '4.1 MB', '1.6 MB', '4.7 MB'][index],
    index === 1 ? '中国' : '韩国'
  )
)

const citations: Citation[] = [
  {
    id: 1,
    document: '관세법 시행규칙',
    reference: '제48조 ③',
    text: '"담배 · 궐련 · 200개비" [별도면세범위 표]',
  },
]

const initialMessages: Message[] = [
  {
    id: 1,
    role: 'user',
    body: '중국에서 한국으로 입국하는 성인 여행객입니다. 일반 담배 2보루(400개비)를 가지고 있는데, 모두 면세가 되나요?',
  },
  {
    id: 2,
    role: 'assistant',
    body: '아니요. 한국 입국 시 일반 담배의 면세한도는 여행자 1명당 200개비(1보루)입니다. 400개비는 면세한도를 초과하므로 세관에 신고해야 합니다. 실제 세금은 적용 조건에 따라 확인이 필요합니다.',
    citations: [1],
    citationDetails: [
      {
        evidence_id: 'E1',
        filename: '관세법 시행규칙_여행자휴대품면세범위.pdf',
        page: 27,
        quote: '담배 · 궐련 · 200개비',
      },
    ],
    kind: 'NORMAL',
    responseStatus: 'ANSWER',
  },
]

const enableDevelopmentScenarioControls = false

// Deterministic prototype simulation; replace this function with an API call that returns ChatResponse.
// Convert the C5 API response into the format used by the Chat UI.
function convertC5ChatResponse(data: C5ChatResponse): ChatResponse {
  return {
    // 后端 ANSWERED 对应前端 ANSWER
    status: data.status === 'ANSWERED' ? 'ANSWER' : data.status,

    // 保存回答内容和后端提示信息
    answer: data.answer ?? undefined,
    message: data.message ?? undefined,

    // 将 E1、E2 等证据编号转换为前端使用的数字
    citations: data.citations
      .map((citation) => {
        const citationId = Number(
          citation.evidence_id.replace(/^E/i, '')
        )
        return citationId
      })
      .filter((id) => Number.isFinite(id)),

    // 保留 PDF 文件名、页码和证据原文
    citationDetails: data.citations,
  }
}

// Generate a mock chat response using the C5 API response format.
function createMockChatResponse(
  question: string,
  readyDocumentCount: number
): ChatResponse {
  let mockResponse: C5ChatResponse

  if (readyDocumentCount === 0) {
    mockResponse = {
      status: 'NO_READY_DOCUMENTS',
      answer: null,
      citations: [],
      message: '현재 사용 가능한 정책 문서가 없습니다.',
    }
  } else if (
    /(시스템 오류|서버 오류|에러|system error|server error|系统错误|服务器错误)/i.test(question)
  ) {
    mockResponse = {
      status: 'SYSTEM_ERROR',
      answer: null,
      citations: [],
      message: '일시적인 시스템 오류가 발생했습니다.',
    }
  } else if (
    /(담배|궐련|香烟|卷烟|cigarette|tobacco)/i.test(question)
  ) {
    let answer =
      '根据已上传的演示文件，韩国入境时普通香烟的免税限额为每名旅客 200 支。携带 400 支超过免税限额，需要向海关申报。实际税额仍需根据适用条件确认。'

    if (/[가-힣]/.test(question)) {
      answer =
        '아니요. 한국 입국 시 일반 담배의 면세한도는 여행자 1명당 200개비(1보루)입니다. 400개비는 면세한도를 초과하므로 세관에 신고해야 합니다. 실제 세금은 적용 조건에 따라 확인이 필요합니다.'
    }

    if (/^[\x00-\x7F\s?.,'!-]+$/.test(question)) {
      answer =
        'No. The duty-free allowance for regular cigarettes when entering Korea is 200 cigarettes per traveler. Carrying 400 exceeds the allowance and must be declared to customs.'
    }

    mockResponse = {
      status: 'ANSWERED',
      answer,
      citations: [
        {
          evidence_id: 'E1',
          filename: '관세법 시행규칙_여행자휴대품면세범위.pdf',
          page: 27,
          quote: '담배 · 궐련 · 200개비',
        },
      ],
      message: null,
    }
  } else {
    mockResponse = {
      status: 'INSUFFICIENT_EVIDENCE',
      answer: null,
      citations: [],
      message: '현재 정책 문서에서 충분한 근거를 찾지 못했습니다.',
    }
  }

  return convertC5ChatResponse(mockResponse)
}

const initialConversations: Conversation[] = [
  { id: 101, title: '한국 입국 시 담배 면세한도', updatedAt: '2026年10月16日', messages: initialMessages },
  {
    id: 102,
    title: '중국 입국 시 주류 면세한도',
    updatedAt: '2026年10月15日',
    messages: [
      { id: 21, role: 'user', body: '한국에서 중국으로 입국할 때 술은 몇 병까지 면세로 반입할 수 있나요?' },
      { id: 22, role: 'assistant', body: '', responseStatus: 'INSUFFICIENT_EVIDENCE' },
    ],
  },
  {
    id: 103,
    title: '상비약 반입 신고 여부',
    updatedAt: '2026年10月14日',
    messages: [
      { id: 31, role: 'user', body: '감기약 같은 상비약을 가지고 입국하면 신고해야 하나요?' },
      { id: 32, role: 'assistant', body: '', responseStatus: 'SYSTEM_ERROR', request: '감기약 같은 상비약을 가지고 입국하면 신고해야 하나요?' },
    ],
  },
  {
    id: 104,
    title: '육류 가공품 반입 가능 여부',
    updatedAt: '2026年10月13日',
    messages: [
      { id: 41, role: 'user', body: '육포 같은 육류 가공품을 한국에 가져올 수 있나요?' },
      { id: 42, role: 'assistant', body: '', responseStatus: 'NO_READY_DOCUMENTS' },
    ],
  },
]

function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, React.ReactNode> = {
    files: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/><path d="M9 13h6M9 17h6"/></>,
    chat: <><path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z"/><path d="M8 9h8M8 13h5"/></>,
    upload: <><path d="M12 16V3M7 8l5-5 5 5"/><path d="M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6"/></>,
    info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></>,
    help: <><circle cx="12" cy="12" r="9"/><path d="M9.6 9a2.6 2.6 0 1 1 3.5 2.44c-.8.32-1.1.83-1.1 1.56M12 17h.01"/></>,
    trash: <><path d="M4 7h16M9 7V4h6v3M7 7l1 14h8l1-14M10 11v6M14 11v6"/></>,
    close: <><path d="m6 6 12 12M18 6 6 18"/></>,
    search: <><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></>,
    chevron: <path d="m9 18 6-6-6-6"/>,
    file: <><path d="M14 2H6a2 2 0 0 0-2 2v16h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/></>,
    send: <><path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/></>,
    spark: <><path d="m12 3 1.4 4.1L17.5 8.5l-4.1 1.4L12 14l-1.4-4.1-4.1-1.4 4.1-1.4Z"/><path d="m19 15 .8 2.2 2.2.8-2.2.8L19 21l-.8-2.2L16 18l2.2-.8Z"/></>,
    retry: <><path d="M20 7v5h-5"/><path d="M19 12a7 7 0 1 0-2 5"/></>,
    check: <path d="m5 12 4 4L19 6"/>,
    warning: <><path d="M12 3 2.5 20h19Z"/><path d="M12 9v4M12 17h.01"/></>,
    globe: <><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/></>,
    more: <><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></>,
    panel: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/></>,
  }
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>
}

function Button({ children, variant = 'default', className = '', ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'default' | 'primary' | 'danger' | 'ghost' | 'icon' }) {
  return <button className={`button button-${variant} ${className}`} {...props}>{children}</button>
}

function StatusBadge({ status }: { status: Status }) {
  const { t } = useI18n()
  const labels: Record<Status, TranslationKey> = {
    Ready: 'status.ready',
    Processing: 'status.processing',
    Failed: 'status.failed',
  }
  return <span className={`status status-${status.toLowerCase()}`}><span className="status-dot"/>{t(labels[status])}</span>
}

function Disclaimer({ showDemoTag = true }: { showDemoTag?: boolean }) {
  const { t } = useI18n()
  return <div className="disclaimer"><Icon name="warning" size={16}/><span><strong>{t('disclaimer.title')}</strong>　 {t('disclaimer.text')}</span>{showDemoTag && <span className="demo-tag">{t('disclaimer.demo')}</span>}</div>
}

function LanguageSwitcher() {
  const { locale, setLocale, t } = useI18n()
  return (
    <div className="language-switcher" aria-label={t('language.label')}>
      <Icon name="globe" size={14}/>
      <Button variant="ghost" className={locale === 'zh-CN' ? 'active' : ''} onClick={() => setLocale('zh-CN')}>中文</Button>
      <span/>
      <Button variant="ghost" className={locale === 'ko-KR' ? 'active' : ''} onClick={() => setLocale('ko-KR')}>한국어</Button>
    </div>
  )
}

function localizeCountry(country: string, t: (key: TranslationKey, variables?: Record<string, string | number>) => string) {
  return country === '中国' ? t('country.china') : country === '韩国' ? t('country.korea') : country
}

function Sidebar({
  page,
  onNavigate,
  documents,
  conversations,
  selectedConversationId,
  onSelectConversation,
  onNewConversation,
  onDeleteConversation,
}: {
  page: Page
  onNavigate: (page: Page) => void
  documents: DocumentItem[]
  conversations: Conversation[]
  selectedConversationId: number | null
  onSelectConversation: (id: number) => void
  onNewConversation: () => void
  onDeleteConversation: (id: number) => void
}) {
  const { t } = useI18n()
  const [deletingConversation, setDeletingConversation] = useState<Conversation | null>(null)
  const counts = {
    Ready: documents.filter((d) => d.status === 'Ready').length,
    Processing: documents.filter((d) => d.status === 'Processing').length,
    Failed: documents.filter((d) => d.status === 'Failed').length,
  }
  return (
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><span>P</span></span><span>Policy AI</span></div>
      <div className="nav-section">
        <div className="nav-label">{t('nav.workspace')}</div>
        <Button variant="ghost" className={`nav-item ${page === 'documents' ? 'active' : ''}`} onClick={() => onNavigate('documents')}><Icon name="files"/><span>Documents</span></Button>
        <Button variant="ghost" className={`nav-item ${page === 'chat' ? 'active' : ''}`} onClick={() => onNavigate('chat')}><Icon name="chat"/><span>Chat</span><span className="beta">AI</span></Button>
      </div>
      {page === 'chat' ? (
        <div className="conversation-history">
          <div className="history-heading"><span>{t('history.title')}</span><span>{t('history.feature')}</span></div>
          <Button variant="default" className="new-conversation" onClick={onNewConversation}><span className="plus-symbol">+</span><span>{t('history.new')}</span></Button>
          <div className="history-list">
            {conversations.map((conversation) => (
              <div
                className={`history-item ${selectedConversationId === conversation.id ? 'selected' : ''}`}
                key={conversation.id}
                role="button"
                tabIndex={0}
                onClick={() => onSelectConversation(conversation.id)}
                onKeyDown={(event) => { if (event.key === 'Enter') onSelectConversation(conversation.id) }}
              >
                <Icon name="chat" size={15}/>
                <div className="history-copy"><strong>{conversation.isUntitled ? t('history.untitled') : conversation.title}</strong></div>
                <Button variant="icon" className="history-delete" aria-label={t('history.deleteAria', { title: conversation.isUntitled ? t('history.untitled') : conversation.title })} onClick={(event) => { event.stopPropagation(); setDeletingConversation(conversation) }}><Icon name="trash" size={14}/></Button>
              </div>
            ))}
            {conversations.length === 0 && <div className="history-empty">{t('history.empty')}</div>}
          </div>
        </div>
      ) : (
        <div className="nav-section status-nav">
          <div className="nav-label">{t('nav.documentStatus')}</div>
          {(['Ready', 'Processing', 'Failed'] as Status[]).map((status) => (
            <div className="status-filter" key={status}><span className={`filter-dot ${status.toLowerCase()}`}/><span>{t(`status.${status.toLowerCase()}` as TranslationKey)}</span><span className="count">{counts[status]}</span></div>
          ))}
        </div>
      )}
      <div className="sidebar-bottom">
        <div className="storage-card">
          <div className="storage-title"><Icon name="info" size={16}/><span>{t('prototype.title')}</span></div>
          <p>{t('prototype.body')}</p>
        </div>
        <Button variant="ghost" className="help-link"><Icon name="help"/><span>{t('prototype.help')}</span></Button>
        <div className="version">Policy AI · Prototype v1.0</div>
      </div>
      {deletingConversation && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setDeletingConversation(null)}>
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="delete-conversation-title" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-icon"><Icon name="trash" size={22}/></div>
            <h2 id="delete-conversation-title">{t('conversation.deleteTitle')}</h2>
            <p>{t('conversation.deleteBody', { title: deletingConversation.isUntitled ? t('history.untitled') : deletingConversation.title })}</p>
            <div className="modal-actions"><Button onClick={() => setDeletingConversation(null)}>{t('common.cancel')}</Button><Button variant="danger" onClick={() => { onDeleteConversation(deletingConversation.id); setDeletingConversation(null) }}>{t('common.confirmDelete')}</Button></div>
          </div>
        </div>
      )}
    </aside>
  )
}

function UploadZone({ onUpload, compact = false }: { onUpload: () => void; compact?: boolean }) {
  const { t } = useI18n()
  const [dragging, setDragging] = useState(false)
  return (
    <div
      className={`upload-zone ${compact ? 'compact' : ''} ${dragging ? 'dragging' : ''}`}
      onDragOver={(event) => { event.preventDefault(); setDragging(true) }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => { event.preventDefault(); setDragging(false); onUpload() }}
    >
      <div className="upload-icon"><Icon name="upload" size={compact ? 19 : 24}/></div>
      <div>
        <div className="upload-title">{compact ? t('upload.compact') : t('upload.title')}</div>
        {!compact && <div className="upload-hint">{t('upload.hint')}</div>}
      </div>
      <Button variant={compact ? 'ghost' : 'default'} onClick={onUpload}>{compact ? t('upload.select') : t('upload.browse')}</Button>
    </div>
  )
}

function DocumentDetails({ document, onClose }: { document: DocumentItem | null; onClose: () => void }) {
  const { t } = useI18n()
  if (!document) {
    return (
      <aside className="details-panel blank">
        <div className="details-head"><div><span className="eyebrow">{t('details.eyebrow')}</span><div className="panel-title">{t('details.title')}</div></div></div>
        <div className="blank-panel"><div className="blank-icon"><Icon name="panel" size={22}/></div><p>{t('details.selectPrompt')}</p></div>
      </aside>
    )
  }
  return (
    <aside className="details-panel">
      <div className="details-head">
        <div><span className="eyebrow">{t('details.eyebrow')}</span><div className="panel-title">{t('details.title')}</div></div>
        <Button variant="icon" aria-label={t('details.close')} onClick={onClose}><Icon name="close"/></Button>
      </div>
      <div className="pdf-preview">
        <div className="pdf-paper"><span>PDF</span><i/><i/><i/></div>
        <div className="preview-caption">{t('details.preview')}</div>
      </div>
      <div className="detail-name">{document.name}</div>
      <StatusBadge status={document.status}/>
      <div className="metadata">
        <div><span>{t('details.fileSize')}</span><strong>{document.size}</strong></div>
        <div><span>{t('details.country')}</span><strong>{localizeCountry(document.country, t)}</strong></div>
        <div><span>{t('details.pages')}</span><strong>{document.pages ? t('details.pageUnit', { count: document.pages }) : '—'}</strong></div>
      </div>
      {document.status === 'Ready' && <div className="process-summary"><div className="summary-icon"><Icon name="check" size={16}/></div><div><strong>{t('details.readyTitle')}</strong><p>{t('details.readyBody')}</p></div></div>}
      {document.status === 'Processing' && <div className="process-summary processing"><span className="spinner"/><div><strong>{t('details.processingTitle')}</strong><p>{t('details.processingBody')}</p><div className="progress"><span/></div></div></div>}
      {document.status === 'Failed' && <div className="process-summary failed"><div className="summary-icon"><Icon name="warning" size={16}/></div><div><strong>{t('details.failedTitle')}</strong><p>{document.errorMessage ?? t('details.failedBody')}</p></div></div>}
    </aside>
  )
}

function DocumentsPage({
  documents,
  setDocuments,
  selected,
  setSelected,
}: {
  documents: DocumentItem[]
  setDocuments: React.Dispatch<React.SetStateAction<DocumentItem[]>>
  selected: DocumentItem | null
  setSelected: (document: DocumentItem | null) => void
}) {
  const { t } = useI18n()
  const [emptyView, setEmptyView] = useState(false)
  const [deleting, setDeleting] = useState<DocumentItem | null>(null)
  const [search, setSearch] = useState('')
  const [uploading, setUploading] = useState(false)
  const shownDocuments = emptyView ? [] : documents.filter((document) => document.name.toLowerCase().includes(search.toLowerCase()))

  const simulateUpload = () => {
    setEmptyView(false)
    setUploading(true)
    const item: DocumentItem = { id: Date.now(), name: '여행자휴대품_통관안내_추가자료.pdf', date: '刚刚', status: 'Processing', size: '1.9 MB', pages: null, country: '中国' }
    setDocuments((current) => [item, ...current])
    setSelected(item)
    window.setTimeout(() => {
      setDocuments((current) => current.map((document) => document.id === item.id ? { ...document, status: 'Ready', pages: 18 } : document))
      setSelected({ ...item, status: 'Ready', pages: 18 })
      setUploading(false)
    }, 2200)
  }

  const confirmDelete = () => {
    if (!deleting) return
    setDocuments((current) => current.filter((document) => document.id !== deleting.id))
    if (selected?.id === deleting.id) setSelected(null)
    setDeleting(null)
  }

  return (
    <>
      <main className="main-column">
        <header className="page-header">
          <div><div className="title-row"><h1>Documents</h1><span className="doc-total">{t('documents.count', { count: documents.length })}</span></div><p>{t('documents.subtitle')}</p></div>
          <div className="header-actions"><Button variant="primary" onClick={simulateUpload}><Icon name="upload"/><span>{t('documents.uploadButton')}</span></Button><LanguageSwitcher/></div>
        </header>
        <Disclaimer showDemoTag={false}/>
        <div className="demo-switch">
          <span>{t('documents.viewState')}</span>
          <div className="segmented">
            <Button variant="ghost" className={!emptyView ? 'selected' : ''} onClick={() => setEmptyView(false)}>{t('documents.management')}</Button>
            <Button variant="ghost" className={emptyView ? 'selected' : ''} onClick={() => { setEmptyView(true); setSelected(null) }}>{t('documents.emptyView')}</Button>
          </div>
          <span className="story-ref">{emptyView ? 'S1 업로드/처리 (MVP) · S6 최신성 안내 (MVP)' : 'S1 처리 상태 (MVP) · S5 문서 목록 · 삭제 (MVP)'}</span>
        </div>
        {shownDocuments.length === 0 ? (
          <section className="empty-state">
            <div className="empty-illustration">
              <div className="empty-file back"/><div className="empty-file front"><span>PDF</span><i/><i/><i/></div>
              <div className="empty-plus">+</div>
            </div>
            <h2>{t('documents.emptyTitle')}</h2>
            <p>{t('documents.emptyBody')}</p>
            <UploadZone onUpload={simulateUpload}/>
            <div className="empty-footnote"><Icon name="info" size={15}/>{t('documents.emptyFootnote')}</div>
          </section>
        ) : (
          <section className="document-workspace">
            <UploadZone compact onUpload={simulateUpload}/>
            {uploading && <div className="upload-progress"><span className="spinner"/><div><strong>{t('documents.uploading')}</strong><small>{t('documents.indexing')}</small></div><div className="progress"><span/></div></div>}
            <div className="list-toolbar">
              <div className="search-group"><div className="search-box"><Icon name="search" size={17}/><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t('documents.search')}/></div><span className="later-label">Later</span></div>
              <div className="list-meta">{t('documents.recent')} <Icon name="chevron" size={14}/></div>
            </div>
            <div className="document-table">
              <div className="table-head"><span>{t('documents.columnName')}</span><span>{t('documents.columnStatus')}</span><span/></div>
              {shownDocuments.map((document) => (
                <div className={`table-row ${selected?.id === document.id ? 'selected' : ''}`} key={document.id} onClick={() => setSelected(document)}>
                  <div className="file-cell"><span className="pdf-icon"><Icon name="file" size={17}/></span><div><strong>{document.name}</strong><small>{document.size} · {localizeCountry(document.country, t)}</small></div></div>
                  <StatusBadge status={document.status}/>
                  <Button variant="icon" className="delete-button" aria-label={t('documents.deleteAria', { name: document.name })} onClick={(event) => { event.stopPropagation(); setDeleting(document) }}><Icon name="trash" size={17}/></Button>
                </div>
              ))}
            </div>
            <div className="table-footer">{t('documents.footer', { count: shownDocuments.length })}</div>
          </section>
        )}
      </main>
      <DocumentDetails document={selected} onClose={() => setSelected(null)}/>
      {deleting && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setDeleting(null)}>
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="delete-title" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-icon"><Icon name="trash" size={22}/></div>
            <h2 id="delete-title">{t('documents.deleteTitle')}</h2>
            <p>{t('documents.deleteBody', { name: deleting.name })}</p>
            <div className="modal-actions"><Button onClick={() => setDeleting(null)}>{t('common.cancel')}</Button><Button variant="danger" onClick={confirmDelete}>{t('common.confirmDelete')}</Button></div>
          </div>
        </div>
      )}
    </>
  )
}

function CitationMarker({ id, onSelect }: { id: number; onSelect: (id: number) => void }) {
  return <Button variant="ghost" className="citation-marker" onClick={() => onSelect(id)}>[{id}]</Button>
}

function CitationPanel({
  selectedId,
  onSelect,
  responseStatus,
  onOpenDocuments,
  items = citations,
}: {
  selectedId: number
  onSelect: (id: number) => void
  responseStatus?: ResponseStatus
  onOpenDocuments: () => void
  items?: Citation[]
}) {
  const { t } = useI18n()

  // Find the selected citation from the provided data.
  const citation =
    items.find((item) => item.id === selectedId) ?? items[0]

  // Display an empty state for non-answered responses.
  if (responseStatus && responseStatus !== 'ANSWER') {
    const emptyMessages: Record<
      Exclude<ResponseStatus, 'ANSWER'>,
      string
    > = {
      INSUFFICIENT_EVIDENCE: t('citations.empty.insufficient'),
      NO_READY_DOCUMENTS: t('citations.empty.noReady'),
      SYSTEM_ERROR: t('citations.empty.system'),
    }

    return (
      <aside className="details-panel citation-panel">
        <div className="details-head">
          <div>
            <span className="eyebrow">
              {t('citations.eyebrow')}
            </span>
            <div className="panel-title">
              {t('citations.title')}
            </div>
          </div>
          <span className="source-count">
            {t('citations.count', { count: 0 })}
          </span>
        </div>

        <div className="blank-panel citation-empty">
          <div className="blank-icon">
            <Icon
              name={responseStatus === 'SYSTEM_ERROR' ? 'warning' : 'info'}
              size={22}
            />
          </div>

          <p>{emptyMessages[responseStatus]}</p>

          {responseStatus === 'NO_READY_DOCUMENTS' && (
            <Button variant="default" onClick={onOpenDocuments}>
              {t('chat.goDocuments')}
            </Button>
          )}
        </div>
      </aside>
    )
  }

  // Display available citation data.
  return (
    <aside className="details-panel citation-panel">
      <div className="details-head">
        <div>
          <span className="eyebrow">
            {t('citations.eyebrow')}
          </span>
          <div className="panel-title">
            {t('citations.title')}
          </div>
        </div>
        <span className="source-count">
          {t('citations.count', { count: items.length })}
        </span>
      </div>

      <div className="citation-tabs">
        {items.slice(0, 2).map((item) => (
          <Button
            key={item.id}
            variant="ghost"
            className={selectedId === item.id ? 'active' : ''}
            onClick={() => onSelect(item.id)}
          >
            [{item.id}]
          </Button>
        ))}
      </div>

      {citation ? (
        <div className="citation-card">
          <div className="citation-file">
            <span className="pdf-icon large">
              <Icon name="file" size={19} />
            </span>
            <div>
              <strong>{citation.document}</strong>
              <span>{t('citations.koreanLawPdf')}</span>
            </div>
          </div>

          <div className="page-number">
            <span>{t('citations.reference')}</span>
            <strong>{citation.reference}</strong>
          </div>

          <div className="evidence-label">
            <span>{t('citations.original')}</span>
            <span>{t('disclaimer.demo')}</span>
          </div>

          <blockquote>{citation.text}</blockquote>

          <div className="evidence-note">
            <Icon name="info" size={15} />
            <span>{t('citations.note')}</span>
          </div>
        </div>
      ) : (
        <div className="blank-panel citation-empty">
          <div className="blank-icon">
            <Icon name="info" size={22} />
          </div>
          <p>{t('citations.empty.insufficient')}</p>
        </div>
      )}

      <div className="citation-guide">
        <strong>{t('citations.guideTitle')}</strong>
        <p>{t('citations.guideBody')}</p>
      </div>
    </aside>
  )
}

function ChatPage({
  conversation,
  onNewConversation,
  onSetMessages,
  readyDocumentCount,
  onOpenDocuments,
}: {
  conversation: Conversation | null
  onNewConversation: () => void
  onSetMessages: (conversationId: number, update: Message[] | ((messages: Message[]) => Message[])) => void
  readyDocumentCount: number
  onOpenDocuments: () => void
}) {
  const { t } = useI18n()
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [mode, setMode] = useState<ChatMode>('NORMAL')
  const [selectedCitation, setSelectedCitation] = useState(1)
  const messagesEnd = useRef<HTMLDivElement>(null)
  const messages = conversation?.messages ?? []
  const exampleQuestions: RecommendedScenario[] = [
    { question: t('chat.example1'), status: 'ANSWER' },
    { question: t('chat.example2'), status: 'INSUFFICIENT_EVIDENCE' },
    { question: t('chat.example3'), status: 'NO_READY_DOCUMENTS' },
    { question: t('chat.example4'), status: 'SYSTEM_ERROR' },
  ]
  const latestAssistantMessage = [...messages].reverse().find((message) => message.role === 'assistant')
  const citationPanelStatus = latestAssistantMessage?.responseStatus
    ?? (latestAssistantMessage?.citations?.length ? 'ANSWER' : undefined)
    const currentCitations: Citation[] =
  latestAssistantMessage?.citationDetails?.map((item, index) => ({
    id: Number(item.evidence_id.replace(/^E/i, '')) || index + 1,
    document: item.filename,
    reference: `PDF p.${item.page}`,
    text: item.quote,
  })) ?? citations     //Convert C5 citation details into the CitationPanel display format.
  
  const exceptionContent: Record<Exclude<ChatMode, 'NORMAL'>, { title: string; body: string; action: string; prompt: string }> = {
    CLARIFICATION: {
      title: t('exception.clarification.title'),
      body: t('exception.clarification.body'),
      action: t('exception.clarification.action'),
      prompt: t('exception.clarification.prompt'),
    },
    INSUFFICIENT_EVIDENCE: {
      title: t('exception.insufficient.title'),
      body: t('exception.insufficient.body'),
      action: t('exception.insufficient.action'),
      prompt: t('exception.insufficient.prompt'),
    },
    CONFLICTING_EVIDENCE: {
      title: t('exception.conflicting.title'),
      body: t('exception.conflicting.body'),
      action: t('exception.conflicting.action'),
      prompt: t('exception.conflicting.prompt'),
    },
    SYSTEM_ERROR: {
      title: t('exception.system.title'),
      body: t('exception.system.body'),
      action: t('exception.system.action'),
      prompt: t('exception.system.prompt'),
    },
  }
  const automaticResponseContent: Record<Exclude<ResponseStatus, 'ANSWER'>, { title: string; body: string; action: string }> = {
    INSUFFICIENT_EVIDENCE: {
      title: t('exception.insufficient.title'),
      body: t('exception.insufficient.body'),
      action: t('exception.insufficient.action'),
    },
    NO_READY_DOCUMENTS: {
      title: t('response.noReady.title'),
      body: t('response.noReady.body'),
      action: t('response.noReady.action'),
    },
    SYSTEM_ERROR: {
      title: t('exception.system.title'),
      body: t('exception.system.body'),
      action: t('exception.system.action'),
    },
  }

  useEffect(() => { messagesEnd.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, loading])

  const selectMode = (nextMode: ChatMode) => {
    if (!conversation) return
    setMode(nextMode)
    if (nextMode === 'NORMAL') {
      onSetMessages(conversation.id, initialMessages)
      return
    }
    onSetMessages(conversation.id, [
      { id: Date.now(), role: 'user', body: exceptionContent[nextMode].prompt },
      { id: Date.now() + 1, role: 'assistant', body: exceptionContent[nextMode].body, kind: nextMode, citations: nextMode === 'CONFLICTING_EVIDENCE' ? [1] : undefined },
    ])
  }

  const requestMockResponse = (
  conversationId: number,
  question: string,
  predefinedResponse?: ChatResponse
) => {
  setLoading(true)

  window.setTimeout(() => {
    // 本地测试：模拟 HTTP 500 返回 SYSTEM_ERROR JSON
    let response: ChatResponse

    if (predefinedResponse?.status === 'SYSTEM_ERROR') {
      const error = createMockSystemError()
      const data = getApiErrorData(error)

      if (error.status === 500 && isSystemErrorResponse(data)) {
        response = convertC5ChatResponse({
  status: 'SYSTEM_ERROR',
  answer: null,
  citations: [],
  message: data.message,
})
      } else {
        response = predefinedResponse
      }
    } else {
      response = predefinedResponse ?? createMockChatResponse(question, readyDocumentCount)
    }

    const body = response.status === 'ANSWER'
      ? response.answer ?? ''
      : automaticResponseContent[response.status].body

    onSetMessages(conversationId, (current) => [...current, {
      id: Date.now(),
      role: 'assistant',
      body,
      citations: response.citations,
      citationDetails: response.citationDetails,
      responseStatus: response.status,
      request: question,
    }])

    setLoading(false)
  }, 1500)
}

  const submitQuestion = (question: string, predefinedResponse?: ChatResponse) => {
    const trimmed = question.trim()
    if (!trimmed || loading || !conversation) return
    const conversationId = conversation.id
    onSetMessages(conversationId, (current) => [...current, { id: Date.now(), role: 'user', body: trimmed }])
    setInput('')
    requestMockResponse(conversationId, trimmed, predefinedResponse)
  }
const sendMessage = () => submitQuestion(input)
const submitRecommendedScenario = (scenario: RecommendedScenario) => {
  
  const response: ChatResponse = scenario.status === 'ANSWER'
    ? {
        status: 'ANSWER',
        answer: t('response.tobaccoAnswer'),
        citations: [1],
        citationDetails: [
          {
            evidence_id: 'E1',
            filename: '관세법 시행규칙_여행자휴대품면세범위.pdf',
            page: 27,
            quote: '담배 · 궐련 · 200개비',
          },
        ],
      }
    : { status: scenario.status }

  submitQuestion(scenario.question, response)
}

  const retryRequest = (question?: string) => {
    if (!conversation || !question || loading) return
    requestMockResponse(conversation.id, question, { status: 'INSUFFICIENT_EVIDENCE' })
  }

  return (
    <>
      <main className="main-column chat-main">
        <header className="page-header chat-header">
          <div><div className="title-row"><h1>Chat</h1><span className="online-dot"/><span className="online-text">{t('chat.ready')}</span></div><p>{t('chat.subtitle', { count: readyDocumentCount, title: conversation ? (conversation.isUntitled ? t('history.untitled') : conversation.title) : t('chat.current') })}</p></div>
          <div className="header-actions"><Button variant="default" onClick={() => { onNewConversation(); setMode('NORMAL') }}>{t('history.new')}</Button><LanguageSwitcher/></div>
        </header>
        {enableDevelopmentScenarioControls && <div className="exception-controls">
          <div className="control-heading"><span>{t('chat.demoScenarios')}</span><span className="story-ref feature-list"><span>S2 정책 질의응답 (MVP)</span><span>S3 출처 표시 (MVP)</span><span>S4 거절 (MVP)</span><span>S7 조건 질문 (1차)</span><span>S9 충돌 안내 (1차)</span><span className="later-stage">S10 다중 턴 (Later · 2차)</span></span></div>
          <div className="mode-buttons">
            {(['NORMAL', 'CLARIFICATION', 'INSUFFICIENT_EVIDENCE', 'CONFLICTING_EVIDENCE', 'SYSTEM_ERROR'] as ChatMode[]).map((item) => (
              <Button key={item} variant="ghost" className={mode === item ? 'active' : ''} onClick={() => selectMode(item)}>{item === 'NORMAL' ? t('chat.normal') : item}</Button>
            ))}
          </div>
        </div>}
        <section className="conversation">
          <div className="conversation-inner">
            {messages.length === 0 && (
              <div className="chat-welcome">
                <div className="welcome-mark"><Icon name="spark" size={23}/></div>
                <h2>{t('chat.welcomeTitle')}</h2>
                <p>{t('chat.welcomeBody')}</p>
                {readyDocumentCount > 0 ? (
                  <div className="example-questions">
                    {exampleQuestions.map((item) => (
                      <Button variant="ghost" className="example-question" key={item.question} onClick={() => submitRecommendedScenario(item)}>
                        <span>{item.question}</span><Icon name="chevron" size={14}/>
                      </Button>
                    ))}
                  </div>
                ) : (
                  <div className="no-documents-notice">
                    <span className="notice-icon"><Icon name="files" size={18}/></span>
                    <div><strong>{t('chat.noDocumentsTitle')}</strong><p>{t('chat.noDocumentsBody')}</p></div>
                    <Button variant="default" onClick={onOpenDocuments}>{t('chat.goDocuments')}</Button>
                  </div>
                )}
              </div>
            )}
            {messages.map((message) => {
              const responseState = message.responseStatus && message.responseStatus !== 'ANSWER'
                ? automaticResponseContent[message.responseStatus]
                : null
              const developmentState = message.kind && message.kind !== 'NORMAL'
                ? exceptionContent[message.kind]
                : null
              const renderedState = responseState ?? developmentState
              const stateCode = responseState ? message.responseStatus : developmentState ? message.kind : null
              return (
                <div className={`message-row ${message.role}`} key={message.id}>
                  <div className={message.role === 'assistant' ? 'ai-avatar' : 'user-avatar'}>{message.role === 'assistant' ? <Icon name="spark" size={17}/> : t('chat.userAvatar')}</div>
                  <div className="message-wrap">
                    <div className="message-author">{message.role === 'assistant' ? 'Policy AI' : t('chat.consultant')}{message.role === 'assistant' && message.responseStatus !== 'NO_READY_DOCUMENTS' && message.responseStatus !== 'SYSTEM_ERROR' && <span>{t('chat.basedOnDocuments')}</span>}</div>
                    <div className={`message-bubble ${renderedState && stateCode ? `exception ${stateCode.toLowerCase()}` : ''}`}>
                      {renderedState && stateCode && <div className="exception-title"><span className="exception-symbol"><Icon name={stateCode === 'SYSTEM_ERROR' ? 'warning' : 'info'} size={17}/></span><strong>{renderedState.title}</strong><span className="exception-code">{stateCode}</span></div>}
                      <div className="message-text">{renderedState ? renderedState.body : message.body}</div>
                      {message.citations && <div className="message-citations">{message.citations.map((id) => <CitationMarker id={id} onSelect={setSelectedCitation} key={id}/>)}</div>}
                      {renderedState && <div className="exception-action">{renderedState.action}</div>}
                      {stateCode === 'NO_READY_DOCUMENTS' && <Button variant="default" className="retry-button" onClick={onOpenDocuments}><Icon name="files" size={16}/>{t('chat.goDocuments')}</Button>}
                      {stateCode === 'SYSTEM_ERROR' && <Button variant="default" className="retry-button" onClick={() => responseState ? retryRequest(message.request) : selectMode('NORMAL')}><Icon name="retry" size={16}/>{t('chat.retry')}</Button>}
                    </div>
                  </div>
                </div>
              )
            })}
            {loading && <div className="message-row assistant"><div className="ai-avatar"><Icon name="spark" size={17}/></div><div className="message-wrap"><div className="message-author">Policy AI <span>{t('chat.searching')}</span></div><div className="typing"><i/><i/><i/><span>{t('chat.searchingBody')}</span></div></div></div>}
            <div ref={messagesEnd}/>
          </div>
        </section>
        <div className="composer-area">
          <div className="composer-disclaimer"><Disclaimer showDemoTag={false}/></div>
          <div className="composer">
            <textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendMessage() } }} placeholder={t('chat.input')} rows={1}/>
            <div className="composer-bottom"><div><Icon name="globe" size={15}/>{t('chat.languages')}</div><Button variant="primary" className="send-button" aria-label={t('chat.sendAria')} onClick={sendMessage} disabled={!input.trim() || loading}><Icon name="send" size={17}/></Button></div>
          </div>
          <div className="ai-note">{t('chat.aiNote')}</div>
        </div>
      </main>
      <CitationPanel
  selectedId={selectedCitation}
  onSelect={setSelectedCitation}
  responseStatus={citationPanelStatus}
  onOpenDocuments={onOpenDocuments}
  items={currentCitations}
/>
</>
)
}

export default function App() {
  const [page, setPage] = useState<Page>('documents')
  const [documents, setDocuments] = useState<DocumentItem[]>(initialDocuments)
  const [selectedId, setSelectedId] = useState<number | null>(2)
  const [conversations, setConversations] = useState<Conversation[]>(initialConversations)
  const [selectedConversationId, setSelectedConversationId] = useState<number | null>(initialConversations[0].id)
  const selected = useMemo(() => documents.find((document) => document.id === selectedId) ?? null, [documents, selectedId])
  const selectedConversation = useMemo(() => conversations.find((conversation) => conversation.id === selectedConversationId) ?? null, [conversations, selectedConversationId])

  const newConversation = () => {
    const conversation: Conversation = { id: Date.now(), title: '', updatedAt: '刚刚', messages: [], isUntitled: true }
    setConversations((current) => [conversation, ...current])
    setSelectedConversationId(conversation.id)
    setPage('chat')
  }

  const setConversationMessages = (conversationId: number, update: Message[] | ((messages: Message[]) => Message[])) => {
    setConversations((current) => current.map((conversation) => {
      if (conversation.id !== conversationId) return conversation
      const messages = typeof update === 'function' ? update(conversation.messages) : update
      const firstUserMessage = messages.find((message) => message.role === 'user')?.body
      const title = firstUserMessage ? `${firstUserMessage.slice(0, 18)}${firstUserMessage.length > 18 ? '…' : ''}` : ''
      return { ...conversation, messages, title, updatedAt: '刚刚', isUntitled: !firstUserMessage }
    }))
  }

  const deleteConversation = (conversationId: number) => {
    setConversations((current) => {
      const remaining = current.filter((conversation) => conversation.id !== conversationId)
      if (selectedConversationId === conversationId) setSelectedConversationId(remaining[0]?.id ?? null)
      return remaining
    })
  }

  return (
    <div className="app-shell">
      <Sidebar
        page={page}
        onNavigate={setPage}
        documents={documents}
        conversations={conversations}
        selectedConversationId={selectedConversationId}
        onSelectConversation={setSelectedConversationId}
        onNewConversation={newConversation}
        onDeleteConversation={deleteConversation}
      />
      {page === 'documents' ? (
        <DocumentsPage documents={documents} setDocuments={setDocuments} selected={selected} setSelected={(document) => setSelectedId(document?.id ?? null)}/>
      ) : (
        <ChatPage
          conversation={selectedConversation}
          onNewConversation={newConversation}
          onSetMessages={setConversationMessages}
          readyDocumentCount={documents.filter((document) => document.status === 'Ready').length}
          onOpenDocuments={() => setPage('documents')}
        />
      )}
    </div>
  )
}
