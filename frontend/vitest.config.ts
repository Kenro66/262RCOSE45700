import { defineConfig } from 'vitest/config'

export default defineConfig({
  // 独立测试配置，避免加载仅供 Figma 开发预览使用的插件。
  oxc: { jsx: { runtime: 'automatic' } },
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
    restoreMocks: true,
    unstubGlobals: true,
    reporters: ['default', 'junit'],
    outputFile: { junit: 'test-results/frontend.xml' },
    coverage: {
      provider: 'v8',
      reportOnFailure: true,
      include: ['src/**/*.{ts,tsx,js,jsx}'],
      exclude: ['src/**/*.d.ts'],
      reporter: ['text', 'html', 'lcov', 'cobertura', 'json'],
    },
  },
})
