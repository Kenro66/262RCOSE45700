import { describe, expect, it, vi } from 'vitest'
import {
  ApiError, deleteDocument, getDocuments, sendChatMessage, uploadDocument,
} from '../src/api/client'

describe('C1/C5 接口需求', () => {
  it('C1-01：从同源 /api/documents 读取后端原始文件列表', async () => {
    const documents = [{
      document_id: 'DOC_REAL', filename: '政策.pdf', status: 'READY',
      error_message: null, page_count: 2, uploaded_at: '2026-11-13T00:00:00Z',
    }]
    const fetcher = vi.fn().mockResolvedValue(Response.json(documents))
    vi.stubGlobal('fetch', fetcher)
    expect(await getDocuments()).toEqual(documents)
    expect(fetcher).toHaveBeenCalledWith('/api/documents', { method: 'GET' })
  })

  it('C1-02：上传使用 multipart 的 file 字段，不把 PDF 当 JSON 发送', async () => {
    const file = new File(['%PDF-1.7'], '政策.pdf', { type: 'application/pdf' })
    const fetcher = vi.fn().mockResolvedValue(Response.json({
      document_id: 'DOC_UPLOAD', filename: file.name, status: 'PROCESSING',
      error_message: null, page_count: null, uploaded_at: '2026-11-13T00:00:00Z',
    }, { status: 201 }))
    vi.stubGlobal('fetch', fetcher)
    expect((await uploadDocument(file)).status).toBe('PROCESSING')
    const [url, options] = fetcher.mock.calls[0]
    expect(url).toBe('/api/documents')
    expect(options.method).toBe('POST')
    expect(options.body).toBeInstanceOf(FormData)
    expect(options.body.get('file')).toBe(file)
    expect(options.headers).toBeUndefined()
  })

  it('C1-03：删除使用编码后的真实 document_id，204 不读取 JSON', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetcher)
    await expect(deleteDocument('DOC /中文')).resolves.toBeNull()
    expect(fetcher).toHaveBeenCalledWith('/api/documents/DOC%20%2F%E4%B8%AD%E6%96%87', {
      method: 'DELETE',
    })
  })

  it('C5-01：问答发送 message，并将取消信号传给请求', async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({
      status: 'NO_READY_DOCUMENTS', answer: null, citations: [], message: '当前没有可用文件',
    }))
    vi.stubGlobal('fetch', fetcher)
    const controller = new AbortController()
    await sendChatMessage('入境规定是什么？', controller.signal)
    expect(fetcher).toHaveBeenCalledWith('/api/chat', {
      method: 'POST', signal: controller.signal,
      headers: { 'Content-Type': 'application/json' },
      body: '{"message":"入境规定是什么？"}',
    })
  })

  it.each([400, 422, 500])('ERR-01：HTTP %i 保留状态码和后端错误体', async (status) => {
    const data = status === 500
      ? { status: 'SYSTEM_ERROR', answer: null, citations: [], message: '服务暂不可用' }
      : { detail: '请求内容不符合要求' }
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => Response.json(data, { status })))
    await expect(sendChatMessage('问题')).rejects.toMatchObject({
      name: 'ApiError', status, data,
    })
    await expect(getDocuments()).rejects.toBeInstanceOf(ApiError)
  })
})

// C5：传输失败与取消必须向调用者报告，不能伪装成成功响应。
describe('C5 传输边界', () => {
  it('ERR-02：网络失败保留原异常，不返回伪造回答', async () => {
    const error = new TypeError('Network unavailable')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(error))
    await expect(sendChatMessage('入境政策是什么？')).rejects.toBe(error)
  })

  it('C5-02：取消请求传播 AbortError 和原始信号', async () => {
    const controller = new AbortController()
    const error = new DOMException('Aborted', 'AbortError')
    const fetcher = vi.fn().mockRejectedValue(error)
    vi.stubGlobal('fetch', fetcher)
    controller.abort()
    await expect(sendChatMessage('入境政策是什么？', controller.signal)).rejects.toBe(error)
    expect(fetcher.mock.calls[0][1].signal).toBe(controller.signal)
  })
})
