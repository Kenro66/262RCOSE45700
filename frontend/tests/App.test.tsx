import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../src/App'
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
    fetcher.mockImplementation(async (url) => Response.json(url === '/api/chat' ? answered : [readyDocument]))
    const user = await openChat()
    await ask(user)
    expect(await screen.findByText('应当按照证据办理申报。')).toBeVisible()
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
      ? { status, answer: status === 'INSUFFICIENT_EVIDENCE' ? text : null, citations: [], message: text }
      : [readyDocument]))
    const user = await openChat()
    await ask(user)
    expect(await screen.findByText(text)).toBeVisible()
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
    await user.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('应当按照证据办理申报。')).toBeVisible()
    const requests = fetcher.mock.calls.filter(([url]) => url === '/api/chat')
    expect(requests).toHaveLength(2)
    expect(requests.map(([, options]) => JSON.parse(options!.body as string))).toEqual([
      { message: '入境需要申报吗？' }, { message: '入境需要申报吗？' },
    ])
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
