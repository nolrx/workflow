# Worksflow — Figma Importer plugin

Rebuilds a design exported from Worksflow (a Code-domain UI preview or
generated app) as native Figma layers.

## How it works

1. In the platform, open a project and click **导出到 Figma** → you get an 8-char
   one-time **pairing code** (valid 5 minutes).
2. In Figma, run this plugin, paste your backend URL + the pairing code, and click
   **导入**.
3. The plugin's UI fetches the export package from the unauthenticated
   `GET /api/code/figma/pull?code=…` endpoint (the pairing code is the credential),
   decodes any inline images, and the plugin main thread reconstructs the Design
   IR as Figma frames / rectangles / text / image fills.

## Develop / load locally

```bash
cd plugin/figma
npm install
npm run build        # bundles src/code.ts -> code.js (esbuild)
npm run typecheck    # tsc --noEmit
```

Then in the Figma desktop app: **Plugins → Development → Import plugin from
manifest…** and choose `plugin/figma/manifest.json`. Re-run `npm run build` (or
`npm run watch`) after editing `src/`.

## Production note

`manifest.json` sets `networkAccess.allowedDomains` to `"*"` for local
development. Before publishing, pin it to your exact backend origin, e.g.
`["https://studio.example.com"]`.
