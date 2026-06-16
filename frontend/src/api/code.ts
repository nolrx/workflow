import { api } from "@/api/client"

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
    const response = await api.post<Envelope<{ project: CodeProject }>>("/code/projects", {
      requirement,
    })
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
      `/code/projects/${projectId}/flow`
    )
    return response.data.project
  },
  splitDocuments: async (projectId: string) => {
    const response = await api.post<Envelope<{ project: CodeProject }>>(
      `/code/projects/${projectId}/documents`
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
      { style_ids: styleIds }
    )
    return response.data.project
  },
  generatePreviews: async (projectId: string, prompt?: string) => {
    const response = await api.post<Envelope<{ project: CodeProject }>>(
      `/code/projects/${projectId}/previews`,
      { prompt, count: 2 }
    )
    return response.data.project
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
}
