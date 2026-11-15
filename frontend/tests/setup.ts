import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, vi } from 'vitest'

beforeEach(() => {
  window.localStorage.clear()
  window.localStorage.setItem('policy-ai-locale', 'zh-CN')
  // jsdom 不实现滚动布局，仅补足此浏览器接口，不替换页面行为。
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  cleanup()
})
