const API_BASE_URL = '/api'

export class ApiError extends Error {
  constructor(
    public status: number,
    public data: unknown
  ) {
    super('API request failed')
    this.name = 'ApiError'
  }
}

export async function apiRequest<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, options)

  const data: unknown = await response.json()

  if (!response.ok) {
    throw new ApiError(response.status, data)
  }

  return data as T
}

// ========================================
// C1：Documents 文件列表接口
// 对应 Issue #8 的 GET /api/documents
// ========================================

// 定义后端返回的 PDF 文件信息格式
export type ApiDocument = {
  document_id: string           // 文件唯一 ID，删除文件时使用
  filename: string              // PDF 文件名
  status: 'READY' | 'PROCESSING' | 'FAILED'  // 文件处理状态
  error_message: string | null  // 处理失败原因，正常时为 null
  page_count: number | null     // PDF 页数，尚未处理完成时可能为 null
  uploaded_at: string           // 文件上传时间
}

// 获取后端的真实文件列表
export function getDocuments(): Promise<ApiDocument[]> {
  // GET 请求，最终访问 /api/documents
  return apiRequest<ApiDocument[]>('/documents', {
    method: 'GET',
  })
}

// ========================================
// C1：上传 PDF 文件接口
// POST /api/documents
// ========================================

// 把用户选择的 PDF 文件发送给后端
export function uploadDocument(file: File): Promise<ApiDocument> {
  const formData = new FormData()
  formData.append('file', file)

  return apiRequest<ApiDocument>('/documents', {
    method: 'POST',
    body: formData,
  })
}

// ========================================
// C1：删除 PDF 文件接口
// DELETE /api/documents/{id}
// ========================================

// 使用后端返回的 document_id 删除真实文件
export function deleteDocument(documentId: string): Promise<unknown> {
  return apiRequest<unknown>(
    `/documents/${encodeURIComponent(documentId)}`,
    {
      method: 'DELETE',
    }
  )
}

// ========================================
// 统一处理后端 HTTP 400 / 500 错误
// 保留后端返回的 JSON 内容
// ========================================

export function getApiErrorData(error: unknown): unknown {
  if (error instanceof ApiError) {
    return error.data
  }

  return null
}

// 判断后端是否返回了 C5 格式的 SYSTEM_ERROR
export function isSystemErrorResponse(
  data: unknown
): data is {
  status: 'SYSTEM_ERROR'
  answer: null
  citations: unknown[]
  message: string
} {
  if (typeof data !== 'object' || data === null) {
    return false
  }

  const value = data as Record<string, unknown>

  return (
    value.status === 'SYSTEM_ERROR' &&
    value.answer === null &&
    Array.isArray(value.citations) &&
    typeof value.message === 'string'
  )
}

// ========================================
// 本地测试：模拟后端 HTTP 500
// 对应 Issue #8 的 SYSTEM_ERROR JSON
// 不发送真实网络请求
// ========================================

export function createMockSystemError(): ApiError {
  return new ApiError(500, {
    status: 'SYSTEM_ERROR',
    answer: null,
    citations: [],
    message: '系统暂时无法处理请求，请稍后重试。',
  })
}