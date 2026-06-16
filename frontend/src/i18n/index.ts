import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import LanguageDetector from 'i18next-browser-languagedetector'

// Import all translation files
import enCommon from '@/locales/en/common.json'
import enAuth from '@/locales/en/auth.json'
import enDashboard from '@/locales/en/dashboard.json'
import enPpt from '@/locales/en/ppt.json'
import enRedbook from '@/locales/en/redbook.json'
import enCode from '@/locales/en/code.json'
import enAgent from '@/locales/en/agent.json'
import enCodeApp from '@/locales/en/codeapp.json'
import enTeam from '@/locales/en/team.json'
import enSettings from '@/locales/en/settings.json'
import enErrors from '@/locales/en/errors.json'

import zhCommon from '@/locales/zh-CN/common.json'
import zhAuth from '@/locales/zh-CN/auth.json'
import zhDashboard from '@/locales/zh-CN/dashboard.json'
import zhPpt from '@/locales/zh-CN/ppt.json'
import zhRedbook from '@/locales/zh-CN/redbook.json'
import zhCode from '@/locales/zh-CN/code.json'
import zhAgent from '@/locales/zh-CN/agent.json'
import zhCodeApp from '@/locales/zh-CN/codeapp.json'
import zhTeam from '@/locales/zh-CN/team.json'
import zhSettings from '@/locales/zh-CN/settings.json'
import zhErrors from '@/locales/zh-CN/errors.json'

import jaCommon from '@/locales/ja/common.json'
import jaAuth from '@/locales/ja/auth.json'
import jaDashboard from '@/locales/ja/dashboard.json'
import jaPpt from '@/locales/ja/ppt.json'
import jaRedbook from '@/locales/ja/redbook.json'
import jaCode from '@/locales/ja/code.json'
import jaAgent from '@/locales/ja/agent.json'
import jaCodeApp from '@/locales/ja/codeapp.json'
import jaTeam from '@/locales/ja/team.json'
import jaSettings from '@/locales/ja/settings.json'
import jaErrors from '@/locales/ja/errors.json'

import koCommon from '@/locales/ko/common.json'
import koAuth from '@/locales/ko/auth.json'
import koDashboard from '@/locales/ko/dashboard.json'
import koPpt from '@/locales/ko/ppt.json'
import koRedbook from '@/locales/ko/redbook.json'
import koCode from '@/locales/ko/code.json'
import koAgent from '@/locales/ko/agent.json'
import koCodeApp from '@/locales/ko/codeapp.json'
import koTeam from '@/locales/ko/team.json'
import koSettings from '@/locales/ko/settings.json'
import koErrors from '@/locales/ko/errors.json'

export const supportedLanguages = ['en', 'zh-CN', 'ja', 'ko'] as const
export type SupportedLanguage = (typeof supportedLanguages)[number]

export const languageNames: Record<SupportedLanguage, string> = {
  en: 'English',
  'zh-CN': '简体中文',
  ja: '日本語',
  ko: '한국어',
}

export const languageFlags: Record<SupportedLanguage, string> = {
  en: '🇺🇸',
  'zh-CN': '🇨🇳',
  ja: '🇯🇵',
  ko: '🇰🇷',
}

const resources = {
  en: {
    common: enCommon,
    auth: enAuth,
    dashboard: enDashboard,
    ppt: enPpt,
    redbook: enRedbook,
    code: enCode,
    agent: enAgent,
    codeapp: enCodeApp,
    team: enTeam,
    settings: enSettings,
    errors: enErrors,
  },
  'zh-CN': {
    common: zhCommon,
    auth: zhAuth,
    dashboard: zhDashboard,
    ppt: zhPpt,
    redbook: zhRedbook,
    code: zhCode,
    agent: zhAgent,
    codeapp: zhCodeApp,
    team: zhTeam,
    settings: zhSettings,
    errors: zhErrors,
  },
  ja: {
    common: jaCommon,
    auth: jaAuth,
    dashboard: jaDashboard,
    ppt: jaPpt,
    redbook: jaRedbook,
    code: jaCode,
    agent: jaAgent,
    codeapp: jaCodeApp,
    team: jaTeam,
    settings: jaSettings,
    errors: jaErrors,
  },
  ko: {
    common: koCommon,
    auth: koAuth,
    dashboard: koDashboard,
    ppt: koPpt,
    redbook: koRedbook,
    code: koCode,
    agent: koAgent,
    codeapp: koCodeApp,
    team: koTeam,
    settings: koSettings,
    errors: koErrors,
  },
}

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    fallbackLng: 'en',
    supportedLngs: supportedLanguages,
    ns: ['common', 'auth', 'dashboard', 'ppt', 'redbook', 'code', 'agent', 'codeapp', 'team', 'settings', 'errors'],
    defaultNS: 'common',

    detection: {
      order: ['localStorage', 'navigator', 'htmlTag'],
      caches: ['localStorage'],
      lookupLocalStorage: 'i18n-language',
    },

    interpolation: {
      escapeValue: false, // React already escapes
    },
  })

export default i18n

// Utility function for use in stores (outside React components)
export const t = (key: string, options?: Record<string, unknown>) => i18n.t(key, options)
