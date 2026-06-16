/**
 * RedBook Store
 * Manages RedBook task state, images, and generation progress
 */
import { create } from "zustand"
import { api, apiClient } from "@/api/client"
import { t } from "@/i18n"
import { useCreditStore } from "@/stores/creditStore"

// Types
export interface RedBookPage {
  index: number
  type: "封面" | "内容" | "总结"
  content: string
}

export interface RedBookImage {
  id: string
  task_id: string
  order_index: number
  filename: string
  thumbnail_path: string | null
  status: "pending" | "generating" | "completed" | "error"
  error_message: string | null
  generated_at: string | null
}

export interface RedBookTask {
  id: string
  user_id: string
  team_id: string | null
  title: string
  topic: string | null
  outline_raw: string | null
  outline_parsed: RedBookPage[] | null
  image_task_id: string | null
  thumbnail_path: string | null
  content_titles: string[] | null
  content_copywriting: string | null
  content_tags: string[] | null
  status: "draft" | "generating" | "partial" | "completed" | "error"
  visibility: string
  created_at: string
  updated_at: string
  images?: RedBookImage[]
}

export interface GenerateProgress {
  total: number
  completed: number
  failed: number
  currentIndex: number | null
}

export interface TaskStats {
  total: number
  completed: number
  generating: number
  error: number
  draft: number
}

interface RedBookState {
  tasks: RedBookTask[]
  currentTask: RedBookTask | null
  isLoading: boolean
  error: string | null
  hasMore: boolean
  stats: TaskStats | null

  // Generation state
  isGenerating: boolean
  generateProgress: GenerateProgress | null
  generatedImages: Map<number, string>

  // Actions
  fetchTasks: (limit?: number, offset?: number) => Promise<void>
  fetchTask: (taskId: string) => Promise<void>
  createTask: (title: string, topic?: string) => Promise<RedBookTask>
  updateTask: (taskId: string, data: Partial<RedBookTask>) => Promise<RedBookTask>
  deleteTask: (taskId: string) => Promise<void>
  setCurrentTask: (task: RedBookTask | null) => void
  fetchStats: () => Promise<void>

  // Outline actions
  generateOutline: (
    topic: string,
    images?: File[]
  ) => Promise<{ outline: string; pages: RedBookPage[] }>

  // Image actions
  generateImages: (
    taskId: string,
    pages: RedBookPage[],
    fullOutline?: string,
    userImages?: File[],
    userTopic?: string
  ) => Promise<void>
  retryImage: (
    taskId: string,
    imageTaskId: string,
    page: RedBookPage
  ) => Promise<{ success: boolean; image_url?: string; error?: string }>
  getImageUrl: (imageTaskId: string, filename: string) => string

  // Content actions
  generateContent: (
    topic: string,
    outline: string
  ) => Promise<{
    titles: string[]
    copywriting: string
    tags: string[]
  }>

  clearError: () => void
  resetGeneration: () => void
}

