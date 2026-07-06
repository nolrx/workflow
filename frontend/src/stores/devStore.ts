/**
 * Dev Mode session + checklist store.
 *
 * Holds the interactive dev SESSION (long-running container) and its persistent
 * checklist board. Turn STREAMING is delegated to agentStore (openRun on the
 * returned run_id) — this store only owns session lifecycle + the board, and
 * absorbs live CHECKLIST_UPDATED events pushed through the run's SSE stream.
 */
import { create } from "zustand"

import {
  devApi,
  type DevBoard,
  type DevPlanTask,
  type DevSession,
  type DevSprint,
  type DevTaskPlan,
} from "@/api/dev"

interface DevState {
  projectId: string | null
  session: DevSession | null
  board: DevBoard | null
  /** The run id to attach agentStore to (bootstrap run, then each turn). */
  currentRunId: string | null
  starting: boolean
  error: string | null

  /** The live sprint (serial task scheduler) attached to the frontend session. */
  sprint: DevSprint | null
  sprintBusy: boolean

  /** The active backlog-planner draft (P1) awaiting user confirmation. */
  taskPlan: DevTaskPlan | null
  plannerBusy: boolean

  // Backend-lane dev session (full-stack loop): a separate long-running backend
  // dev container the live frontend talks to over /preview/<pid>/api.
  backendSession: DevSession | null
  backendBoard: DevBoard | null
  backendStarting: boolean

  start: (projectId: string) => Promise<string | null>
  refresh: () => Promise<void>
  sendTurn: (instruction: string) => Promise<string | null>
  sendParallelTurn: (instructions: string[]) => Promise<string | null>
  stop: () => Promise<void>
  addTask: (title: string) => Promise<void>
  setTaskStatus: (taskId: string, status: string) => Promise<void>
  /** Absorb a board pushed by a CHECKLIST_UPDATED SSE event. */
  applyBoard: (board: DevBoard) => void
  setCurrentRun: (runId: string | null) => void
  reset: () => void

  // Sprint scheduler actions (serial, P0).
  startSprint: (maxTurns?: number) => Promise<void>
  pauseSprint: () => Promise<void>
  resumeSprint: () => Promise<void>
  cancelSprint: () => Promise<void>
  refreshSprint: () => Promise<void>
  /** Absorb a sprint snapshot pushed through the sprint run's SSE stream. */
  applySprint: (sprint: DevSprint) => void

  // Backlog planner (P1).
  startTaskPlanner: (opts?: { instruction?: string; maxTasks?: number }) => Promise<void>
  refreshTaskPlan: () => Promise<void>
  loadLatestTaskPlan: () => Promise<void>
  editTaskPlan: (patch: { tasks?: DevPlanTask[]; summary?: string }) => Promise<void>
  applyTaskPlan: (opts?: { replace?: boolean; force?: boolean }) => Promise<boolean>
  rejectTaskPlan: () => Promise<void>

  // Backend lane actions.
  startBackend: () => Promise<string | null>
  refreshBackend: () => Promise<void>
  sendBackendTurn: (instruction: string) => Promise<string | null>
  runTests: () => Promise<string | null>
  stopBackend: () => Promise<void>
  applyBackendBoard: (board: DevBoard) => void
}

