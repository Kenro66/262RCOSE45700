import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App'
import { ClarificationForm } from '../src/chat/AgentMessages'
import { clarificationFixture, conflictFixture } from '../src/mocks/chat-fixtures'
import { I18nProvider } from '../src/i18n/I18nContext'
import type { ApiChatResponse, ApiDocument } from '../src/api/client'

const readyDocument: ApiDocument = {
  document_id: 'DOC_BACKEND_987', filename: '中国政策.pdf', status: 'READY',
  error_message: null, page_count: 4, uploaded_at: '2026-11-13T00:00:00Z',
}
const answered: ApiChatResponse = {
  status: 'ANSWERED', answer: '应当按照证据办理申报。[E1]',
  citations: [{ evidence_id: 'E1', filename: '中国政策.pdf', page: 3, quote: '应当书面申报。' }],
  message: null,
}

function mockApi(documents: ApiDocument[] = [readyDocument]) {
  const fetcher = vi.fn(async (url: RequestInfo | URL, options?: RequestInit) => {
    if (url === '/api/documents' && options?.method === 'GET') return Response.json(documents)
    throw new Error('测试未定义的请求：' + String(url))
  })
  vi.stubGlobal('fetch', fetcher)
  return fetcher
}

function renderApp() {
  return render(<I18nProvider><App /></I18nProvider>)
}

async function openChat() {
  const user = userEvent.setup()
  renderApp()
  await screen.findByText('中国政策.pdf')
  await user.click(screen.getByRole('button', { name: /^Chat/ }))
  return user
}

async function ask(user: ReturnType<typeof userEvent.setup>, question = '入境需要申报吗？') {
  await user.type(screen.getByRole('textbox'), question)
  await user.click(screen.getByRole('button', { name: '发送问题' }))
}

beforeEach(() => {
  mockApi()
})

it('进入 Chat 时只滚动消息区', async () => {
  const scrollHeight = vi.spyOn(Element.prototype, 'scrollHeight', 'get').mockReturnValue(240)
  const scrollIntoView = vi.mocked(Element.prototype.scrollIntoView).mockClear()
  try {
    await openChat()
    const conversation = document.querySelector('.conversation')
    expect(conversation?.scrollTop).toBe(240)
    expect(scrollIntoView).not.toHaveBeenCalled()
  } finally {
    scrollHeight.mockRestore()
  }
})

it.each([404, 500])('keeps the chat layout when the session endpoint returns %i', async (status) => {
  const fetcher = vi.fn(async (url: RequestInfo | URL) => {
    if (url === '/api/documents') return Response.json([readyDocument])
    if (url === '/api/chat/sessions') return Response.json({ detail: 'Session request failed' }, { status })
    throw new Error('Unexpected request: ' + String(url))
  })
  vi.stubGlobal('fetch', fetcher)

  await openChat()
  expect(fetcher).toHaveBeenCalledWith('/api/chat/sessions', expect.any(Object))

  if (status === 404) {
    expect(screen.queryByRole('alert')).toBeNull()
  } else {
    const alert = await screen.findByRole('alert')
    expect(alert.closest('.conversation-history')).toBeVisible()
  }
})


