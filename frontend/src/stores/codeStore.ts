import { create } from "zustand"
import {
  codeApi,
  type CodeDocument,
  type CodeProject,
  type UIStyle,
} from "@/api/code"
import { t } from "@/i18n"

type CodeAction =
  | "create"
  | "saveProject"
  | "flow"
  | "documents"
  | "saveDocument"
  | "style"
  | "preview"
  | "confirm"

interface CodeState {
  project: CodeProject | null
  projects: CodeProject[]
  hasMoreProjects: boolean
  styles: UIStyle[]
  selectedStyleIds: string[]
  isLoading: boolean
  activeAction: CodeAction | null
  error: string | null
  fetchStyles: () => Promise<void>
  createProject: (requirement: string) => Promise<void>
  loadProject: (projectId: string) => Promise<void>
  fetchProjects: (limit?: number, offset?: number) => Promise<void>
  setCurrentProject: (project: CodeProject | null) => void
  updateProject: (data: Partial<CodeProject>) => Promise<void>
  updateProjectDraft: (data: Partial<CodeProject>) => void
  generateFlow: () => Promise<void>
  splitDocuments: () => Promise<void>
  updateDocument: (documentId: string, data: Partial<CodeDocument>) => Promise<void>
  updateDocumentDraft: (documentId: string, data: Partial<CodeDocument>) => void
  toggleStyle: (styleId: string) => void
  generateStylePrompt: () => Promise<void>
  generatePreviews: () => Promise<void>
  confirmPreview: (previewUrl: string) => Promise<void>
  clearError: () => void
}

function getErrorMessage(error: unknown, fallback: string) {
  return (
    (error as { response?: { data?: { message?: string } } })?.response?.data?.message ||
    fallback
  )
}

