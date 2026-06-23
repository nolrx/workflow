import { api } from "@/api/client"

// AI generation routes run a full synchronous LLM/image call server-side
// (Claude's read timeout is 120s). The global 30s axios timeout aborts these
// long requests even though the backend keeps running and persists the result —
// which surfaced as "generation failed" while the document only appeared after a
// manual page refresh. Give these endpoints a generous per-request timeout.
const AI_GENERATION_TIMEOUT = 180000

export interface UIStyle {
  id: string
  name: string
  description: string
  prompt: string
  source_url: string
}

export interface PromptPrefix {
  id: string
  name: string
  category: string
  description: string
  recommended_outputs: string[]
  text?: string
}

export interface PromptRoute {
  selected_prefixes: string[]
  reason: string
  primary_role: string
  secondary_roles: string[]
  missing_context: string[]
  recommended_system_prompt_order: string[]
}

export interface CodeDocument {
  id: string
  project_id: string
  document_type: string
  title: string
  content: string
  prompt_expert: string
  order_index: number
  created_at: string
  updated_at: string
}

export interface PreviewImage {
  id: string
  url: string
  prompt: string
}

/** One selectable option in a requirements clarification question. */
export interface ClarificationOption {
  value: string
  label: string
  description?: string
}

/**
 * A requirements clarification question generated alongside the requirements
 * doc. The front-end renders it as a single/multi selection control (+ optional
 * free-text) in the confirmation dialog; unanswered questions fall back to
 * `default`. See docs/requirements-clarify-spec.md.
 */
export interface ClarificationQuestion {
  id: string
  category?: string
  question: string
  type: "single" | "multi"
  options: ClarificationOption[]
  /** Recommended option value(s) — used when the user doesn't change the answer. */
  default: string[]
  /** Whether to show a free-text "other" input for this question. */
  allow_custom: boolean
  rationale?: string
}

export interface CodeProject {
  id: string
  user_id: string
  team_id: string | null
  title: string
  requirement_input: string
  requirements_doc: string | null
  development_flow: string | null
  style_prompt: string | null
  ui_baseline_prompt: string | null
  confirmed_preview_url: string | null
  selected_style_ids: string[]
  preview_images: PreviewImage[]
  documents: CodeDocument[]
  status: string
  visibility: string
  created_at: string
  updated_at: string
}

export type StageVersionSource =
  | "generated"
  | "manual_edit"
  | "partial_revision"
  | "rollback"
  | "import"

/** One historical version of a Code project stage product. */
export interface StageVersion {
  id: string
  project_id: string
  stage: string
  version_number: number
  is_current: boolean
  source: StageVersionSource
  summary: string | null
  run_id: string | null
  step_id: string | null
  note: string | null
  created_at: string
  // Present only on the single-version detail endpoint.
  content_text?: string | null
  content_json?: unknown
}

/** The character range in the (post-revision) document that the model changed. */
export interface SectionChange {
  start: number
  end: number
}

interface Envelope<T> {
  data: T
  message?: string
}