describe('文件页面需求', () => {
  it('DOC-01：展示后端的可用、处理中、失败状态及失败原因', async () => {
    mockApi([
      readyDocument,
      { ...readyDocument, document_id: 'DOC_PENDING', filename: '处理中.pdf', status: 'PROCESSING', page_count: null },
      { ...readyDocument, document_id: 'DOC_FAILED', filename: '扫描件.pdf', status: 'FAILED', error_message: '没有可提取的文字' },
    ])
    const user = userEvent.setup()
    renderApp()
    await screen.findByText('处理中.pdf')
    const failed = await screen.findByText('扫描件.pdf')
    expect(screen.getAllByText('可用').length).toBeGreaterThan(0)
    expect(screen.getAllByText('处理中').length).toBeGreaterThan(0)
    await user.click(failed)
    expect(await screen.findByText('没有可提取的文字')).toBeVisible()
  })

  it('DOC-02：非 PDF 文件被拒绝，不发送上传请求', async () => {
    const fetcher = mockApi([])
    const alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
    const user = userEvent.setup({ applyAccept: false })
    const { container } = renderApp()
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!
    await user.upload(input, new File(['test'], 'notes.docx'))
    expect(alert).toHaveBeenCalledWith('仅支持上传 PDF 文件。')
    expect(fetcher.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0)
  })

  it('DOC-03：上传成功展示后端返回的 PROCESSING 记录', async () => {
    const fetcher = mockApi([])
    fetcher.mockImplementation(async (_url, options) => Response.json(
      options?.method === 'POST'
        ? { ...readyDocument, filename: '新政策.pdf', status: 'PROCESSING', page_count: null }
        : [],
      { status: options?.method === 'POST' ? 201 : 200 },
    ))
    const user = userEvent.setup()
    const { container } = renderApp()
    await user.upload(container.querySelector<HTMLInputElement>('input[type="file"]')!,
      new File(['%PDF-1.7'], '新政策.pdf', { type: 'application/pdf' }))
    expect((await screen.findAllByText('新政策.pdf')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('处理中').length).toBeGreaterThan(0)
  })

  it('DOC-04：上传失败显示后端原因，文件不会作为成功记录出现', async () => {
    const fetcher = mockApi([])
    fetcher.mockImplementation(async (_url, options) => options?.method === 'POST'
      ? Response.json({ detail: 'PDF exceeds the 25 MB limit' }, { status: 400 })
      : Response.json([]))
    const alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const user = userEvent.setup()
    const { container } = renderApp()
    await user.upload(container.querySelector<HTMLInputElement>('input[type="file"]')!,
      new File(['%PDF-1.7'], '失败政策.pdf', { type: 'application/pdf' }))
    await waitFor(() => expect(alert).toHaveBeenCalledWith('PDF 文件不能超过 25 MB。'))
    expect(screen.queryByText('失败政策.pdf')).not.toBeInTheDocument()
  })

  it('DOC-05：确认删除后使用后端 ID，删除成功才移除文件', async () => {
    const fetcher = mockApi()
    fetcher.mockImplementation(async (_url, options) => options?.method === 'DELETE'
      ? new Response(null, { status: 204 })
      : Response.json([readyDocument]))
    const user = userEvent.setup()
    renderApp()
    await screen.findByText('中国政策.pdf')
    await user.click(screen.getByRole('button', { name: '删除 中国政策.pdf' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: '确认删除' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: '删除 中国政策.pdf' })).not.toBeInTheDocument())
    expect(fetcher).toHaveBeenCalledWith('/api/documents/DOC_BACKEND_987', { method: 'DELETE' })
  })

  it('DOC-06：列表连接失败显示错误提示，不伪装成正常空库', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Network error')))
    vi.spyOn(console, 'error').mockImplementation(() => {})
    renderApp()
    expect(await screen.findByRole('alert')).toBeVisible()
  })

  it('DOC-07：后续轮询会把 PROCESSING 更新为 READY', async () => {
    const fetcher = mockApi()
    let reads = 0
    fetcher.mockImplementation(async () => {
      reads++
      return Response.json([{ ...readyDocument, status: reads === 1 ? 'PROCESSING' : 'READY' }])
    })
    renderApp()
    await screen.findByText('中国政策.pdf')
    const row = screen.getByText('中国政策.pdf').closest('.table-row')!
    expect(within(row as HTMLElement).getByText('处理中')).toBeVisible()
    await waitFor(() => expect(within(row as HTMLElement).getByText('可用')).toBeVisible(), { timeout: 4500 })
  })
})