export const useCodeStore = create<CodeState>()((set, get) => ({
  project: null,
  projects: [],
  hasMoreProjects: false,
  styles: [],
  selectedStyleIds: [],
  isLoading: false,
  activeAction: null,
  error: null,

  fetchStyles: async () => {
    try {
      const styles = await codeApi.fetchStyles()
      set({ styles })
    } catch (error) {
      set({ error: getErrorMessage(error, t("errors:code.fetchStylesFailed")) })
    }
  },

  createProject: async (requirement) => {
    set({ isLoading: true, activeAction: "create", error: null })
    try {
      const project = await codeApi.createProject(requirement)
      set({
        project,
        selectedStyleIds: project.selected_style_ids,
        isLoading: false,
        activeAction: null,
      })
    } catch (error) {
      set({
        error: getErrorMessage(error, t("errors:code.createProjectFailed")),
        isLoading: false,
        activeAction: null,
      })
    }
  },

  loadProject: async (projectId) => {
    set({ isLoading: true, error: null })
    try {
      const project = await codeApi.getProject(projectId)
      set({
        project,
        selectedStyleIds: project.selected_style_ids,
        isLoading: false,
      })
    } catch (error) {
      set({
        error: getErrorMessage(error, t("errors:code.createProjectFailed")),
        isLoading: false,
      })
    }
  },

  // Session list for the sidebar/history. Non-critical: failures are swallowed
  // (the list just stays empty) so they never block the workspace.
  fetchProjects: async (limit = 50, offset = 0) => {
    try {
      const { projects, has_more } = await codeApi.listProjects(limit, offset)
      set((state) => ({
        projects: offset === 0 ? projects : [...state.projects, ...projects],
        hasMoreProjects: has_more,
      }))
    } catch {
      // ignore — sidebar/history list fetch is non-critical
    }
  },

  setCurrentProject: (project) =>
    set({ project, selectedStyleIds: project?.selected_style_ids ?? [] }),

  updateProject: async (data) => {
    const project = get().project
    if (!project) return
    set({ isLoading: true, activeAction: "saveProject", error: null })
    try {
      const updated = await codeApi.updateProject(project.id, data)
      set({
        project: updated,
        selectedStyleIds: updated.selected_style_ids,
        isLoading: false,
        activeAction: null,
      })
    } catch (error) {
      set({
        error: getErrorMessage(error, t("errors:code.saveProjectFailed")),
        isLoading: false,
        activeAction: null,
      })
    }
  },

  updateProjectDraft: (data) => {
    const project = get().project
    if (!project) return
    set({ project: { ...project, ...data } })
  },

  generateFlow: async () => {
    const project = get().project
    if (!project) return
    set({ isLoading: true, activeAction: "flow", error: null })
    try {
      const updated = await codeApi.generateFlow(project.id)
      set({ project: updated, isLoading: false, activeAction: null })
    } catch (error) {
      set({
        error: getErrorMessage(error, t("errors:code.generateFlowFailed")),
        isLoading: false,
        activeAction: null,
      })
    }
  },

  splitDocuments: async () => {
    const project = get().project
    if (!project) return
    set({ isLoading: true, activeAction: "documents", error: null })
    try {
      const updated = await codeApi.splitDocuments(project.id)
      set({ project: updated, isLoading: false, activeAction: null })
    } catch (error) {
      set({
        error: getErrorMessage(error, t("errors:code.splitDocumentsFailed")),
        isLoading: false,
        activeAction: null,
      })
    }
  },

  updateDocument: async (documentId, data) => {
    const project = get().project
    if (!project) return
    set({ isLoading: true, activeAction: "saveDocument", error: null })
    try {
      const updatedDocument = await codeApi.updateDocument(project.id, documentId, data)
      set({
        project: {
          ...project,
          documents: project.documents.map((document) =>
            document.id === documentId ? updatedDocument : document
          ),
        },
        isLoading: false,
        activeAction: null,
      })
    } catch (error) {
      set({
        error: getErrorMessage(error, t("errors:code.saveDocumentFailed")),
        isLoading: false,
        activeAction: null,
      })
    }
  },

  updateDocumentDraft: (documentId, data) => {
    const project = get().project
    if (!project) return
    set({
      project: {
        ...project,
        documents: project.documents.map((document) =>
          document.id === documentId ? { ...document, ...data } : document
        ),
      },
    })
  },

  toggleStyle: (styleId) => {
    const selectedStyleIds = get().selectedStyleIds
    set({
      selectedStyleIds: selectedStyleIds.includes(styleId)
        ? selectedStyleIds.filter((id) => id !== styleId)
        : [...selectedStyleIds, styleId],
    })
  },

  generateStylePrompt: async () => {
    const project = get().project
    if (!project) return
    set({ isLoading: true, activeAction: "style", error: null })
    try {
      const updated = await codeApi.generateStylePrompt(project.id, get().selectedStyleIds)
      set({
        project: updated,
        selectedStyleIds: updated.selected_style_ids,
        isLoading: false,
        activeAction: null,
      })
    } catch (error) {
      set({
        error: getErrorMessage(error, t("errors:code.generateStyleFailed")),
        isLoading: false,
        activeAction: null,
      })
    }
  },

  generatePreviews: async () => {
    const project = get().project
    if (!project) return
    set({ isLoading: true, activeAction: "preview", error: null })
    try {
      const updated = await codeApi.generatePreviews(project.id, project.style_prompt || undefined)
      set({ project: updated, isLoading: false, activeAction: null })
    } catch (error) {
      set({
        error: getErrorMessage(error, t("errors:code.generatePreviewFailed")),
        isLoading: false,
        activeAction: null,
      })
    }
  },

  confirmPreview: async (previewUrl) => {
    const project = get().project
    if (!project) return
    set({ isLoading: true, activeAction: "confirm", error: null })
    try {
      const updated = await codeApi.confirmPreview(
        project.id,
        previewUrl,
        project.style_prompt
      )
      set({ project: updated, isLoading: false, activeAction: null })
    } catch (error) {
      set({
        error: getErrorMessage(error, t("errors:code.confirmPreviewFailed")),
        isLoading: false,
        activeAction: null,
      })
    }
  },

  clearError: () => set({ error: null }),
}))