export const useRedBookStore = create<RedBookState>()((set, get) => ({
  tasks: [],
  currentTask: null,
  isLoading: false,
  error: null,
  hasMore: false,
  stats: null,

  isGenerating: false,
  generateProgress: null,
  generatedImages: new Map(),

  fetchTasks: async (limit = 20, offset = 0) => {
    set({ isLoading: true, error: null })
    try {
      const response = await api.get<{
        data: {
          tasks: RedBookTask[]
          has_more: boolean
          limit: number
          offset: number
        }
      }>(`/redbook/tasks?limit=${limit}&offset=${offset}`)
      set({
        tasks:
          offset === 0
            ? response.data.tasks
            : [...get().tasks, ...response.data.tasks],
        hasMore: response.data.has_more,
        isLoading: false,
      })
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:redbook.fetchTasksFailed')
      set({ error: message, isLoading: false })
    }
  },

  fetchTask: async (taskId) => {
    set({ isLoading: true, error: null })
    try {
      const response = await api.get<{ data: RedBookTask }>(
        `/redbook/tasks/${taskId}`
      )
      set({ currentTask: response.data, isLoading: false })
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:redbook.fetchTaskFailed')
      set({ error: message, isLoading: false })
    }
  },

  createTask: async (title, topic) => {
    set({ isLoading: true, error: null })
    try {
      const response = await api.post<{ data: RedBookTask }>("/redbook/tasks", {
        title,
        topic,
      })
      const newTask = response.data
      set((state) => ({
        tasks: [newTask, ...state.tasks],
        currentTask: newTask,
        isLoading: false,
      }))
      return newTask
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:redbook.createTaskFailed')
      set({ error: message, isLoading: false })
      throw error
    }
  },

  updateTask: async (taskId, data) => {
    set({ isLoading: true, error: null })
    try {
      const response = await api.put<{ data: RedBookTask }>(
        `/redbook/tasks/${taskId}`,
        data
      )
      set((state) => ({
        tasks: state.tasks.map((t) =>
          t.id === taskId ? response.data : t
        ),
        currentTask:
          state.currentTask?.id === taskId
            ? response.data
            : state.currentTask,
        isLoading: false,
      }))
      return response.data
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:redbook.updateTaskFailed')
      set({ error: message, isLoading: false })
      throw error
    }
  },

  deleteTask: async (taskId) => {
    set({ isLoading: true, error: null })
    try {
      await api.delete(`/redbook/tasks/${taskId}`)
      set((state) => ({
        tasks: state.tasks.filter((t) => t.id !== taskId),
        currentTask:
          state.currentTask?.id === taskId ? null : state.currentTask,
        isLoading: false,
      }))
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response
          ?.data?.message || t('errors:redbook.deleteTaskFailed')
      set({ error: message, isLoading: false })
      throw error
    }
  },

  setCurrentTask: (task) => {
    set({ currentTask: task })
  },

  fetchStats: async () => {
    try {
      const response = await api.get<{ data: TaskStats }>("/redbook/tasks/stats")
      set({ stats: response.data })
    } catch (error) {
      console.error("Failed to fetch stats:", error)
    }
  },

  generateOutline: async (topic, images) => {
    set({ isLoading: true, error: null })
    try {
      let response
      if (images && images.length > 0) {
        // Use FormData for file upload
        const formData = new FormData()
        formData.append("topic", topic)
        images.forEach((img) => {
          formData.append("images", img)
        })
        response = await apiClient.post<{
          data: { outline: string; pages: RedBookPage[] }
        }>("/redbook/outline", formData, {
          headers: { "Content-Type": "multipart/form-data" },
        })
        response = response.data
      } else {
        response = await api.post<{
          data: { outline: string; pages: RedBookPage[] }
        }>("/redbook/outline", { topic })
      }
      // 大纲生成消耗了积分,刷新余额显示
      void useCreditStore.getState().refreshBalance()
      set({ isLoading: false })
      return response.data
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response?.data
          ?.message || t('errors:redbook.generateOutlineFailed')
      set({ error: message, isLoading: false })
      throw new Error(message)
    }
  },

  generateImages: async (taskId, pages, fullOutline, userImages, userTopic) => {
    set({
      isGenerating: true,
      generateProgress: { total: pages.length, completed: 0, failed: 0, currentIndex: 0 },
      generatedImages: new Map(),
      error: null,
    })

    try {
      // Prepare request data
      const requestData: Record<string, unknown> = {
        task_id: taskId,
        pages,
        full_outline: fullOutline || "",
        user_topic: userTopic || "",
      }

      // Convert user images to base64 if provided
      if (userImages && userImages.length > 0) {
        const imagesBase64: string[] = []
        for (const file of userImages) {
          const base64 = await fileToBase64(file)
          imagesBase64.push(base64)
        }
        requestData.user_images = imagesBase64
      }

      // Use SSE for real-time progress
      const response = await fetch("/api/redbook/generate", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("access_token")}`,
        },
        body: JSON.stringify(requestData),
      })

      if (!response.ok) {
        throw new Error(t('errors:redbook.generateRequestFailed'))
      }

      const reader = response.body?.getReader()
      if (!reader) {
        throw new Error(t('errors:redbook.responseStreamFailed'))
      }

      const decoder = new TextDecoder()
      let buffer = ""

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split("\n")
        buffer = lines.pop() || ""

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            try {
              const data = JSON.parse(line.slice(6))

              if (data.event === "image") {
                const imageData = data.data
                set((state) => {
                  const newImages = new Map(state.generatedImages)
                  newImages.set(imageData.index, imageData.image_url)
                  return {
                    generatedImages: newImages,
                    generateProgress: state.generateProgress
                      ? {
                          ...state.generateProgress,
                          completed: state.generateProgress.completed + 1,
                          currentIndex: imageData.index,
                        }
                      : null,
                  }
                })
              } else if (data.event === "error") {
                set((state) => ({
                  generateProgress: state.generateProgress
                    ? {
                        ...state.generateProgress,
                        failed: state.generateProgress.failed + 1,
                        currentIndex: data.data.index,
                      }
                    : null,
                }))
              } else if (data.event === "complete") {
                set({
                  isGenerating: false,
                  generateProgress: {
                    total: data.data.total,
                    completed: data.data.completed,
                    failed: data.data.failed,
                    currentIndex: null,
                  },
                })
                // 图片按成功逐张扣费,生成结束后刷新余额
                void useCreditStore.getState().refreshBalance()
              }
            } catch {
              console.error("Failed to parse SSE data:", line)
            }
          }
        }
      }
    } catch (error: unknown) {
      const message =
        error instanceof Error ? error.message : t('errors:redbook.generateImagesFailed')
      set({ error: message, isGenerating: false })
      throw error
    }
  },

  retryImage: async (taskId, _imageTaskId, page) => {
    try {
      const response = await api.post<{
        data: { success: boolean; image_url?: string; error?: string }
      }>("/redbook/retry", {
        task_id: taskId,
        page,
      })

      if (response.data.success && response.data.image_url) {
        set((state) => {
          const newImages = new Map(state.generatedImages)
          newImages.set(page.index, response.data.image_url!)
          return { generatedImages: newImages }
        })
        // 重试成功也会消耗一次图片积分,刷新余额
        void useCreditStore.getState().refreshBalance()
      }

      return response.data
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response?.data
          ?.message || t('errors:redbook.retryFailed')
      return { success: false, error: message }
    }
  },

  getImageUrl: (imageTaskId, filename) => {
    return `/api/redbook/files/${imageTaskId}/${filename}`
  },

  generateContent: async (topic, outline) => {
    set({ isLoading: true, error: null })
    try {
      const response = await api.post<{
        data: { titles: string[]; copywriting: string; tags: string[] }
      }>("/redbook/content", { topic, outline })
      // 文案生成消耗了积分,刷新余额显示
      void useCreditStore.getState().refreshBalance()
      set({ isLoading: false })
      return response.data
    } catch (error: unknown) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response?.data
          ?.message || t('errors:redbook.generateContentFailed')
      set({ error: message, isLoading: false })
      throw new Error(message)
    }
  },

  clearError: () => set({ error: null }),

  resetGeneration: () =>
    set({
      isGenerating: false,
      generateProgress: null,
      generatedImages: new Map(),
    }),
}))

// Helper function to convert File to base64
async function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.readAsDataURL(file)
    reader.onload = () => {
      const result = reader.result as string
      // Remove data URL prefix (e.g., "data:image/png;base64,")
      const base64 = result.split(",")[1]
      resolve(base64)
    }
    reader.onerror = (error) => reject(error)
  })
}
