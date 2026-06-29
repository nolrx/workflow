// Per-language translation bundle — Vite emits one lazily-loadable chunk per
// language, so the main app bundle ships NO locale data. Only the active language
// (+ the English fallback) is fetched at startup; the others load on demand when
// the user switches language. See src/i18n/index.ts.
import common from './common.json'
import auth from './auth.json'
import dashboard from './dashboard.json'
import code from './code.json'
import agent from './agent.json'
import apps from './apps.json'
import codeapp from './codeapp.json'
import fullstack from './fullstack.json'
import team from './team.json'
import settings from './settings.json'
import errors from './errors.json'
import admin from './admin.json'
import canvas from './canvas.json'
import notifications from './notifications.json'

export default {
  common,
  auth,
  dashboard,
  code,
  agent,
  apps,
  codeapp,
  fullstack,
  team,
  settings,
  errors,
  admin,
  canvas,
  notifications,
}