export const useDevStore = create<DevState>((set, get) => ({
  projectId: null,
  session: null,
  board: null,
  currentRunId: null,
  starting: false,
  error: null,
  sprint: null,
  sprintBusy: false,
  taskPlan: null,
  plannerBusy: false,
  backendSession: null,
  backendBoard: null,
  backendStarting: false,

  start: async (projectId) => {
    set({ starting: true, error: null, projectId })
    try {
      const view = await devApi.startSession(projectId)
      set({
        session: view.session,
        board: view.board,
        sprint: view.sprint ?? null,
        currentRunId: view.run_id ?? view.latest_run_id ?? null,
        starting: false,
      })
      return view.run_id ?? view.latest_run_id ?? null
    } catch (e) {
      const msg = e instanceof Error ? e.message : "开发会话启动失败"
      set({ starting: false, error: msg })
      return null
    }
  },

  refresh: async () => {
    const { projectId, session } = get()
    if (!projectId || !session) return
    try {
      const view = await devApi.getSession(projectId, session.id)
      set({ session: view.session, board: view.board, sprint: view.sprint ?? get().sprint })
    } catch {
      /* transient — keep current view */
    }
  },

  sendTurn: async (instruction) => {
    const { projectId, session } = get()
    if (!projectId || !session) return null
    try {
      const { run_id } = await devApi.startTurn(projectId, session.id, instruction)
      set({ currentRunId: run_id })
      return run_id
    } catch (e) {
      set({ error: e instanceof Error ? e.message : "发送失败" })
      return null
    }
  },

  sendParallelTurn: async (instructions) => {
    const { projectId, session } = get()
    if (!projectId || !session) return null
    const lanes = instructions.map((s) => s.trim()).filter(Boolean)
    if (lanes.length === 0) return null
    try {
      const { run_id } = await devApi.startParallelTurn(projectId, session.id, lanes)
      set({ currentRunId: run_id })
      return run_id
    } catch (e) {
      set({ error: e instanceof Error ? e.message : "并行开发启动失败" })
      return null
    }
  },

  stop: async () => {
    const { projectId, session } = get()
    if (!projectId || !session) return
    try {
      const { session: updated } = await devApi.stopSession(projectId, session.id)
      set({ session: updated })
    } catch {
      /* ignore — best effort */
    }
  },

  addTask: async (title) => {
    const { projectId, session } = get()
    if (!projectId || !session || !title.trim()) return
    const { board } = await devApi.addTask(projectId, session.id, { title: title.trim() })
    set({ board })
  },

  setTaskStatus: async (taskId, status) => {
    const { projectId } = get()
    if (!projectId) return
    const { board } = await devApi.updateTask(projectId, taskId, {
      status: status as never,
    })
    set({ board })
  },

  applyBoard: (board) => set({ board }),

  setCurrentRun: (runId) => set({ currentRunId: runId }),

  // --- sprint scheduler --------------------------------------------------------
  startSprint: async (maxTurns) => {
    const { projectId, session } = get()
    if (!projectId || !session) return
    set({ sprintBusy: true, error: null })
    try {
      const { sprint } = await devApi.createSprint(projectId, session.id, { maxTurns })
      set({ sprint, sprintBusy: false })
    } catch (e) {
      set({ sprintBusy: false, error: e instanceof Error ? e.message : "Sprint 启动失败" })
    }
  },

  pauseSprint: async () => {
    const { projectId, session, sprint } = get()
    if (!projectId || !session || !sprint) return
    set({ sprintBusy: true })
    try {
      const res = await devApi.pauseSprint(projectId, session.id, sprint.id)
      set({ sprint: res.sprint, sprintBusy: false })
    } catch (e) {
      set({ sprintBusy: false, error: e instanceof Error ? e.message : "暂停失败" })
    }
  },

  resumeSprint: async () => {
    const { projectId, session, sprint } = get()
    if (!projectId || !session || !sprint) return
    set({ sprintBusy: true })
    try {
      const res = await devApi.resumeSprint(projectId, session.id, sprint.id)
      set({ sprint: res.sprint, sprintBusy: false })
    } catch (e) {
      set({ sprintBusy: false, error: e instanceof Error ? e.message : "恢复失败" })
    }
  },

  cancelSprint: async () => {
    const { projectId, session, sprint } = get()
    if (!projectId || !session || !sprint) return
    set({ sprintBusy: true })
    try {
      const res = await devApi.cancelSprint(projectId, session.id, sprint.id)
      set({ sprint: res.sprint, sprintBusy: false })
      // The cancel released claimed tasks — pull the fresh board.
      void get().refresh()
    } catch (e) {
      set({ sprintBusy: false, error: e instanceof Error ? e.message : "取消失败" })
    }
  },

  refreshSprint: async () => {
    const { projectId, session, sprint } = get()
    if (!projectId || !session || !sprint) return
    try {
      const res = await devApi.getSprint(projectId, session.id, sprint.id)
      set({ sprint: res.sprint, board: res.board })
    } catch {
      /* transient */
    }
  },

  applySprint: (sprint) => set({ sprint }),

  // --- backlog planner (P1) ---------------------------------------------------
  startTaskPlanner: async (opts) => {
    const { projectId, session } = get()
    if (!projectId || !session) return
    set({ plannerBusy: true, error: null })
    try {
      const { plan } = await devApi.createTaskPlan(projectId, session.id, {
        instruction: opts?.instruction,
        maxTasks: opts?.maxTasks,
      })
      set({ taskPlan: plan, plannerBusy: false })
    } catch (e) {
      set({ plannerBusy: false, error: e instanceof Error ? e.message : "任务规划启动失败" })
    }
  },

  refreshTaskPlan: async () => {
    const { projectId, session, taskPlan } = get()
    if (!projectId || !session || !taskPlan) return
    try {
      const { plan } = await devApi.getTaskPlan(projectId, session.id, taskPlan.id)
      set({ taskPlan: plan })
    } catch {
      /* transient */
    }
  },

  loadLatestTaskPlan: async () => {
    const { projectId, session } = get()
    if (!projectId || !session) return
    try {
      const plans = await devApi.listTaskPlans(projectId, session.id)
      const live = plans.find((p) => p.status === "planning" || p.status === "draft" || p.status === "stale")
      if (live) {
        const { plan } = await devApi.getTaskPlan(projectId, session.id, live.id)
        set({ taskPlan: plan })
      }
    } catch {
      /* transient */
    }
  },

  editTaskPlan: async (patch) => {
    const { projectId, session, taskPlan } = get()
    if (!projectId || !session || !taskPlan) return
    set({ plannerBusy: true })
    try {
      const { plan } = await devApi.updateTaskPlan(projectId, session.id, taskPlan.id, patch)
      set({ taskPlan: plan, plannerBusy: false })
    } catch (e) {
      set({ plannerBusy: false, error: e instanceof Error ? e.message : "草案保存失败" })
    }
  },

  applyTaskPlan: async (opts) => {
    const { projectId, session, taskPlan } = get()
    if (!projectId || !session || !taskPlan) return false
    set({ plannerBusy: true, error: null })
    try {
      const res = await devApi.applyTaskPlan(projectId, session.id, taskPlan.id, opts)
      set({ taskPlan: res.plan, board: res.board, plannerBusy: false })
      return true
    } catch (e) {
      // 409 = plan stale — surface it and refresh so the panel shows the state.
      set({ plannerBusy: false, error: e instanceof Error ? e.message : "应用失败" })
      void get().refreshTaskPlan()
      return false
    }
  },

  rejectTaskPlan: async () => {
    const { projectId, session, taskPlan } = get()
    if (!projectId || !session || !taskPlan) return
    set({ plannerBusy: true })
    try {
      const { plan } = await devApi.rejectTaskPlan(projectId, session.id, taskPlan.id)
      set({ taskPlan: plan, plannerBusy: false })
    } catch {
      set({ plannerBusy: false })
    }
  },

  // --- backend lane ----------------------------------------------------------
  startBackend: async () => {
    const { projectId } = get()
    if (!projectId) return null
    set({ backendStarting: true, error: null })
    try {
      const view = await devApi.startBackendSession(projectId)
      set({
        backendSession: view.session,
        backendBoard: view.board,
        backendStarting: false,
        currentRunId: view.run_id ?? view.latest_run_id ?? get().currentRunId,
      })
      return view.run_id ?? view.latest_run_id ?? null
    } catch (e) {
      set({ backendStarting: false, error: e instanceof Error ? e.message : "后端开发会话启动失败" })
      return null
    }
  },

  refreshBackend: async () => {
    const { projectId, backendSession } = get()
    if (!projectId || !backendSession) return
    try {
      const view = await devApi.getSession(projectId, backendSession.id)
      set({ backendSession: view.session, backendBoard: view.board })
    } catch {
      /* transient */
    }
  },

  sendBackendTurn: async (instruction) => {
    const { projectId, backendSession } = get()
    if (!projectId || !backendSession) return null
    try {
      // The turns endpoint picks the workflow by the session's lane.
      const { run_id } = await devApi.startTurn(projectId, backendSession.id, instruction)
      set({ currentRunId: run_id })
      return run_id
    } catch (e) {
      set({ error: e instanceof Error ? e.message : "发送失败" })
      return null
    }
  },

  runTests: async () => {
    const { projectId, backendSession } = get()
    if (!projectId || !backendSession) return null
    try {
      const { run_id } = await devApi.runTests(projectId, backendSession.id)
      set({ currentRunId: run_id })
      return run_id
    } catch (e) {
      set({ error: e instanceof Error ? e.message : "运行测试失败" })
      return null
    }
  },

  stopBackend: async () => {
    const { projectId, backendSession } = get()
    if (!projectId || !backendSession) return
    try {
      const { session: updated } = await devApi.stopSession(projectId, backendSession.id)
      set({ backendSession: updated })
    } catch {
      /* best effort */
    }
  },

  applyBackendBoard: (board) => set({ backendBoard: board }),

  reset: () =>
    set({
      projectId: null,
      session: null,
      board: null,
      currentRunId: null,
      starting: false,
      error: null,
      sprint: null,
      sprintBusy: false,
      taskPlan: null,
      plannerBusy: false,
      backendSession: null,
      backendBoard: null,
      backendStarting: false,
    }),
}))
