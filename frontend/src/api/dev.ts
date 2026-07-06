/**
 * Dev Mode (交互式开发模式) API client.
 *
 * A dev SESSION owns a long-running dev container (Vite dev server + HMR) and a
 * persistent functional checklist; each TURN is a bounded agent run (started here,
 * streamed via the shared /api/agent/runs/<id>/stream). This module only covers
 * session lifecycle + the checklist board — turn streaming reuses agentStore.
 */
import { api } from "@/api/client"

interface Envelope<T> {
  success: boolean
  data: T
  message?: string
}

export type DevSessionStatus = "starting" | "running" | "repairing" | "stopped" | "failed"
// Full task state machine (sprint scheduler): queued/verifying etc. are
// scheduler-owned; the user may only set pending/in_progress/done/skipped.
export type DevTaskStatus =
  | "pending"
  | "queued"
  | "in_progress"
  | "verifying"
  | "done"
  | "blocked"
  | "failed"
  | "skipped"
  | "cancelled"

export type DevSprintStatus =
  | "planned"
  | "running"
  | "pausing"
  | "paused"
  | "completed"
  | "blocked"
  | "failed"
  | "cancelled"

export interface DevSession {
  id: string
  project_id: string
  lane: string
  status: DevSessionStatus
  health: string | null
  restart_count: number
  container_name: string | null
  internal_port: number | null
  preview_path: string | null
  base_source_run_id: string | null
  error_message: string | null
  created_at: string | null
  updated_at: string | null
  last_active_at: string | null
  stopped_at: string | null
}

export interface DevResourceOutput {
  path: string
  size?: string
  prompt?: string
  required?: boolean
  used_by?: string[]
}

export interface DevResourceSpec {
  skill?: string
  style_brief?: string
  outputs?: DevResourceOutput[]
  verified_outputs?: { path: string; exists: boolean; bytes: number; required: boolean }[]
  fallback_allowed?: boolean
}

export interface DevTask {
  id: string
  project_id: string
  session_id: string
  feature_id: string | null
  parent_feature_id: string | null
  lane: string
  category: string
  title: string
  description: string | null
  status: DevTaskStatus
  source: string
  origin_turn_run_id: string | null
  last_attempt_run_id: string | null
  note: string | null
  blocked_reason: string | null
  order_index: number
  priority: number
  retry_count: number
  max_retries: number | null
  acceptance_criteria: string[]
  depends_on: string[]
  resource_spec: DevResourceSpec
  plan_id?: string | null
  planner_meta?: Record<string, unknown>
  created_at: string | null
  updated_at: string | null
}

export type DevTaskPlanStatus =
  | "planning"
  | "draft"
  | "applying"
  | "applied"
  | "rejected"
  | "stale"
  | "failed"

export interface DevPlanTask {
  feature_id: string
  parent_feature_id?: string | null
  lane: string
  category: string
  title: string
  description?: string | null
  acceptance_criteria: string[]
  depends_on: string[]
  resource_spec?: DevResourceSpec
  priority?: number | null
  max_retries?: number | null
  planner_meta?: { risk?: string; estimated_turns?: number; files_hint?: string[] }
}

export interface DevTaskPlan {
  id: string
  project_id: string
  session_id: string
  run_id: string | null
  status: DevTaskPlanStatus
  mode: string
  target_lanes: string[]
  warnings: string[]
  error_message: string | null
  inserted_count: number | null
  updated_count: number | null
  skipped_count: number | null
  plan?: {
    version?: string
    summary?: string
    assumptions?: string[]
    tasks?: DevPlanTask[]
    warnings?: string[]
  }
  created_at: string | null
  updated_at: string | null
  applied_at: string | null
}

export interface DevSprint {
  id: string
  project_id: string
  session_id: string
  run_id: string | null
  lane: string
  status: DevSprintStatus
  mode: string
  max_turns: number | null
  turn_count: number
  stall_count: number
  progress_snapshot: {
    total?: number
    done?: number
    ready?: number
    unsettled?: number
    settled_ok?: number
    reason?: string
    counts?: Record<string, number>
  }
  current_task_ids: string[]
  created_by?: string
  created_at: string | null
  updated_at: string | null
  finished_at: string | null
}

