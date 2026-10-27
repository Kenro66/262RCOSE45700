import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { koKR } from './ko-KR'
import { zhCN, type TranslationKey } from './zh-CN'

export type Locale = 'zh-CN' | 'ko-KR'
type Variables = Record<string, string | number>

type I18nValue = {
  locale: Locale
  setLocale: (locale: Locale) => void
  t: (key: TranslationKey, variables?: Variables) => string
}

const dictionaries = { 'zh-CN': zhCN, 'ko-KR': koKR }
const I18nContext = createContext<I18nValue | null>(null)

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [locale, setLocale] = useState<Locale>(() => {
    const saved = window.localStorage.getItem('policy-ai-locale')
    return saved === 'zh-CN' || saved === 'ko-KR' ? saved : 'ko-KR'
  })

  useEffect(() => {
    window.localStorage.setItem('policy-ai-locale', locale)
    document.documentElement.lang = locale
  }, [locale])

 const value = useMemo<I18nValue>(() => ({
  locale,
  setLocale,
  t: (key, variables = {}) => Object.entries(variables).reduce(
    (text, [name, replacement]) =>
      text.split(`{${name}}`).join(String(replacement)),
    dictionaries[locale][key],
  ),
}), [locale])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n() {
  const value = useContext(I18nContext)
  if (!value) throw new Error('useI18n must be used within I18nProvider')
  return value
}
