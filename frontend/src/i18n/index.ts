import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import LanguageDetector from 'i18next-browser-languagedetector'

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

export const namespaces = [
  'common', 'auth', 'dashboard', 'code', 'agent', 'apps', 'codeapp',
  'fullstack', 'team', 'settings', 'errors', 'admin', 'canvas', 'notifications',
] as const

const FALLBACK_LNG: SupportedLanguage = 'en'

type LocaleBundle = Record<string, Record<string, unknown>>

// One lazily-loaded chunk per language (the barrel statically pulls in that
// language's 14 namespace JSONs, so each `import()` resolves to a single chunk).
// The main bundle therefore ships NO locale data — previously all 4 languages ×
// 14 namespaces were baked into the entry chunk; now only the active language
// (+ the English fallback) is fetched at startup and the rest load on switch.
const languageLoaders: Record<SupportedLanguage, () => Promise<{ default: LocaleBundle }>> = {
  en: () => import('@/locales/en'),
  'zh-CN': () => import('@/locales/zh-CN'),
  ja: () => import('@/locales/ja'),
  ko: () => import('@/locales/ko'),
}

const loadedLanguages = new Set<SupportedLanguage>()

/** Fetch one language's bundle and register every namespace on the i18n instance. */
async function registerLanguage(lng: SupportedLanguage): Promise<void> {
  if (loadedLanguages.has(lng)) return
  const bundle = (await languageLoaders[lng]()).default
  for (const ns of namespaces) {
    if (bundle[ns]) i18n.addResourceBundle(lng, ns, bundle[ns], true, true)
  }
  loadedLanguages.add(lng)
}

/** Resolve the initial language the same way the detector would (localStorage cache
 *  first, then the browser language), so we preload the right bundle before init. */
function detectInitialLanguage(): SupportedLanguage {
  const stored =
    typeof localStorage !== 'undefined' ? localStorage.getItem('i18n-language') : null
  if (stored && (supportedLanguages as readonly string[]).includes(stored)) {
    return stored as SupportedLanguage
  }
  const nav = (typeof navigator !== 'undefined' && navigator.language) || 'en'
  const lower = nav.toLowerCase()
  if (lower.startsWith('zh')) return 'zh-CN'
  const base = lower.split('-')[0]
  return (supportedLanguages as readonly string[]).includes(base)
    ? (base as SupportedLanguage)
    : FALLBACK_LNG
}

const initialLng = detectInitialLanguage()

// Preload the active language (+ the English fallback when different) BEFORE
// init, building the initial resource set. Top-level await keeps importers
// (main.tsx imports this for its side effect) suspended until resources are ready,
// so the first render is fully translated — no key-flash, no Suspense gymnastics.
const [activeBundle, fallbackBundle] = await Promise.all([
  languageLoaders[initialLng](),
  initialLng === FALLBACK_LNG ? Promise.resolve(null) : languageLoaders[FALLBACK_LNG](),
])
const initialResources: Record<string, LocaleBundle> = { [initialLng]: activeBundle.default }
if (fallbackBundle) initialResources[FALLBACK_LNG] = fallbackBundle.default

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: initialResources,
    lng: initialLng,
    fallbackLng: FALLBACK_LNG,
    supportedLngs: supportedLanguages as unknown as string[],
    ns: namespaces as unknown as string[],
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

loadedLanguages.add(initialLng)
if (initialLng !== FALLBACK_LNG) loadedLanguages.add(FALLBACK_LNG)

/**
 * Switch language, loading its bundle first. Use this instead of
 * ``i18n.changeLanguage`` directly: a not-yet-loaded language must have its
 * resources registered before the switch, otherwise the UI flashes raw keys (or
 * fallback English) until the bundle arrives.
 */
export async function changeLanguage(lng: SupportedLanguage): Promise<void> {
  await registerLanguage(lng)
  await i18n.changeLanguage(lng)
}

export default i18n

// Utility function for use in stores (outside React components)
export const t = (key: string, options?: Record<string, unknown>) => i18n.t(key, options)
