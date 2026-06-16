/**
 * Export Tasks Store
 * Manages async export task tracking for PPT exports
 */
import { create } from "zustand"
import { persist } from "zustand/middleware"
import { api } from "@/api/client"

export type ExportFormat = "pptx" | "pdf" | "editable-pptx"

export interface ExportTask {
  id: string
  taskId: string
  projectId: string
  projectName?: string
  format: ExportFormat
  status: "pending" | "processing" | "completed" | "failed"
  progress?: {
    total: number
    completed: number
    current_step?: string
  }
  downloadUrl?: string
  filename?: string
  warnings?: string[]
  errorMessage?: string
  createdAt: string
  completedAt?: string
}

interface ExportTasksState {
  tasks: ExportTask[]
  isPolling: boolean

  // Actions
  addTask: (task: Omit<ExportTask, "id" | "createdAt">) => string
  updateTask: (id: string, updates: Partial<ExportTask>) => void
  removeTask: (id: string) => void
  clearCompletedTasks: () => void
  clearAllTasks: () => void

  // Polling
  pollTask: (projectId: string, taskId: string) => Promise<void>
  pollAllPendingTasks: () => Promise<void>

  // Getters
  getPendingTasks: () => ExportTask[]
  getTasksByProject: (projectId: string) => ExportTask[]
}

export const useExportTasksStore = create<ExportTasksState>()(
  persist(
    (set, get) => ({
      tasks: [],
      isPolling: false,

      addTask: (task) => {
        const id = `export-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`
        const newTask: ExportTask = {
          ...task,
          id,
          createdAt: new Date().toISOString(),
        }
        set((state) => ({
          tasks: [newTask, ...state.tasks],
        }))
        return id
      },

      updateTask: (id, updates) => {
        set((state) => ({
          tasks: state.tasks.map((task) =>
            task.id === id ? { ...task, ...updates } : task
          ),
        }))
      },

      removeTask: (id) => {
        set((state) => ({
          tasks: state.tasks.filter((task) => task.id !== id),
        }))
      },

      clearCompletedTasks: () => {
        set((state) => ({
          tasks: state.tasks.filter(
            (task) => task.status !== "completed" && task.status !== "failed"
          ),
        }))
      },

      clearAllTasks: () => {
        set({ tasks: [] })
      },

      pollTask: async (projectId, taskId) => {
        const { updateTask, tasks } = get()
        const task = tasks.find((t) => t.taskId === taskId)
        if (!task) return

        try {
          const response = await api.get<{
            data: {
              id: string
              status: string
              progress?: {
                total?: number
                completed?: number
                current_step?: string
              }
              error_message?: string
              result?: {
                download_url?: string
                filename?: string
                warnings?: string[]
              }
            }
          }>(`/ppt/projects/${projectId}/tasks/${taskId}`)

          const taskData = response.data

          if (taskData.status === "COMPLETED") {
            updateTask(task.id, {
              status: "completed",
              downloadUrl: taskData.result?.download_url,
              filename: taskData.result?.filename,
              warnings: taskData.result?.warnings,
              completedAt: new Date().toISOString(),
            })
          } else if (taskData.status === "FAILED") {
            updateTask(task.id, {
              status: "failed",
              errorMessage: taskData.error_message || "Export failed",
              completedAt: new Date().toISOString(),
            })
          } else {
            updateTask(task.id, {
              status: taskData.status === "PROCESSING" ? "processing" : "pending",
              progress: taskData.progress
                ? {
                    total: taskData.progress.total || 0,
                    completed: taskData.progress.completed || 0,
                    current_step: taskData.progress.current_step,
                  }
                : undefined,
            })

            // Continue polling if not completed
            setTimeout(() => {
              get().pollTask(projectId, taskId)
            }, 2000)
          }
        } catch (error) {
          console.error("Failed to poll export task:", error)
          updateTask(task.id, {
            status: "failed",
            errorMessage: "Failed to check task status",
            completedAt: new Date().toISOString(),
          })
        }
      },

      pollAllPendingTasks: async () => {
        const { tasks, pollTask, isPolling } = get()
        if (isPolling) return

        set({ isPolling: true })

        const pendingTasks = tasks.filter(
          (t) => t.status === "pending" || t.status === "processing"
        )

        await Promise.all(
          pendingTasks.map((task) => pollTask(task.projectId, task.taskId))
        )

        set({ isPolling: false })
      },

      getPendingTasks: () => {
        return get().tasks.filter(
          (t) => t.status === "pending" || t.status === "processing"
        )
      },

      getTasksByProject: (projectId) => {
        return get().tasks.filter((t) => t.projectId === projectId)
      },
    }),
    {
      name: "ppt-export-tasks",
      partialize: (state) => ({
        // Only persist tasks from the last 24 hours
        tasks: state.tasks.filter((task) => {
          const createdAt = new Date(task.createdAt).getTime()
          const now = Date.now()
          return now - createdAt < 24 * 60 * 60 * 1000
        }),
      }),
    }
  )
)
