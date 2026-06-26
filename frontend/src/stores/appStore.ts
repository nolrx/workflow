/**
 * App Space (应用空间) store — deployed-app list, app detail, and 二次开发 iterations.
 *
 * Data-ops only (list / detail / iteration CRUD). Run-driven progress is polled
 * by the pages via {@link useAppStore.refreshIteration}; the deploy step reuses
 * the full-stack deploy API (linked to the iteration via iteration_id).
 */
import { create } from "zustand"
import {
  appsApi,
  type AppDetail,
  type AppIteration,
  type AppListItem,
  type AppListParams,
  type ConfirmIterationBody,
  type CreateIterationBody,
} from "@/api/apps"

/** Guards against a slow detail fetch for an old project clobbering a newer one. */
let detailLoadSeq = 0
/** Generation guard for the list: a stale fetch/loadMore must not write a newer
 *  filter's list (else loadMore appends an old page onto fresh results → dupes). */
let listLoadSeq = 0

interface AppState {
  // --- list ---
  apps: AppListItem[]
  total: number
  isLoadingApps: boolean
  isLoadingMore: boolean
  filters: AppListParams

  // --- detail ---
  detail: AppDetail | null
  isLoadingDetail: boolean
  iterations: AppIteration[]
  selectedIterationId: string | null

  isSubmitting: boolean
  error: string | null

  fetchApps: (params?: AppListParams) => Promise<void>
  loadMore: () => Promise<void>
  setFilters: (filters: AppListParams) => void
  fetchApp: (projectId: string) => Promise<void>
  selectIteration: (iterationId: string | null) => void
  createIteration: (
    projectId: string,
    body: CreateIterationBody
  ) => Promise<AppIteration | null>
  confirmIteration: (
    projectId: string,
    iterationId: string,
    body?: ConfirmIterationBody
  ) => Promise<boolean>
  refreshIteration: (
    projectId: string,
    iterationId: string
  ) => Promise<AppIteration | null>
  reset: () => void
}

/** Merge an updated iteration into the in-memory list (insert if new). */
function upsertIteration(list: AppIteration[], next: AppIteration): AppIteration[] {
  const idx = list.findIndex((it) => it.id === next.id)
  if (idx === -1) return [next, ...list]
  const copy = list.slice()
  copy[idx] = next
  return copy
}

export const useAppStore = create<AppState>()((set, get) => ({
  apps: [],
  total: 0,
  isLoadingApps: false,
  isLoadingMore: false,
  filters: {},

  detail: null,
  isLoadingDetail: false,
  iterations: [],
  selectedIterationId: null,

  isSubmitting: false,
  error: null,

  fetchApps: async (params) => {
    const seq = ++listLoadSeq
    const filters = { ...get().filters, ...(params ?? {}) }
    set({ isLoadingApps: true, error: null, filters })
    try {
      const result = await appsApi.list({ ...filters, offset: 0 })
      if (seq !== listLoadSeq) return // a newer fetch superseded this one
      set({ apps: result.apps, total: result.total, isLoadingApps: false })
    } catch (err) {
      if (seq !== listLoadSeq) return
      set({
        isLoadingApps: false,
        error: err instanceof Error ? err.message : "加载应用列表失败",
      })
    }
  },

  /** Append the next page (pagination). No-op when everything is already loaded. */
  loadMore: async () => {
    const { apps, total, filters, isLoadingMore } = get()
    if (isLoadingMore || apps.length >= total) return
    const seq = listLoadSeq // the list generation this page belongs to
    set({ isLoadingMore: true })
    try {
      const result = await appsApi.list({ ...filters, offset: apps.length })
      // A fetchApps (filter change) bumped the generation while we were loading →
      // this page belongs to a stale list; drop it instead of appending dupes.
      if (seq !== listLoadSeq) {
        set({ isLoadingMore: false })
        return
      }
      set((state) => ({
        apps: [...state.apps, ...result.apps],
        total: result.total,
        isLoadingMore: false,
      }))
    } catch {
      set({ isLoadingMore: false })
    }
  },

  setFilters: (filters) => set({ filters }),

  fetchApp: async (projectId) => {
    const seq = ++detailLoadSeq
    set({ isLoadingDetail: true, error: null })
    try {
      const detail = await appsApi.get(projectId)
      // Drop a stale response if the user navigated to another app meanwhile.
      if (seq !== detailLoadSeq) return
      set({
        detail,
        iterations: detail.iterations,
        isLoadingDetail: false,
      })
    } catch (err) {
      if (seq !== detailLoadSeq) return
      set({
        isLoadingDetail: false,
        error: err instanceof Error ? err.message : "加载应用详情失败",
      })
    }
  },

  selectIteration: (iterationId) => set({ selectedIterationId: iterationId }),

  createIteration: async (projectId, body) => {
    set({ isSubmitting: true, error: null })
    try {
      const { iteration } = await appsApi.createIteration(projectId, body)
      set((state) => ({
        isSubmitting: false,
        iterations: upsertIteration(state.iterations, iteration),
        selectedIterationId: iteration.id,
      }))
      return iteration
    } catch (err) {
      set({
        isSubmitting: false,
        error: err instanceof Error ? err.message : "创建二次开发失败",
      })
      return null
    }
  },

  confirmIteration: async (projectId, iterationId, body) => {
    set({ isSubmitting: true, error: null })
    try {
      const { iteration } = await appsApi.confirmIteration(projectId, iterationId, body)
      set((state) => ({
        isSubmitting: false,
        iterations: upsertIteration(state.iterations, iteration),
      }))
      return true
    } catch (err) {
      set({
        isSubmitting: false,
        error: err instanceof Error ? err.message : "确认执行计划失败",
      })
      return false
    }
  },

  refreshIteration: async (projectId, iterationId) => {
    try {
      const iteration = await appsApi.getIteration(projectId, iterationId)
      set((state) => ({ iterations: upsertIteration(state.iterations, iteration) }))
      return iteration
    } catch {
      return null
    }
  },

  reset: () =>
    set({
      detail: null,
      iterations: [],
      selectedIterationId: null,
      isLoadingDetail: false,
      isSubmitting: false,
      error: null,
    }),
}))