describe('问答页面需求', () => {
  it('CHAT-01：ANSWERED 展示回答、真实文件名、物理页码和证据原文', async () => {
    const fetcher = mockApi()
    fetcher.mockImplementation(async (url) => Response.json(url === '/api/chat'
      ? { ...answered, model_name: 'single-provider-model' } : [readyDocument]))
    const user = await openChat()
    await ask(user)
    expect(await screen.findByText('应当按照证据办理申报。')).toBeVisible()
    const modelLabel = screen.getByText('模型：single-provider-model')
    expect(modelLabel.closest('.message-author')).toHaveTextContent('Policy AI')
    expect(screen.getByText('中国政策.pdf')).toBeVisible()
    expect(screen.getByText('PDF p.3')).toBeVisible()
    expect(screen.getByText('应当书面申报。')).toBeVisible()
    expect(screen.getAllByRole('button', { name: '[1]' })[0]).toBeVisible()
  })

  it.each([
    ['INSUFFICIENT_EVIDENCE', '现有资料不足以回答。'],
    ['NO_READY_DOCUMENTS', '当前没有可用文件'],
  ] as const)('CHAT-02：%s 显示拒答或提示，不展示虚构引用', async (status, text) => {
    const fetcher = mockApi()
    fetcher.mockImplementation(async (url) => Response.json(url === '/api/chat'
      ? { status, answer: status === 'INSUFFICIENT_EVIDENCE' ? text : null, citations: [], message: text, model_name: null }
      : [readyDocument]))
    const user = await openChat()
    await ask(user)
    expect(await screen.findByText(text)).toBeVisible()
    expect(screen.queryByText(/^模型：/)).not.toBeInTheDocument()
    expect(screen.queryByText('PDF p.3')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '[1]' })).not.toBeInTheDocument()
    if (status === 'NO_READY_DOCUMENTS') {
      expect(screen.getAllByRole('button', { name: '前往 Documents' }).length).toBeGreaterThan(0)
    }
  })

  it('CHAT-03：HTTP 500 的 SYSTEM_ERROR 显示后端提示，并允许重试原问题', async () => {
    const fetcher = mockApi()
    let calls = 0
    fetcher.mockImplementation(async (url) => {
      if (url !== '/api/chat') return Response.json([readyDocument])
      calls++
      return calls === 1
        ? Response.json({ status: 'SYSTEM_ERROR', answer: null, citations: [], message: '生成服务暂时不可用' }, { status: 500 })
        : Response.json(answered)
    })
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const user = await openChat()
    await ask(user)
    expect(await screen.findByText('生成服务暂时不可用')).toBeVisible()
    const retry = screen.getByRole('button', { name: '重试' })
    act(() => {
      retry.click()
      retry.click()
    })
    expect(await screen.findByText('应当按照证据办理申报。')).toBeVisible()
    const requests = fetcher.mock.calls.filter(([url]) => url === '/api/chat')
    expect(requests).toHaveLength(2)
    const [firstRequest, retryRequest] = requests.map(([, options]) => JSON.parse(options!.body as string))
    expect(firstRequest).toEqual({
      message: '入境需要申报吗？',
      session_id: expect.stringMatching(/.+/),
    })
    expect(retryRequest).toEqual(firstRequest)
  })

  it('CHAT-04：未知状态显示系统错误，不能当作成功回答', async () => {
    const fetcher = mockApi()
    fetcher.mockImplementation(async (url) => Response.json(url === '/api/chat'
      ? { status: 'UNKNOWN', answer: null, citations: [], message: '无法识别的响应' }
      : [readyDocument]))
    const user = await openChat()
    await ask(user)
    expect(await screen.findByText('SYSTEM_ERROR')).toBeVisible()
    expect(screen.queryByRole('button', { name: '[1]' })).not.toBeInTheDocument()
  })

  it('CHAT-05：空白输入不能发送；请求处理中不能重复发送', async () => {
    const fetcher = mockApi()
    fetcher.mockImplementation(async (url) => url === '/api/chat'
      ? new Promise<Response>(() => {})
      : Response.json([readyDocument]))
    const user = await openChat()
    const send = screen.getByRole('button', { name: '发送问题' })
    expect(send).toBeDisabled()
    await user.type(screen.getByRole('textbox'), '   ')
    expect(send).toBeDisabled()
    await user.clear(screen.getByRole('textbox'))
    await ask(user)
    expect(send).toBeDisabled()
    await user.click(send)
    expect(fetcher.mock.calls.filter(([url]) => url === '/api/chat')).toHaveLength(1)
  })

  it('同一回答按文件合并连续页码，并保留每条引用原文', async () => {
    const fetcher = mockApi()
    fetcher.mockImplementation(async (url) => url === '/api/chat' ? Response.json({
      ...answered,
      citations: [
        { evidence_id: 'E1', filename: '中国政策.pdf', page: 13, quote: '第十三页原文。' },
        { evidence_id: 'E2', filename: '中国政策.pdf', page: 12, quote: '第十二页原文。' },
        { evidence_id: 'E3', filename: '中国政策.pdf', page: 15, quote: '第十五页原文。' },
        { evidence_id: 'E4', filename: '另一份政策.pdf', page: 14, quote: '另一份文件的原文。' },
      ],
    }) : Response.json([readyDocument]))
    const user = await openChat()
    await ask(user)
    expect(await screen.findByText('PDF p.12–13')).toBeVisible()
    const selectCitation = async (id: number) => {
      const tabs = screen.getByText('PDF p.', { exact: false }).closest('.citation-panel')!
      await user.click(within(tabs as HTMLElement).getByRole('button', { name: `[${id}]` }))
    }
    await selectCitation(2)
    expect(screen.getByText('PDF p.12–13')).toBeVisible()
    expect(screen.getByText('第十二页原文。')).toBeVisible()
    await selectCitation(3)
    expect(screen.getByText('PDF p.15')).toBeVisible()
    await selectCitation(4)
    expect(screen.getByText('PDF p.14')).toBeVisible()
  })

  it('CHAT-06：连续回答后点击旧回答引用，展示旧回答自己的来源', async () => {
    const fetcher = mockApi()
    let calls = 0
    fetcher.mockImplementation(async (url) => {
      if (url !== '/api/chat') return Response.json([readyDocument])
      calls++
      return Response.json(calls === 1 ? answered : {
        status: 'ANSWERED', answer: '第二份政策的回答。[E1]',
        citations: [{ evidence_id: 'E1', filename: '韩国政策.pdf', page: 9, quote: '第二份政策的原文。' }],
        message: null,
      })
    })
    const user = await openChat()
    await ask(user)
    await screen.findByText('应当按照证据办理申报。')
    await ask(user, '另一个问题')
    await screen.findByText('PDF p.9')
    const markers = screen.getAllByRole('button', { name: '[1]' })
    await user.click(markers[0])
    expect(await screen.findByText('PDF p.3')).toBeVisible()
    expect(screen.getByText('应当书面申报。')).toBeVisible()
    expect(screen.queryByText('PDF p.9')).not.toBeInTheDocument()
  })
})

