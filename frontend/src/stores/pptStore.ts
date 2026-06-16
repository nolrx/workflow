/**
 * PPT Store
 * Manages PPT project state, pages, and generation tasks
 */
import { create } from "zustand"
import { api } from "@/api/client"
import { t } from "@/i18n"
import { useCreditStore } from "@/stores/creditStore"
import type {
  ExportFormat,
  ExportMode,
  ExportResult,
  ImageVersion,
} from "@/api/ppt"

// Types
export interface PPTPage {
  id: string
  page_id?: string  // Backend uses page_id in to_dict
  project_id: string
  order_index: number
  part: string | null
  outline_content: {
    title?: string
    points?: string[]
  } | null
  description_content: {
    text?: string
    text_content?: string[]
  } | null
  generated_image_url: string | null  // Backend returns generated_image_url
  generated_image_path?: string | null  // Keep for compatibility
  status: string
  created_at: string
  updated_at: string
}

// Helper function to normalize page data from backend
// Backend returns page_id, frontend expects id
function normalizePage(page: Record<string, unknown>): PPTPage {
  return {
    ...page,
    id: (page.page_id as string) || (page.id as string),
  } as PPTPage
}

function normalizePages(pages: Record<string, unknown>[] | undefined): PPTPage[] | undefined {
  if (!pages) return undefined
  return pages.map(normalizePage)
}

function normalizeProject(project: Record<string, unknown>): PPTProject {
  return {
    ...project,
    pages: normalizePages(project.pages as Record<string, unknown>[] | undefined),
  } as PPTProject
}

export interface PPTProject {
  id: string
  user_id: string
  team_id: string | null
  creation_type: "idea" | "outline" | "descriptions"
  idea_prompt: string | null
  outline_text: string | null
  description_text: string | null
  extra_requirements: string | null
  template_image_path: string | null
  template_style: string | null
  export_extractor_method: string | null
  export_inpaint_method: string | null
  visibility: string
  status: string
  created_at: string
  updated_at: string
  pages?: PPTPage[]
}

export interface PPTTask {
  id: string
  project_id: string
  task_type: string
  status: "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED"
  progress: {
    total?: number
    completed?: number
    failed?: number
    current_step?: string
    percent?: number
  } | null
  error_message: string | null
  created_at: string
  completed_at: string | null
}

export interface CreateProjectRequest {
  creation_type: "idea" | "outline" | "descriptions"
  idea_prompt?: string
  outline_text?: string
  description_text?: string
  template_style?: string
  team_id?: string
}

interface PPTState {
  projects: PPTProject[]
  currentProject: PPTProject | null
  currentTask: PPTTask | null
  isLoading: boolean
  isGenerating: boolean
  activeTaskId: string | null
  taskProgress: { total: number; completed: number; failed: number } | null
  generationStep: string | null
  error: string | null
  hasMore: boolean

  // Page selection for batch operations
  selectedPageIds: Set<string>

  // Actions
  fetchProjects: (limit?: number, offset?: number) => Promise<void>
  fetchProject: (projectId: string) => Promise<void>
  createProject: (data: CreateProjectRequest) => Promise<PPTProject>
  updateProject: (
    projectId: string,
    data: Partial<PPTProject>
  ) => Promise<PPTProject>
  deleteProject: (projectId: string) => Promise<void>
  setCurrentProject: (project: PPTProject | null) => void

  // Page actions
  updatePage: (
    projectId: string,
    pageId: string,
    data: Partial<PPTPage>
  ) => Promise<PPTPage>
  deletePage: (projectId: string, pageId: string) => Promise<void>
  addPage: (projectId: string, afterIndex?: number) => Promise<PPTPage>
  reorderPages: (projectId: string, pageIds: string[]) => Promise<void>

  // Page selection actions
  selectPage: (pageId: string) => void
  deselectPage: (pageId: string) => void
  togglePageSelection: (pageId: string) => void
  selectAllPages: () => void
  deselectAllPages: () => void
  getSelectedPages: () => PPTPage[]

  // Task actions
  fetchTaskStatus: (projectId: string, taskId: string) => Promise<PPTTask>
  setCurrentTask: (task: PPTTask | null) => void

