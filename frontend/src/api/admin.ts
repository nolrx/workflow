import { api } from "@/api/client"

/** One prompt in the admin list (no full content — just a preview). */
export interface PromptSummary {
  key: string
  scope: string
  name: string
  description: string
  category: string
  is_overridden: boolean
  updated_at: string | null
  updated_by: string | null
  has_default: boolean
  preview: string
}

/** Full prompt detail, including current content and the bundled default. */
export interface PromptDetail {
  key: string
  scope: string
  name: string
  description: string
  category: string
  is_overridden: boolean
  updated_at: string | null
  updated_by: string | null
  has_default: boolean
  content: string
  default_content: string | null
}

interface Envelope<T> {
  success: boolean
  data: T
  message?: string
}

/** Prompt keys contain "/" (e.g. code/requirements_prompt.txt). Encode each
 *  segment but keep the slashes so the Flask <path:key> route matches. */
function keyPath(key: string): string {
  return key.split("/").map(encodeURIComponent).join("/")
}

export const adminApi = {
  listPrompts: async (scope?: string) => {
    const response = await api.get<
      Envelope<{ prompts: PromptSummary[]; mongo_available: boolean }>
    >("/admin/prompts", { params: scope ? { scope } : undefined })
    return response.data
  },

  getPrompt: async (key: string) => {
    const response = await api.get<
      Envelope<{ prompt: PromptDetail; mongo_available: boolean }>
    >(`/admin/prompts/${keyPath(key)}`)
    return response.data
  },

  updatePrompt: async (key: string, content: string) => {
    const response = await api.put<Envelope<{ prompt: PromptDetail }>>(
      `/admin/prompts/${keyPath(key)}`,
      { content }
    )
    return response.data.prompt
  },

  resetPrompt: async (key: string) => {
    const response = await api.post<Envelope<{ prompt: PromptDetail }>>(
      `/admin/prompts/${keyPath(key)}/reset`
    )
    return response.data.prompt
  },
}