export const codeApi = {
  fetchStyles: async () => {
    const response = await api.get<Envelope<{ styles: UIStyle[] }>>("/code/styles")
    return response.data.styles
  },
  fetchPromptPrefixes: async (includeText = false) => {
    const response = await api.get<
      Envelope<{
        prefixes: PromptPrefix[]
        recipes: Record<string, string[]>
        recipe_examples: Record<string, { prefixes: string[]; tasks: string[] }>
        assembly_guide: string
      }>
    >(`/code/prompt-prefixes${includeText ? "?include_text=true" : ""}`)
    return response.data
  },
  fetchPromptPrefix: async (prefixId: string) => {
    const response = await api.get<Envelope<{ prefix: PromptPrefix }>>(
      `/code/prompt-prefixes/${prefixId}`
    )
    return response.data.prefix
  },
  routePromptPrefixes: async (task: string) => {
    const response = await api.post<Envelope<{ route: PromptRoute }>>(
      "/code/prompt-prefixes/route",
      { task }
    )
    return response.data.route
  },
  composePromptPrefixes: async (
    primaryRole: string,
    secondaryRoles: string[] = [],
    includeBase = true,
    includeOutputContract = true
  ) => {
    const response = await api.post<Envelope<{ system_prompt: string }>>(
      "/code/prompt-prefixes/compose",
      {
        primary_role: primaryRole,
        secondary_roles: secondaryRoles,
        include_base: includeBase,
        include_output_contract: includeOutputContract,
      }
    )
    return response.data.system_prompt
  },
  listProjects: async (limit = 50, offset = 0) => {
    const response = await api.get<Envelope<{ projects: CodeProject[]; has_more: boolean }>>(
      `/code/projects?limit=${limit}&offset=${offset}`
    )
    return response.data
  },
  createProject: async (requirement: string) => {
    const response = await api.post<Envelope<{ project: CodeProject }>>(
      "/code/projects",
      { requirement },
      { timeout: AI_GENERATION_TIMEOUT }
    )
    return response.data.project
  },
  getProject: async (projectId: string) => {
    const response = await api.get<Envelope<{ project: CodeProject }>>(
      `/code/projects/${projectId}`
    )
    return response.data.project
  },
  updateProject: async (projectId: string, data: Partial<CodeProject>) => {
    const response = await api.patch<Envelope<{ project: CodeProject }>>(
      `/code/projects/${projectId}`,
      data
    )
    return response.data.project
  },
  generateFlow: async (projectId: string) => {
    const response = await api.post<Envelope<{ project: CodeProject }>>(
      `/code/projects/${projectId}/flow`,
      undefined,
      { timeout: AI_GENERATION_TIMEOUT }
    )
    return response.data.project
  },
  splitDocuments: async (projectId: string) => {
    const response = await api.post<Envelope<{ project: CodeProject }>>(
      `/code/projects/${projectId}/documents`,
      undefined,
      { timeout: AI_GENERATION_TIMEOUT }
    )
    return response.data.project
  },
  updateDocument: async (
    projectId: string,
    documentId: string,
    data: Partial<CodeDocument>
  ) => {
    const response = await api.patch<Envelope<{ document: CodeDocument }>>(
      `/code/projects/${projectId}/documents/${documentId}`,
      data
    )
    return response.data.document
  },
  generateStylePrompt: async (projectId: string, styleIds: string[]) => {
    const response = await api.post<Envelope<{ project: CodeProject }>>(
      `/code/projects/${projectId}/style-prompt`,
      { style_ids: styleIds },
      { timeout: AI_GENERATION_TIMEOUT }
    )
    return response.data.project
  },
  generatePreviews: async (projectId: string, prompt?: string) => {
    const response = await api.post<
      Envelope<{ project: CodeProject; preview_skipped?: boolean }>
    >(
      `/code/projects/${projectId}/previews`,
      { prompt, count: 2 },
      { timeout: AI_GENERATION_TIMEOUT }
    )
    return {
      project: response.data.project,
      previewSkipped: response.data.preview_skipped ?? false,
    }
  },
  confirmPreview: async (
    projectId: string,
    previewUrl: string,
    uiBaselinePrompt?: string | null
  ) => {
    const response = await api.post<Envelope<{ project: CodeProject }>>(
      `/code/projects/${projectId}/confirm-preview`,
      { preview_url: previewUrl, ui_baseline_prompt: uiBaselinePrompt }
    )
    return response.data.project
  },
  // Toggle whether the built frontend preview at /preview/<id>/ is reachable on
  // the public internet (no login). Owner-only; only the rendered site is exposed.
  setPreviewVisibility: async (projectId: string, isPublic: boolean) => {
    const response = await api.post<
      Envelope<{ visibility: string; public: boolean; preview_path: string }>
    >(`/code/projects/${projectId}/preview-visibility`, { public: isPublic })
    return response.data
  },
  // --- inline section (partial) revision ---
  // Rewrite only the user-selected span of a confirmed document. The model is
  // fed the whole document as context but returns just the replacement, which the
  // backend splices at the selection offsets — returning the updated project (or
  // document) plus the exact changed range to highlight. Runs an LLM call, so use
  // the long timeout; the UI keeps this off the global loading gate (async).
  reviseStageSection: async (
    projectId: string,
    stage: "requirements" | "flow" | "style",
    selectedText: string,
    instruction: string,
    selectionStart: number,
    selectionEnd: number
  ) => {
    const response = await api.post<
      Envelope<{ project: CodeProject; version: StageVersion | null; change: SectionChange | null }>
    >(
      `/code/projects/${projectId}/stages/${stage}/revise-section`,
      {
        selected_text: selectedText,
        instruction,
        selection_start: selectionStart,
        selection_end: selectionEnd,
      },
      { timeout: AI_GENERATION_TIMEOUT }
    )
    return { project: response.data.project, change: response.data.change }
  },
  reviseDocumentSection: async (
    projectId: string,
    documentId: string,
    selectedText: string,
    instruction: string,
    selectionStart: number,
    selectionEnd: number
  ) => {
    const response = await api.post<
      Envelope<{ document: CodeDocument; version: StageVersion | null; change: SectionChange | null }>
    >(
      `/code/projects/${projectId}/documents/${documentId}/revise-section`,
      {
        selected_text: selectedText,
        instruction,
        selection_start: selectionStart,
        selection_end: selectionEnd,
      },
      { timeout: AI_GENERATION_TIMEOUT }
    )
    return { document: response.data.document, change: response.data.change }
  },
  // --- stage version history ---
  listStageVersions: async (projectId: string, stage: string) => {
    const response = await api.get<Envelope<{ versions: StageVersion[] }>>(
      `/code/projects/${projectId}/stages/${stage}/versions`
    )
    return response.data.versions
  },
  getStageVersion: async (projectId: string, stage: string, versionId: string) => {
    const response = await api.get<Envelope<{ version: StageVersion }>>(
      `/code/projects/${projectId}/stages/${stage}/versions/${versionId}`
    )
    return response.data.version
  },
  activateStageVersion: async (projectId: string, stage: string, versionId: string) => {
    const response = await api.post<Envelope<{ project: CodeProject }>>(
      `/code/projects/${projectId}/stages/${stage}/versions/${versionId}/activate`
    )
    return response.data.project
  },
}