// 文件服务契约：删除失败必须保留可见记录，用户仍可重试。
describe('删除失败保护', () => {
  it('DOC-08：服务返回 503 时不移除文件，并展示错误', async () => {
    const fetcher = mockApi()
    fetcher.mockImplementation(async (_url, options) => options?.method === 'DELETE'
      ? Response.json({ detail: '原文件暂时无法删除，请稍后重试' }, { status: 503 })
      : Response.json([readyDocument]))
    const alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const user = userEvent.setup()
    renderApp()
    await screen.findByText('中国政策.pdf')
    await user.click(screen.getByRole('button', { name: '删除 中国政策.pdf' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: '确认删除' }))
    await waitFor(() => expect(alert).toHaveBeenCalled())
    expect(screen.getByRole('button', { name: '删除 中国政策.pdf' })).toBeVisible()
    expect(screen.getByRole('dialog')).toBeVisible()
  })
})


describe('持久化聊天', () => {
  it('从 HTTP 响应恢复追问，切页后保留草稿，并用原会话和问题 ID 提交', async () => {
    const session = { session_id: 'saved-session', title: '已有会话' }
    const followup = clarificationFixture(false, true)
    const question = '携带物品入境有什么规定？'
    const execution = '/api/chat/sessions/saved-session/questions/' + followup.question_id + '/execution'
    let completed = false
    let attempts = 0
    const mock = vi.fn(async (url: RequestInfo | URL, options?: RequestInit) => {
      if (url === '/api/documents') return Response.json([readyDocument])
      if (url === '/api/chat/sessions') return Response.json({ sessions: [session] })
      if (url === '/api/chat/sessions/saved-session') return Response.json({
        ...session, questions: [{ question_id: followup.question_id, message: question, clarification: followup.clarification }],
      })
      if (url === execution) return Response.json({
        execution_status: completed ? 'SUCCEEDED' : 'NEEDS_CLARIFICATION',
        retryable: false, response: completed ? answered : followup,
      })
      if (url === '/api/chat' && options?.method === 'POST') {
        attempts += 1
        if (attempts === 1) return Response.json({ status: 'SYSTEM_ERROR', answer: null, citations: [], message: '暂时失败' })
        completed = true
        return Response.json(answered)
      }
      throw new Error('测试未定义的请求：' + String(url))
    })
    vi.stubGlobal('fetch', mock)
    const user = await openChat()
    const country = () => screen.findByRole('combobox', { name: /您准备入境/ })
    await screen.findByText(question, { selector: '.message-text' })
    await user.selectOptions(await country(), 'KR')
    await user.type(screen.getByRole('textbox', { name: /请补充携带物品/ }), '香烟 1 盒')
    await user.click(screen.getByRole('button', { name: /^Documents/ }))
    await user.click(screen.getByRole('button', { name: /^Chat/ }))
    await waitFor(() => expect(screen.getByRole('combobox', { name: /您准备入境/ })).toBeEnabled())
    expect(await country()).toHaveValue('KR')
    expect(screen.getByRole('textbox', { name: /请补充携带物品/ })).toHaveValue('香烟 1 盒')
    const form = (await country()).closest('form')!
    await user.click(within(form).getByRole('button', { name: '补充并继续' }))
    await screen.findByText('提交失败，已保留您的选择。请重试。')
    expect(await country()).toHaveValue('KR')
    await user.click(within(form).getByRole('button', { name: '重试' }))
    await screen.findByText('应当按照证据办理申报。', { selector: '.message-text' })
    const requests = mock.mock.calls.filter(([url]) => url === '/api/chat')
      .map(([, options]) => JSON.parse(options?.body as string))
    expect(requests).toHaveLength(2)
    expect(requests[0]).toEqual(requests[1])
    expect(requests[0]).toEqual({
      session_id: 'saved-session', question_id: followup.question_id, message: question,
      conditions: { destination_country: 'KR', goods: '香烟 1 盒' },
    })
  })

  it('同步删除失败时保留会话，重试成功后刷新也不再列出', async () => {
    const session = { session_id: 'delete-session', title: '待删除会话' }
    let attempts = 0
    const mock = vi.fn(async (url: RequestInfo | URL, options?: RequestInit) => {
      if (url === '/api/documents') return Response.json([readyDocument])
      if (url === '/api/chat/sessions') return Response.json({ sessions: attempts < 2 ? [session] : [] })
      if (url === '/api/chat/sessions/delete-session' && options?.method !== 'DELETE') return Response.json({ ...session, questions: [{ question_id: 'delete-question', message: session.title }] })
      if (url === '/api/chat/sessions/delete-session/questions/delete-question/execution') return Response.json({ execution_status: 'SUCCEEDED', retryable: false, response: answered })
      if (url === '/api/chat/sessions/delete-session' && options?.method === 'DELETE') {
        attempts += 1
        return attempts === 1 ? Response.json({ detail: '暂时无法删除' }, { status: 503 }) : new Response(null, { status: 204 })
      }
      throw new Error('测试未定义的请求：' + String(url))
    })
    vi.stubGlobal('fetch', mock)
    const user = userEvent.setup()
    const view = renderApp()
    await user.click(screen.getByRole('button', { name: /^Chat/ }))
    const deleteName = '删除对话 待删除会话'
    await user.click(await screen.findByRole('button', { name: deleteName }))
    const dialog = screen.getByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: '确认删除' }))
    await within(dialog).findByRole('alert')
    expect(screen.getByRole('button', { name: deleteName })).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: '确认删除' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: deleteName })).not.toBeInTheDocument())
    expect(attempts).toBe(2)
    view.unmount()
    renderApp()
    await screen.findByText('中国政策.pdf')
    await user.click(screen.getByRole('button', { name: /^Chat/ }))
    expect(screen.queryByRole('button', { name: deleteName })).not.toBeInTheDocument()
  })
})


