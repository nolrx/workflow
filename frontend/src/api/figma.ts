/**
 * Figma integration API client (Code domain).
 *
 * Credential management + URL resolve + design attach live here. "Attach" pulls
 * a whole Figma file (all frames) onto the project; a later
 * `code_frontend_project_generation` run then builds the multi-file React
 * project to match the attached design. Export (app -> Figma plugin) also lives here.
 */
import { api } from "@/api/client"

// Resolve hits the Figma REST API server-side; give it the long AI-style timeout.
const FIGMA_TIMEOUT = 60000
// Attach pulls the whole file + renders every frame — can take a while.
const FIGMA_ATTACH_TIMEOUT = 120000

export interface FigmaCredential {
  has_token: boolean
  id?: string
  token_last4?: string | null
  label?: string | null
  created_at?: string
  updated_at?: string
}

export interface FigmaResolved {
  file_key: string
  node_id: string | null
  name: string | null
  thumbnail_url: string | null
  last_modified: string | null
}

/** One frame of an attached Figma design (IR/render kept server-side). */
export interface FigmaFrame {
  node_id: string
  name: string
  order: number
  width: number | null
  height: number | null
}

export interface FigmaAttachedDesign {
  id: string
  project_id: string
  file_key: string
  file_name: string | null
  source_url: string | null
  count: number
  frames: FigmaFrame[]
  created_at?: string | null
  updated_at?: string | null
}

export type FigmaExportSource = "preview_image" | "html" | "sliced"

export interface FigmaExportResult {
  id: string
  pairing_code: string
  source: string
  expires_at: string
  ttl_seconds: number
}

interface Envelope<T> {
  data: T
  message?: string
}

export const figmaApi = {
  getCredential: async (): Promise<FigmaCredential> => {
    const response = await api.get<Envelope<FigmaCredential>>("/code/figma/credential")
    return response.data
  },
  saveCredential: async (token: string, label?: string): Promise<FigmaCredential> => {
    const response = await api.post<Envelope<FigmaCredential>>("/code/figma/credential", {
      token,
      label,
    })
    return response.data
  },
  deleteCredential: async (): Promise<void> => {
    await api.delete<Envelope<unknown>>("/code/figma/credential")
  },
  resolveUrl: async (figmaUrl: string): Promise<FigmaResolved> => {
    const response = await api.post<Envelope<FigmaResolved>>(
      "/code/figma/resolve",
      { figma_url: figmaUrl },
      { timeout: FIGMA_TIMEOUT }
    )
    return response.data
  },
  /** Attach a whole Figma file (all frames) to the project (UPSERT). */
  attachDesign: async (projectId: string, figmaUrl: string): Promise<FigmaAttachedDesign> => {
    const response = await api.post<Envelope<{ design: FigmaAttachedDesign }>>(
      `/code/figma/projects/${projectId}/attach`,
      { figma_url: figmaUrl },
      { timeout: FIGMA_ATTACH_TIMEOUT }
    )
    return response.data.design
  },
  /** The project's currently attached design (or null). */
  getDesign: async (projectId: string): Promise<FigmaAttachedDesign | null> => {
    const response = await api.get<Envelope<{ design: FigmaAttachedDesign | null }>>(
      `/code/figma/projects/${projectId}/design`
    )
    return response.data.design
  },
  detachDesign: async (projectId: string): Promise<void> => {
    await api.delete<Envelope<unknown>>(`/code/figma/projects/${projectId}/design`)
  },
  exportToFigma: async (
    projectId: string,
    opts: { source: FigmaExportSource; previewId?: string; runId?: string }
  ): Promise<FigmaExportResult> => {
    const response = await api.post<Envelope<FigmaExportResult>>(
      `/code/figma/projects/${projectId}/export`,
      { source: opts.source, preview_id: opts.previewId, run_id: opts.runId },
      { timeout: FIGMA_TIMEOUT }
    )
    return response.data
  },
}
