import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { I18nProvider } from '../src/i18n/I18nContext'

test('开发预览显示场景入口，聊天走假数据并禁止文档写入', async () => {
  window.history.replaceState(null, '', '/?mock=1&scenario=conflict')
  try {
    const { default: App } = await import('../src/App')
    const { apiRequest, getDocuments, sendChatMessage } = await import('../src/api/client')
    render(<I18nProvider><App /></I18nProvider>)
    expect(screen.getByRole('link', { name: '跨页引用' })).toHaveAttribute('href', '?mock=1&scenario=page-range')
    await expect(getDocuments()).resolves.toEqual([])
    await expect(apiRequest('/documents', { method: 'POST' })).rejects.toThrow('Document mutations are disabled')
    await expect(sendChatMessage({ session_id: 'preview', message: '入境规定' })).resolves.toMatchObject({
      status: 'CONFLICTING_EVIDENCE',
    })
  } finally {
    window.history.replaceState(null, '', '/')
  }
})