describe('Agent 聊天操作', () => {
  it.each([true, false])('取消等待后端确认并允许重提（旧请求成功返回：%s）', async (lateSuccess) => {
    const followup = clarificationFixture(false)
    const questionPath = '/questions/' + followup.question_id
    const session = { session_id: 'cancel-session', title: '入境规定' }
    let finishOldRequest: (response: Response) => void = () => { throw new Error('旧请求尚未开始') }
    let submissions = 0
    let cancelled = false
    let cancellationAttempts = 0
    const mock = vi.fn(async (url: RequestInfo | URL, options?: RequestInit) => {
      const path = String(url)
      if (path === '/api/documents') return Response.json([readyDocument])
      if (path === '/api/chat/sessions') return Response.json({ sessions: [session] })
      if (path.endsWith(questionPath + '/cancel')) {
        cancellationAttempts += 1
        if (cancellationAttempts === 1) return Response.json({ detail: '取消请求失败' }, { status: 503 })
        if (cancellationAttempts === 2) return Response.json({ execution_status: 'CANCELLED', retryable: false, response: null })
        cancelled = true
        return Response.json({ execution_status: 'CANCELLING', retryable: false, response: null })
      }
      if (path.endsWith(questionPath + '/execution')) return Response.json({
        execution_status: cancelled ? 'CANCELLED' : 'NEEDS_CLARIFICATION', retryable: cancelled, response: followup,
      })
      if (path.startsWith('/api/chat/sessions/')) return Response.json({
        questions: [{ question_id: followup.question_id, message: '入境规定', clarification: followup.clarification }],
      })
      if (path === '/api/chat') {
        submissions += 1
        if (submissions === 1) return new Promise<Response>((resolve, reject) => {
          finishOldRequest = resolve
          if (!lateSuccess) options?.signal?.addEventListener('abort', () => {
            reject(new DOMException('Aborted', 'AbortError'))
          }, { once: true })
        })
        return Response.json(answered)
      }
      throw new Error('测试未定义的请求：' + path)
    })
    vi.stubGlobal('fetch', mock)
    const user = await openChat()
    const select = await screen.findByRole('combobox')
    await waitFor(() => expect(select).toBeEnabled())
    fireEvent.submit(select.closest('form')!)
    expect(submissions).toBe(0)
    await user.selectOptions(select, 'CN')
    await user.click(screen.getByRole('button', { name: '补充并继续' }))
    await user.click(await screen.findByRole('button', { name: '取消' }))
    await screen.findByRole('alert')
    expect(screen.getByRole('button', { name: '补充并继续' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '取消' }))
    await screen.findByRole('alert')
    expect(screen.getByRole('button', { name: '补充并继续' })).toBeDisabled()
    const cancel = screen.getByRole('button', { name: '取消' })
    act(() => {
      cancel.click()
      cancel.click()
    })
    expect(await screen.findByText('正在取消')).toBeInTheDocument()
    if (lateSuccess) finishOldRequest(Response.json({ ...answered, answer: '旧请求迟到的回答' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '补充并继续' })).toBeEnabled(), { timeout: 3000 })
    expect(screen.getByRole('combobox')).toHaveValue('CN')
    await user.click(screen.getByRole('button', { name: '补充并继续' }))
    await screen.findByText('应当按照证据办理申报。', { selector: '.message-text' })
    expect(submissions).toBe(2)
    expect(cancellationAttempts).toBe(3)
    expect(screen.queryByText('旧请求迟到的回答')).not.toBeInTheDocument()
  })

  it('从冲突卡片打开对应的证据', async () => {
    const response = conflictFixture(false)
    vi.stubGlobal('fetch', async (url: RequestInfo | URL) => {
      if (url === '/api/documents') return Response.json([readyDocument])
      if (url === '/api/chat/sessions') return Response.json({ sessions: [{ session_id: 'conflict-session', title: '入境规定' }] })
      if (String(url).endsWith('/execution')) return Response.json({ execution_status: 'SUCCEEDED', retryable: false, response })
      return Response.json({ questions: [{ question_id: response.question_id, message: '入境规定' }] })
    })
    const user = await openChat()
    await user.click(await screen.findByRole('button', { name: /示例文件 B/ }))
    await waitFor(() => expect(screen.getByText(response.citations[0].quote, { selector: '.citation-panel blockquote' })).toBeVisible())
  })
})


