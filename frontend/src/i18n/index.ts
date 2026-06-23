import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import LanguageDetector from 'i18next-browser-languagedetector'

// Import all translation files
import enCommon from '@/locales/en/common.json'
import enAuth from '@/locales/en/auth.json'
import enDashboard from '@/locales/en/dashboard.json'
import enCode from '@/locales/en/code.json'
import enAgent from '@/locales/en/agent.json'
import enCodeApp from '@/locales/en/codeapp.json'
import enFullstack from '@/locales/en/fullstack.json'
import enTeam from '@/locales/en/team.json'
import enSettings from '@/locales/en/settings.json'
import enErrors from '@/locales/en/errors.json'
import enAdmin from '@/locales/en/admin.json'
import enCanvas from '@/locales/en/canvas.json'

import zhCommon from '@/locales/zh-CN/common.json'
import zhAuth from '@/locales/zh-CN/auth.json'
import zhDashboard from '@/locales/zh-CN/dashboard.json'
import zhCode from '@/locales/zh-CN/code.json'
import zhAgent from '@/locales/zh-CN/agent.json'
import zhCodeApp from '@/locales/zh-CN/codeapp.json'
import zhFullstack from '@/locales/zh-CN/fullstack.json'
import zhTeam from '@/locales/zh-CN/team.json'
import zhSettings from '@/locales/zh-CN/settings.json'
import zhErrors from '@/locales/zh-CN/errors.json'
import zhAdmin from '@/locales/zh-CN/admin.json'
import zhCanvas from '@/locales/zh-CN/canvas.json'

import jaCommon from '@/locales/ja/common.json'
import jaAuth from '@/locales/ja/auth.json'
import jaDashboard from '@/locales/ja/dashboard.json'
import jaCode from '@/locales/ja/code.json'
import jaAgent from '@/locales/ja/agent.json'
import jaCodeApp from '@/locales/ja/codeapp.json'
import jaFullstack from '@/locales/ja/fullstack.json'
import jaTeam from '@/locales/ja/team.json'
import jaSettings from '@/locales/ja/settings.json'
import jaErrors from '@/locales/ja/errors.json'
import jaAdmin from '@/locales/ja/admin.json'
import jaCanvas from '@/locales/ja/canvas.json'

import koCommon from '@/locales/ko/common.json'
import koAuth from '@/locales/ko/auth.json'
import koDashboard from '@/locales/ko/dashboard.json'
import koCode from '@/locales/ko/code.json'
import koAgent from '@/locales/ko/agent.json'
import koCodeApp from '@/locales/ko/codeapp.json'
import koFullstack from '@/locales/ko/fullstack.json'
import koTeam from '@/locales/ko/team.json'
import koSettings from '@/locales/ko/settings.json'
import koErrors from '@/locales/ko/errors.json'
import koAdmin from '@/locales/ko/admin.json'
import koCanvas from '@/locales/ko/canvas.json'

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
    code: enCode,
    agent: enAgent,
    codeapp: enCodeApp,
    fullstack: enFullstack,
    team: enTeam,
    settings: enSettings,
    errors: enErrors,
    admin: enAdmin,
    canvas: enCanvas,
  },
  'zh-CN': {
    common: zhCommon,
    auth: zhAuth,
    dashboard: zhDashboard,
    code: zhCode,
    agent: zhAgent,
    codeapp: zhCodeApp,
    fullstack: zhFullstack,
    team: zhTeam,
    settings: zhSettings,
    errors: zhErrors,
    admin: zhAdmin,
    canvas: zhCanvas,
  },
  ja: {
    common: jaCommon,
    auth: jaAuth,
    dashboard: jaDashboard,
    code: jaCode,
    agent: jaAgent,
    codeapp: jaCodeApp,
    fullstack: jaFullstack,
    team: jaTeam,
    settings: jaSettings,
    errors: jaErrors,
    admin: jaAdmin,
    canvas: jaCanvas,
  },
  ko: {
    common: koCommon,
    auth: koAuth,
    dashboard: koDashboard,
    code: koCode,
    agent: koAgent,
    codeapp: koCodeApp,
    fullstack: koFullstack,
    team: koTeam,
    settings: koSettings,
    errors: koErrors,
    admin: koAdmin,
    canvas: koCanvas,
  },
}

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    fallbackLng: 'en',
    supportedLngs: supportedLanguages,
    ns: ['common', 'auth', 'dashboard', 'code', 'agent', 'codeapp', 'fullstack', 'team', 'settings', 'errors', 'admin', 'canvas'],
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