export interface DevBulkTaskInput {
  title: string
  feature_id?: string
  parent_feature_id?: string
  lane?: string
  category?: string
  description?: string
  acceptance_criteria?: string[]
  depends_on?: string[]
  resource_spec?: Record<string, unknown>
  priority?: number
  max_retries?: number
}

export interface DevBoard {
  items: DevTask[]
  total: number
  done: number
  functional_total: number
  functional_done: number
}

export interface DevSessionView {
  session: DevSession
  board: DevBoard
  latest_run_id: string | null
  run_id?: string
  /** The live (non-terminal) sprint, if a scheduler is attached to this session. */
  sprint?: DevSprint | null
}

export interface DevLogs {
  available: boolean
  logs: string
  container: string | null
  lane?: string
}

export const devApi = {
  async startSession(projectId: string): Promise<DevSessionView> {
    const res = await api.post<Envelope<DevSessionView>>(
      `/code/projects/${projectId}/dev-sessions`,
    )
    return res.data
  },

  async startBackendSession(projectId: string): Promise<DevSessionView> {
    const res = await api.post<Envelope<DevSessionView>>(
      `/code/projects/${projectId}/dev-backend-sessions`,
    )
    return res.data
  },

  async runTests(projectId: string, sessionId: string): Promise<{ run_id: string }> {
    const res = await api.post<Envelope<{ run_id: string }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/run-tests`,
    )
    return res.data
  },

  async getSession(projectId: string, sessionId: string): Promise<DevSessionView> {
    const res = await api.get<Envelope<DevSessionView>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}`,
    )
    return res.data
  },

  async getChecklist(projectId: string, sessionId: string): Promise<DevBoard> {
    const res = await api.get<Envelope<{ board: DevBoard }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/checklist`,
    )
    return res.data.board
  },

  async getLogs(projectId: string, sessionId: string, tail = 500): Promise<DevLogs> {
    const res = await api.get<Envelope<DevLogs>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/logs?tail=${tail}`,
    )
    return res.data
  },

  async startTurn(
    projectId: string,
    sessionId: string,
    instruction: string,
  ): Promise<{ run_id: string }> {
    const res = await api.post<Envelope<{ run_id: string }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/turns`,
      { instruction },
    )
    return res.data
  },

  async startParallelTurn(
    projectId: string,
    sessionId: string,
    instructions: string[],
  ): Promise<{ run_id: string; lanes: number }> {
    const res = await api.post<Envelope<{ run_id: string; lanes: number }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/parallel-turns`,
      { lanes: instructions.map((instruction) => ({ instruction })) },
    )
    return res.data
  },

  async addTask(
    projectId: string,
    sessionId: string,
    payload: { title: string; category?: string; description?: string },
  ): Promise<{ task: DevTask; board: DevBoard }> {
    const res = await api.post<Envelope<{ task: DevTask; board: DevBoard }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/tasks`,
      payload,
    )
    return res.data
  },

  async updateTask(
    projectId: string,
    taskId: string,
    patch: Partial<Pick<DevTask, "status" | "title" | "description" | "note">>,
  ): Promise<{ task: DevTask; board: DevBoard }> {
    const res = await api.patch<Envelope<{ task: DevTask; board: DevBoard }>>(
      `/code/projects/${projectId}/dev-tasks/${taskId}`,
      patch,
    )
    return res.data
  },

  async stopSession(projectId: string, sessionId: string): Promise<{ session: DevSession }> {
    const res = await api.post<Envelope<{ session: DevSession }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/stop`,
    )
    return res.data
  },

  // --- sprint scheduler (serial, P0) ----------------------------------------
  async bulkTasks(
    projectId: string,
    sessionId: string,
    tasks: DevBulkTaskInput[],
    replace = false,
  ): Promise<{ inserted: number; updated: number; skipped: number; board: DevBoard }> {
    const res = await api.post<
      Envelope<{ inserted: number; updated: number; skipped: number; board: DevBoard }>
    >(`/code/projects/${projectId}/dev-sessions/${sessionId}/tasks/bulk`, { tasks, replace })
    return res.data
  },

  async createSprint(
    projectId: string,
    sessionId: string,
    opts?: { maxTurns?: number },
  ): Promise<{ sprint: DevSprint; run_id: string }> {
    const res = await api.post<Envelope<{ sprint: DevSprint; run_id: string }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/sprints`,
      { mode: "serial", max_turns: opts?.maxTurns },
    )
    return res.data
  },

  async getSprint(
    projectId: string,
    sessionId: string,
    sprintId: string,
  ): Promise<{ sprint: DevSprint; board: DevBoard; run_id: string | null }> {
    const res = await api.get<
      Envelope<{ sprint: DevSprint; board: DevBoard; run_id: string | null }>
    >(`/code/projects/${projectId}/dev-sessions/${sessionId}/sprints/${sprintId}`)
    return res.data
  },

  async pauseSprint(
    projectId: string,
    sessionId: string,
    sprintId: string,
  ): Promise<{ sprint: DevSprint }> {
    const res = await api.post<Envelope<{ sprint: DevSprint }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/sprints/${sprintId}/pause`,
    )
    return res.data
  },

  async resumeSprint(
    projectId: string,
    sessionId: string,
    sprintId: string,
  ): Promise<{ sprint: DevSprint; run_id?: string }> {
    const res = await api.post<Envelope<{ sprint: DevSprint; run_id?: string }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/sprints/${sprintId}/resume`,
    )
    return res.data
  },

  async cancelSprint(
    projectId: string,
    sessionId: string,
    sprintId: string,
  ): Promise<{ sprint: DevSprint }> {
    const res = await api.post<Envelope<{ sprint: DevSprint }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/sprints/${sprintId}/cancel`,
    )
    return res.data
  },

  // --- backlog planner (P1) ---------------------------------------------------
  async createTaskPlan(
    projectId: string,
    sessionId: string,
    opts?: { instruction?: string; maxTasks?: number; includeAssets?: boolean },
  ): Promise<{ plan: DevTaskPlan; run_id: string }> {
    const res = await api.post<Envelope<{ plan: DevTaskPlan; run_id: string }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/task-plans`,
      {
        instruction: opts?.instruction,
        max_tasks: opts?.maxTasks,
        include_assets: opts?.includeAssets ?? true,
      },
    )
    return res.data
  },

  async getTaskPlan(
    projectId: string,
    sessionId: string,
    planId: string,
  ): Promise<{ plan: DevTaskPlan; run_id: string | null }> {
    const res = await api.get<Envelope<{ plan: DevTaskPlan; run_id: string | null }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/task-plans/${planId}`,
    )
    return res.data
  },

  async listTaskPlans(projectId: string, sessionId: string): Promise<DevTaskPlan[]> {
    const res = await api.get<Envelope<{ plans: DevTaskPlan[] }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/task-plans`,
    )
    return res.data.plans
  },

  async updateTaskPlan(
    projectId: string,
    sessionId: string,
    planId: string,
    patch: { tasks?: DevPlanTask[]; summary?: string; assumptions?: string[] },
  ): Promise<{ plan: DevTaskPlan }> {
    const res = await api.patch<Envelope<{ plan: DevTaskPlan }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/task-plans/${planId}`,
      patch,
    )
    return res.data
  },

  async applyTaskPlan(
    projectId: string,
    sessionId: string,
    planId: string,
    opts?: { replace?: boolean; force?: boolean },
  ): Promise<{
    inserted: number
    updated: number
    skipped: number
    plan: DevTaskPlan
    board: DevBoard
  }> {
    const res = await api.post<
      Envelope<{
        inserted: number
        updated: number
        skipped: number
        plan: DevTaskPlan
        board: DevBoard
      }>
    >(`/code/projects/${projectId}/dev-sessions/${sessionId}/task-plans/${planId}/apply`, {
      replace: opts?.replace ?? false,
      force: opts?.force ?? false,
    })
    return res.data
  },

  async rejectTaskPlan(
    projectId: string,
    sessionId: string,
    planId: string,
  ): Promise<{ plan: DevTaskPlan }> {
    const res = await api.post<Envelope<{ plan: DevTaskPlan }>>(
      `/code/projects/${projectId}/dev-sessions/${sessionId}/task-plans/${planId}/reject`,
    )
    return res.data
  },
}