it('新建聊天后可用示例问题发起请求，并从无文档提示返回文件页', async () => {
  const mock = vi.fn(async (url: RequestInfo | URL) => {
    if (url === '/api/documents') return Response.json([readyDocument])
    if (url === '/api/chat/sessions') return Response.json({ sessions: [] })
    if (url === '/api/chat') return Response.json({
      status: 'NO_READY_DOCUMENTS', answer: null, message: '没有可用文档', citations: [],
    })
    throw new Error('测试未定义的请求：' + String(url))
  })
  vi.stubGlobal('fetch', mock)
  const user = await openChat()
  const newChat = screen.getAllByRole('button', { name: /新建/ })[0]
  await user.click(newChat)
  await user.click(screen.getByRole('button', { name: '进出中国时，可以委托他人办理行李物品的海关手续吗？' }))
  await screen.findByText('没有可用文档', { selector: '.message-text' })
  await user.click(screen.getAllByRole('button', { name: '前往 Documents' })[0])
  expect(screen.getByRole('heading', { name: 'Documents' })).toBeVisible()
})

it('恢复失败时显示提示，取消删除时保留会话且不发送删除请求', async () => {
  const mock = vi.fn(async (url: RequestInfo | URL, options?: RequestInit) => {
    if (url === '/api/documents') return Response.json([readyDocument])
    if (url === '/api/chat/sessions') return Response.json({ sessions: [{ session_id: 'offline', title: '恢复失败的会话' }] })
    if (url === '/api/chat/sessions/offline' && options?.method !== 'DELETE') {
      return Response.json({ detail: '暂时无法恢复' }, { status: 503 })
    }
    throw new Error('测试未定义的请求：' + String(url))
  })
  vi.stubGlobal('fetch', mock)
  const user = await openChat()
  await screen.findByRole('alert')
  const deleteButton = screen.getByRole('button', { name: '删除对话 恢复失败的会话' })
  await user.click(deleteButton)
  await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: '取消' }))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  await user.click(deleteButton)
  const backdrop = screen.getByRole('dialog').parentElement!
  await user.click(backdrop)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(deleteButton).toBeVisible()
  expect(mock.mock.calls.some(([, options]) => options?.method === 'DELETE')).toBe(false)
})