  // Generation actions
  generateOutline: (projectId: string, language?: string) => Promise<void>
  generateDescriptions: (projectId: string, language?: string) => Promise<string>
  generateImages: (projectId: string, language?: string, pageIds?: string[]) => Promise<string>
  regeneratePageImage: (projectId: string, pageId: string) => Promise<string>
  regeneratePageDescription: (projectId: string, pageId: string) => Promise<void>
  pollTaskStatus: (projectId: string, taskId: string) => Promise<void>
  generateAll: (projectId: string, language?: string) => Promise<void>
  generateSelectedImages: (projectId: string, language?: string) => Promise<string>

  // Image editing actions
  editPageImage: (
    projectId: string,
    pageId: string,
    instruction: string,
    options?: {
      selectionMask?: string
      materialImages?: string[]
    }
  ) => Promise<{ page: PPTPage; new_version: ImageVersion }>
  isEditing: boolean

  // Export actions
  exportProject: (
    projectId: string,
    options?: {
      format?: ExportFormat
      mode?: ExportMode
      filename?: string
    }
  ) => Promise<ExportResult>
  exportProjectAsync: (projectId: string, format?: string) => Promise<{ task_id: string }>
  isExporting: boolean

  clearError: () => void
  clearGenerationState: () => void
}

export const usePPTStore = create<PPTState>()((set, get) => ({
  projects: [],
  currentProject: null,
  currentTask: null,
  isLoading: false,
  isGenerating: false,
  isExporting: false,
  isEditing: false,
  activeTaskId: null,
  taskProgress: null,
  generationStep: null,
  error: null,
  hasMore: false,
  selectedPageIds: new Set<string>(),

  fetchProjects: async (limit = 50, offset = 0) => {
    set({ isLoading: true, error: null })
    try {
      const response = await api.get<{
        data: {
          projects: Record<string, unknown>[]
          has_more: boolean
          limit: number
          offset: number
        }
      }>(`/ppt/projects?limit=${limit}&offset=${offset}`)
      const normalizedProjects = response.data.projects.map(normalizeProject)
      set({
        projects:
          offset === 0
            ? normalizedProjects
            : [...get().projects, ...normalizedProjects],
        hasMore: response.data.has_more,
        isLoading: false,
      })
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:ppt.fetchProjectsFailed')
      set({ error: message, isLoading: false })
    }
  },

  fetchProject: async (projectId) => {
    set({ isLoading: true, error: null })
    try {
      const response = await api.get<{ data: Record<string, unknown> }>(
        `/ppt/projects/${projectId}`
      )
      set({ currentProject: normalizeProject(response.data), isLoading: false })
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:ppt.fetchProjectFailed')
      set({ error: message, isLoading: false })
    }
  },

  createProject: async (data) => {
    set({ isLoading: true, error: null })
    try {
      const response = await api.post<{
        data: { project_id: string; status: string; pages: Record<string, unknown>[] }
      }>("/ppt/projects", data)
      const newProject: PPTProject = {
        id: response.data.project_id,
        user_id: "",
        team_id: data.team_id || null,
        creation_type: data.creation_type,
        idea_prompt: data.idea_prompt || null,
        outline_text: data.outline_text || null,
        description_text: data.description_text || null,
        extra_requirements: null,
        template_image_path: null,
        template_style: data.template_style || null,
        export_extractor_method: null,
        export_inpaint_method: null,
        visibility: "private",
        status: response.data.status,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        pages: normalizePages(response.data.pages),
      }
      set((state) => ({
        projects: [newProject, ...state.projects],
        currentProject: newProject,
        isLoading: false,
      }))
      return newProject
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:ppt.createProjectFailed')
      set({ error: message, isLoading: false })
      throw error
    }
  },

  updateProject: async (projectId, data) => {
    set({ isLoading: true, error: null })
    try {
      const response = await api.put<{ data: Record<string, unknown> }>(
        `/ppt/projects/${projectId}`,
        data
      )
      const normalized = normalizeProject(response.data)
      set((state) => ({
        projects: state.projects.map((p) =>
          p.id === projectId ? normalized : p
        ),
        currentProject:
          state.currentProject?.id === projectId
            ? normalized
            : state.currentProject,
        isLoading: false,
      }))
      return normalized
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:ppt.updateProjectFailed')
      set({ error: message, isLoading: false })
      throw error
    }
  },

  deleteProject: async (projectId) => {
    set({ isLoading: true, error: null })
    try {
      await api.delete(`/ppt/projects/${projectId}`)
      set((state) => ({
        projects: state.projects.filter((p) => p.id !== projectId),
        currentProject:
          state.currentProject?.id === projectId ? null : state.currentProject,
        isLoading: false,
      }))
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:ppt.deleteProjectFailed')
      set({ error: message, isLoading: false })
      throw error
    }
  },

  setCurrentProject: (project) => {
    set({ currentProject: project })
  },

  updatePage: async (projectId, pageId, data) => {
    set({ isLoading: true, error: null })
    try {
      const response = await api.put<{ data: Record<string, unknown> }>(
        `/ppt/projects/${projectId}/pages/${pageId}`,
        data
      )
      const normalized = normalizePage(response.data)
      set((state) => ({
        currentProject: state.currentProject
          ? {
              ...state.currentProject,
              pages: state.currentProject.pages?.map((p) =>
                p.id === pageId ? normalized : p
              ),
            }
          : null,
        isLoading: false,
      }))
      return normalized
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:ppt.updatePageFailed')
      set({ error: message, isLoading: false })
      throw error
    }
  },

  deletePage: async (projectId, pageId) => {
    set({ isLoading: true, error: null })
    try {
      await api.delete(`/ppt/projects/${projectId}/pages/${pageId}`)
      set((state) => ({
        currentProject: state.currentProject
          ? {
              ...state.currentProject,
              pages: state.currentProject.pages?.filter((p) => p.id !== pageId),
            }
          : null,
        selectedPageIds: (() => {
          const newSet = new Set(state.selectedPageIds)
          newSet.delete(pageId)
          return newSet
        })(),
        isLoading: false,
      }))
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:ppt.deletePageFailed')
      set({ error: message, isLoading: false })
      throw error
    }
  },

  addPage: async (projectId, afterIndex) => {
    set({ isLoading: true, error: null })
    try {
      const response = await api.post<{ data: Record<string, unknown> }>(
        `/ppt/projects/${projectId}/pages`,
        { after_index: afterIndex }
      )
      const normalized = normalizePage(response.data)
      set((state) => {
        const pages = state.currentProject?.pages || []
        const newPages = [...pages]
        const insertIndex = afterIndex !== undefined ? afterIndex + 1 : newPages.length
        newPages.splice(insertIndex, 0, normalized)
        return {
          currentProject: state.currentProject
            ? { ...state.currentProject, pages: newPages }
            : null,
          isLoading: false,
        }
      })
      return normalized
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:ppt.addPageFailed')
      set({ error: message, isLoading: false })
      throw error
    }
  },

  reorderPages: async (projectId, pageIds) => {
    set({ isLoading: true, error: null })
    try {
      const response = await api.post<{
        data: {
          pages: Record<string, unknown>[]
        }
      }>(`/ppt/projects/${projectId}/pages/reorder`, {
        page_ids: pageIds,
      })
      // Normalize and update current project with reordered pages
      const normalizedPages = normalizePages(response.data.pages) || []
      set((state) => ({
        currentProject: state.currentProject
          ? {
              ...state.currentProject,
              pages: normalizedPages.map((p, idx) => ({
                ...state.currentProject!.pages?.find((op) => op.id === p.id),
                ...p,
                order_index: idx,
              })),
            }
          : null,
        isLoading: false,
      }))
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:ppt.reorderPagesFailed')
      set({ error: message, isLoading: false })
      throw error
    }
  },

  // Page selection actions
  selectPage: (pageId) => {
    set((state) => ({
      selectedPageIds: new Set(state.selectedPageIds).add(pageId),
    }))
  },

  deselectPage: (pageId) => {
    set((state) => {
      const newSet = new Set(state.selectedPageIds)
      newSet.delete(pageId)
      return { selectedPageIds: newSet }
    })
  },

  togglePageSelection: (pageId) => {
    set((state) => {
      const newSet = new Set(state.selectedPageIds)
      if (newSet.has(pageId)) {
        newSet.delete(pageId)
      } else {
        newSet.add(pageId)
      }
      return { selectedPageIds: newSet }
    })
  },

  selectAllPages: () => {
    set((state) => {
      const allPageIds = state.currentProject?.pages?.map((p) => p.id) || []
      return { selectedPageIds: new Set(allPageIds) }
    })
  },

  deselectAllPages: () => {
    set({ selectedPageIds: new Set() })
  },

  getSelectedPages: () => {
    const state = get()
    return (
      state.currentProject?.pages?.filter((p) =>
        state.selectedPageIds.has(p.id)
      ) || []
    )
  },

  fetchTaskStatus: async (projectId, taskId) => {
    try {
      const response = await api.get<{ data: PPTTask }>(
        `/ppt/projects/${projectId}/tasks/${taskId}`
      )
      set({ currentTask: response.data })
      return response.data
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:ppt.fetchTaskStatusFailed')
      set({ error: message })
      throw error
    }
  },

  setCurrentTask: (task) => {
    set({ currentTask: task })
  },

  // Generation actions
  generateOutline: async (projectId, language = "zh") => {
    set({ isGenerating: true, generationStep: "outline", error: null })
    try {
      const response = await api.post<{
        data: { project_id: string; status: string; pages: Record<string, unknown>[] }
      }>(`/ppt/projects/${projectId}/generate/outline`, { language })

      // Update current project with new pages
      set((state) => ({
        currentProject: state.currentProject
          ? {
              ...state.currentProject,
              status: response.data.status,
              pages: normalizePages(response.data.pages),
            }
          : null,
        isGenerating: false,
        generationStep: null,
      }))
      // Outline consumed credits — refresh the displayed balance.
      void useCreditStore.getState().refreshBalance()
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t("errors:ppt.generateOutlineFailed")
      set({ error: message, isGenerating: false, generationStep: null })
      throw error
    }
  },

  generateDescriptions: async (projectId, language = "zh") => {
    set({ isGenerating: true, generationStep: "descriptions", error: null })
    try {
      const response = await api.post<{
        data: { task_id: string; status: string }
      }>(`/ppt/projects/${projectId}/generate/descriptions`, { language })

      set({ activeTaskId: response.data.task_id })
      return response.data.task_id
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t("errors:ppt.generateDescriptionsFailed")
      set({ error: message, isGenerating: false, generationStep: null })
      throw error
    }
  },

  generateImages: async (projectId, language = "zh", pageIds) => {
    set({ isGenerating: true, generationStep: "images", error: null })
    try {
      const response = await api.post<{
        data: { task_id: string; status: string }
      }>(`/ppt/projects/${projectId}/generate/images`, { language, page_ids: pageIds })

      set({ activeTaskId: response.data.task_id })
      return response.data.task_id
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t("errors:ppt.generateImagesFailed")
      set({ error: message, isGenerating: false, generationStep: null })
      throw error
    }
  },

  pollTaskStatus: async (projectId, taskId) => {
    const poll = async (): Promise<void> => {
      try {
        const task = await get().fetchTaskStatus(projectId, taskId)

        // Update progress
        if (task.progress) {
          set({
            taskProgress: {
              total: task.progress.total || 0,
              completed: task.progress.completed || 0,
              failed: task.progress.failed || 0,
            },
          })
        }

        if (task.status === "COMPLETED") {
          // Refresh project data
          await get().fetchProject(projectId)
          // Descriptions/images consumed credits per page — refresh the balance.
          void useCreditStore.getState().refreshBalance()
          set({
            isGenerating: false,
            activeTaskId: null,
            generationStep: null,
            taskProgress: null,
          })
          return
        }

        if (task.status === "FAILED") {
          set({
            error: task.error_message || "Task failed",
            isGenerating: false,
            activeTaskId: null,
            generationStep: null,
            taskProgress: null,
          })
          return
        }

        // Continue polling if PENDING or PROCESSING
        if (task.status === "PENDING" || task.status === "PROCESSING") {
          await new Promise((resolve) => setTimeout(resolve, 2000))
          return poll()
        }
      } catch {
        set({
          error: "Failed to check task status",
          isGenerating: false,
          activeTaskId: null,
          generationStep: null,
          taskProgress: null,
        })
      }
    }

    await poll()
  },

  generateAll: async (projectId, language = "zh") => {
    const { generateOutline, generateDescriptions, generateImages, pollTaskStatus } = get()

    try {
      // Step 1: Generate outline (synchronous)
      await generateOutline(projectId, language)

      // Step 2: Generate descriptions (async + poll)
      const descTaskId = await generateDescriptions(projectId, language)
      await pollTaskStatus(projectId, descTaskId)

      // Step 3: Generate images (async + poll)
      const imageTaskId = await generateImages(projectId, language)
      await pollTaskStatus(projectId, imageTaskId)
    } catch (error) {
      // Error already set in individual functions
      console.error("generateAll failed:", error)
    }
  },

  generateSelectedImages: async (projectId, language = "zh") => {
    const selectedPageIds = Array.from(get().selectedPageIds)
    if (selectedPageIds.length === 0) {
      set({ error: "No pages selected" })
      throw new Error("No pages selected")
    }
    return get().generateImages(projectId, language, selectedPageIds)
  },

  regeneratePageImage: async (projectId, pageId) => {
    set({ isGenerating: true, generationStep: "image", error: null })
    try {
      const response = await api.post<{
        data: { task_id: string; status: string }
      }>(`/ppt/projects/${projectId}/pages/${pageId}/regenerate-image`)

      set({ activeTaskId: response.data.task_id })
      return response.data.task_id
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t("errors:ppt.regenerateImageFailed")
      set({ error: message, isGenerating: false, generationStep: null })
      throw error
    }
  },

  regeneratePageDescription: async (projectId, pageId) => {
    set({ isGenerating: true, generationStep: "description", error: null })
    try {
      const response = await api.post<{
        data: Record<string, unknown>
      }>(`/ppt/projects/${projectId}/pages/${pageId}/regenerate-description`)

      const normalized = normalizePage(response.data)
      // Update page in current project
      set((state) => ({
        currentProject: state.currentProject
          ? {
              ...state.currentProject,
              pages: state.currentProject.pages?.map((p) =>
                p.id === pageId ? normalized : p
              ),
            }
          : null,
        isGenerating: false,
        generationStep: null,
      }))
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t("errors:ppt.regenerateDescriptionFailed")
      set({ error: message, isGenerating: false, generationStep: null })
      throw error
    }
  },

  editPageImage: async (projectId, pageId, instruction, options) => {
    set({ isEditing: true, error: null })
    try {
      const response = await api.post<{
        data: {
          page: {
            id: string
            generated_image_url: string
            status: string
          }
          new_version: ImageVersion
        }
      }>(`/ppt/projects/${projectId}/pages/${pageId}/edit-image`, {
        instruction,
        selection_mask: options?.selectionMask,
        material_images: options?.materialImages,
      })

      // Update page in current project
      set((state) => ({
        currentProject: state.currentProject
          ? {
              ...state.currentProject,
              pages: state.currentProject.pages?.map((p) =>
                p.id === pageId
                  ? {
                      ...p,
                      generated_image_url: response.data.page.generated_image_url,
                      status: response.data.page.status,
                    }
                  : p
              ),
            }
          : null,
        isEditing: false,
      }))

      // Return page and new version info
      const updatedPage = get().currentProject?.pages?.find(
        (p) => p.id === pageId
      )
      return {
        page: updatedPage as PPTPage,
        new_version: response.data.new_version,
      }
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t("errors:ppt.editImageFailed")
      set({ error: message, isEditing: false })
      throw error
    }
  },

  exportProject: async (projectId, options = {}) => {
    set({ isExporting: true, error: null })
    try {
      const { format = "pptx", mode = "simple", filename } = options
      const response = await api.post<{
        data: ExportResult
      }>(`/ppt/projects/${projectId}/export`, {
        format,
        mode,
        filename,
      })

      set({ isExporting: false })
      return response.data
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t("errors:ppt.exportFailed")
      set({ error: message, isExporting: false })
      throw error
    }
  },

  exportProjectAsync: async (projectId, format = "pptx") => {
    set({ isExporting: true, error: null })
    try {
      const response = await api.post<{
        data: { task_id: string; status: string }
      }>(`/ppt/projects/${projectId}/export/async`, { format })

      set({ isExporting: false })
      return { task_id: response.data.task_id }
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t("errors:ppt.exportFailed")
      set({ error: message, isExporting: false })
      throw error
    }
  },

  clearError: () => set({ error: null }),

  clearGenerationState: () =>
    set({
      isGenerating: false,
      activeTaskId: null,
      taskProgress: null,
      generationStep: null,
    }),
}))