it('Enter 发送当前问题，空输入不发送', async () => {
  const mock = vi.fn(async (url: RequestInfo | URL) => {
    if (url === '/api/documents') return Response.json([readyDocument])
    if (url === '/api/chat/sessions') return Response.json({ sessions: [] })
    if (url === '/api/chat') return Response.json(answered)
    throw new Error('测试未定义的请求：' + String(url))
  })
  vi.stubGlobal('fetch', mock)
  const user = await openChat()
  const input = screen.getByRole('textbox')
  await user.click(input)
  await user.keyboard('{Enter}')
  expect(mock.mock.calls.filter(([path]) => path === '/api/chat')).toHaveLength(0)
  await user.type(input, '入境规定{Enter}')
  await screen.findByText('应当按照证据办理申报。', { selector: '.message-text' })
  expect(mock.mock.calls.filter(([path]) => path === '/api/chat')).toHaveLength(1)
})

it('切页续跑，恢复时轮询未完成的请求并保留本地问题', async () => {
  const session = { session_id: 'running', title: '旧问题' }
  let finish: (response: Response) => void = () => { throw new Error('请求尚未开始') }
  let hasSubmitted = false
  let emptySnapshot = false
  let executionReads = 0
  const mock = vi.fn(async (url: RequestInfo | URL) => {
    if (url === '/api/documents') return Response.json([readyDocument])
    if (url === '/api/chat/sessions') return Response.json({ sessions: [session] })
    if (url === '/api/chat/sessions/running') return Response.json({
      ...session, questions: emptySnapshot ? [] : [{ question_id: 'q1', message: '旧问题' }],
    })
    if (url === '/api/chat/sessions/running/questions/q1/execution') {
      executionReads += 1
      const running = executionReads === 1
      return Response.json({ execution_status: running ? 'RUNNING' : 'SUCCEEDED', retryable: false,
        response: running ? null : { ...answered, model_name: 'restored-provider-model' } })
    }
    if (url === '/api/chat') {
      hasSubmitted = true
      return new Promise<Response>(resolve => { finish = resolve })
    }
    throw new Error('测试未定义的请求：' + String(url))
  })
  vi.stubGlobal('fetch', mock)
  const user = await openChat()
  await screen.findByText('旧问题', { selector: '.message-text' })
  await screen.findByText('应当按照证据办理申报。', { selector: '.message-text' }, { timeout: 2000 })
  expect(screen.getByText('模型：restored-provider-model')).toBeVisible()
  expect(executionReads).toBeGreaterThanOrEqual(2)
  await ask(user, '切页续跑的问题')
  expect(hasSubmitted).toBe(true)
  const detailReads = () => mock.mock.calls.filter(([path]) => path === '/api/chat/sessions/running').length
  const beforeSwitch = detailReads()
  await user.click(screen.getByRole('button', { name: /^Documents/ }))
  await user.click(screen.getByRole('button', { name: /^Chat/ }))
  expect(detailReads()).toBe(beforeSwitch)
  await user.click(screen.getByRole('button', { name: /^Documents/ }))
  finish(Response.json({ ...answered, answer: '切页后收到的回答' }))
  emptySnapshot = true
  await user.click(screen.getByRole('button', { name: /^Chat/ }))
  await waitFor(() => expect(detailReads()).toBeGreaterThan(beforeSwitch))
  expect(await screen.findByText('切页后收到的回答', { selector: '.message-text' })).toBeVisible()
  expect(screen.getByText('切页续跑的问题', { selector: '.message-text' })).toBeVisible()
})


it('追问取消回调抛错时显示提示并允许再次取消', async () => {
  const { clarification } = clarificationFixture(false)
  if (!clarification) throw new Error('测试需要追问字段')
  const user = userEvent.setup()
  const onCancel = vi.fn<() => Promise<boolean>>()
    .mockRejectedValueOnce(new Error('取消请求失败'))
    .mockResolvedValueOnce(true)
  const onSubmit = vi.fn(async (_conditions: Record<string, string>, signal: AbortSignal) => {
    await new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), { once: true }))
  })
  render(<I18nProvider><ClarificationForm clarification={clarification} busy={false}
    values={{ destination_country: 'CN' }} onChange={() => {}} onSubmit={onSubmit} onCancel={onCancel}
  /></I18nProvider>)
  await user.click(screen.getByRole('button', { name: '补充并继续' }))
  await user.click(screen.getByRole('button', { name: '取消' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('未能确认取消')
  await user.click(screen.getByRole('button', { name: '取消' }))
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  expect(onCancel).toHaveBeenCalledTimes(2)
})
