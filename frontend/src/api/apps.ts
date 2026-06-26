/**
 * App Space (应用空间) + secondary-development (二次开发) API client.
 *
 * The App Space is the management/iteration entry for ALREADY-DEPLOYED products.
 * Its data source is CodeProject + CodeDeployment (no new "app" master table).
 * An iteration captures one change to a live app: analyze → confirm → generate →
 * deploy → release, all replayable as AgentRuns.
 */
import { api } from "@/api/client"
import type { AgentRun } from "@/api/agent"
import type { Deployment, DeploymentStatus } from "@/api/fullstack"

interface Envelope<T> {
  success: boolean
  data: T
  message?: string
}

export interface AppOwner {
  id: string
  display_name: string | null
  avatar_url: string | null
}

export interface AppListItem {
  project_id: string
  title: string
  visibility: string
  team_id: string | null
  owner: AppOwner | null
  deployment_status: DeploymentStatus
  health: string
  is_running: boolean
  api_base_path: string
  preview_url: string
  deployed_at: string | null
  updated_at: string | null
}

export interface AppListResult {
  apps: AppListItem[]
  total: number
  limit: number
  offset: number
}

export type IterationStatus =
  | "draft"
  | "analyzing"
  | "awaiting_plan_approval"
  | "generating"
  | "staging_deploying"
  | "staging_ready"
  | "release_pending"
  | "released"
  | "failed"
  | "cancelled"

export type ChangeType =
  | "bug_fix"
  | "new_feature"
  | "ui_change"
  | "backend_logic"
  | "data_model"
  | "other"

export type ImpactScope =
  | "frontend"
  | "backend"
  | "frontend_backend"
  | "backend_middleware"
  | "fullstack"

export type RiskLevel = "low" | "medium" | "high"

export interface IterationAnalysis {
  change_summary?: string
  requirement_change?: boolean
  ui_change?: boolean
  frontend_change?: boolean
  backend_change?: boolean
  middleware_change?: boolean
  contract_change?: boolean
  database_change?: boolean
  asset_generation_required?: boolean
  risk_level?: RiskLevel
  recommended_lanes?: string[]
  requires_user_confirmation?: boolean
  reasoning?: string[]
}

export interface IterationPlanStep {
  lane: string
  action: string
  description: string
}

export interface IterationPlan {
  title?: string
  scope?: string
  lanes?: string[]
  steps?: IterationPlanStep[]
  risks?: string[]
  requires_confirmation?: boolean
}

export interface RunRef {
  id: string
  workflow: string
  status: string
}

export interface AppIteration {
  id: string
  project_id: string
  base_deployment_id: string | null
  instruction: string
  change_type: ChangeType
  impact_scope: ImpactScope | null
  status: IterationStatus
  allow_contract_change: boolean
  allow_db_change: boolean
  deploy_to_prod: boolean
  analysis: IterationAnalysis
  plan: IterationPlan
  contract_diff: Record<string, unknown>
  analysis_run_id: string | null
  frontend_run_id: string | null
  backend_run_id: string | null
  middleware_run_id: string | null
  deploy_run_id: string | null
  error_message: string | null
  created_at: string | null
  updated_at: string | null
  runs: {
    analysis: RunRef | null
    frontend: RunRef | null
    backend: RunRef | null
    middleware: RunRef | null
    deploy: RunRef | null
  }
  generation_ready: boolean
}

export interface GitHubInfo {
  repo_owner?: string
  repo_name?: string
  html_url?: string
  default_branch?: string
  dev_branch?: string | null
  last_commit_sha?: string | null
  last_pushed_at?: string | null
  last_status?: string | null
  [key: string]: unknown
}

export interface AppDetail {
  project: {
    id: string
    title: string
    requirement_summary: string
    status: string
    visibility: string
    created_at: string | null
    updated_at: string | null
  }
  deployment: Deployment | null
  preview_url: string
  api_base_path: string
  tech_stack: { language?: string; framework?: string; backend?: string } & Record<string, unknown>
  runs: {
    frontend: AgentRun | null
    backend: AgentRun | null
    middleware: AgentRun | null
    deploy: AgentRun | null
  }
  github: GitHubInfo | null
  iterations: AppIteration[]
}

export interface AppListParams {
  status?: string
  health?: string
  q?: string
  /** Scope: omit / null = personal apps; a team id = that team's apps. */
  team_id?: string | null
  limit?: number
  offset?: number
}

export interface SourceSummary {
  artifact_id: string
  filename: string | null
  file_count: number
  download_url: string
  created_at: string | null
}

export interface AppResources {
  frontend: SourceSummary | null
  backend: SourceSummary | null
  database: {
    engine: string
    db_name: string | null
    redis_prefix: string | null
    table_count: number | null
    introspectable: boolean
  }
  preview_url: string
  api_base_path: string
}

export interface DbColumn {
  name: string
  type: string
}

export interface DbTable {
  name: string
  columns: DbColumn[]
  row_count: number | null
}

export interface DatabaseInfo {
  engine: string
  available: boolean
  tables: DbTable[]
  db_name?: string | null
  redis_prefix?: string | null
  error?: string
}

export interface TableRows {
  available: boolean
  columns: string[]
  rows: (string | number | boolean | null)[][]
  limit?: number
  error?: string
}

export interface CodeListing {
  lane: string
  artifact_id: string | null
  download_url?: string
  files: string[]
}

export interface CodeFile {
  lane: string
  path: string
  size: number
  content: string
  is_binary: boolean
  truncated: boolean
}

export interface AppLogs {
  available: boolean
  logs: string
  container?: string
  error?: string
}

export interface HealthProbe {
  available: boolean
  health: string
  status?: string
}

export interface CreateIterationBody {
  instruction: string
  change_type?: ChangeType
  impact_scope?: ImpactScope
  allow_contract_change?: boolean
  allow_db_change?: boolean
  deploy_to_prod?: boolean
}

export interface ConfirmIterationBody {
  impact_scope?: ImpactScope
  allow_contract_change?: boolean
  allow_db_change?: boolean
}

export const appsApi = {
  /** List the current user's deployed apps (owner-only). */
  list: async (params?: AppListParams): Promise<AppListResult> => {
    const q = new URLSearchParams()
    if (params?.status) q.set("status", params.status)
    if (params?.health) q.set("health", params.health)
    if (params?.q) q.set("q", params.q)
    if (params?.team_id) q.set("team_id", params.team_id)
    if (params?.limit != null) q.set("limit", String(params.limit))
    if (params?.offset != null) q.set("offset", String(params.offset))
    const res = await api.get<Envelope<AppListResult>>(`/code/apps?${q.toString()}`)
    return res.data
  },

  /** One deployed app's full context. */
  get: async (projectId: string): Promise<AppDetail> => {
    const res = await api.get<Envelope<AppDetail>>(`/code/apps/${projectId}`)
    return res.data
  },

  listIterations: async (projectId: string): Promise<AppIteration[]> => {
    const res = await api.get<Envelope<{ iterations: AppIteration[] }>>(
      `/code/apps/${projectId}/iterations`
    )
    return res.data.iterations
  },

  getIteration: async (projectId: string, iterationId: string): Promise<AppIteration> => {
    const res = await api.get<Envelope<{ iteration: AppIteration }>>(
      `/code/apps/${projectId}/iterations/${iterationId}`
    )
    return res.data.iteration
  },

  /** Start a 二次开发: create the iteration + kick off the impact-analysis run. */
  createIteration: async (
    projectId: string,
    body: CreateIterationBody
  ): Promise<{ iteration: AppIteration; stream_url: string }> => {
    const res = await api.post<Envelope<{ iteration: AppIteration; stream_url: string }>>(
      `/code/apps/${projectId}/iterations`,
      body
    )
    return res.data
  },

  /** Confirm the plan → start the requested generation lane runs. */
  confirmIteration: async (
    projectId: string,
    iterationId: string,
    body: ConfirmIterationBody = {}
  ): Promise<{
    iteration: AppIteration
    runs: Record<string, string>
    stream_urls: Record<string, string>
  }> => {
    const res = await api.post<
      Envelope<{
        iteration: AppIteration
        runs: Record<string, string>
        stream_urls: Record<string, string>
      }>
    >(`/code/apps/${projectId}/iterations/${iterationId}/confirm`, body)
    return res.data
  },

  /** Frontend / backend / database resources backing the app. */
  resources: async (projectId: string): Promise<AppResources> => {
    const res = await api.get<Envelope<AppResources>>(`/code/apps/${projectId}/resources`)
    return res.data
  },

  /** Read-only schema introspection of the app's database. */
  database: async (projectId: string): Promise<DatabaseInfo> => {
    const res = await api.get<Envelope<DatabaseInfo>>(`/code/apps/${projectId}/database`)
    return res.data
  },

  /** Read-only sample rows from one table. */
  tableRows: async (
    projectId: string,
    table: string,
    limit = 20
  ): Promise<TableRows> => {
    const res = await api.get<Envelope<TableRows>>(
      `/code/apps/${projectId}/database/tables/${encodeURIComponent(table)}/rows?limit=${limit}`
    )
    return res.data
  },

  /** List the files of the app's latest frontend/backend source. */
  code: async (projectId: string, lane: "frontend" | "backend"): Promise<CodeListing> => {
    const res = await api.get<Envelope<CodeListing>>(`/code/apps/${projectId}/code?lane=${lane}`)
    return res.data
  },

  /** One source file's text content. */
  codeFile: async (
    projectId: string,
    lane: "frontend" | "backend",
    path: string
  ): Promise<CodeFile> => {
    const res = await api.get<Envelope<CodeFile>>(
      `/code/apps/${projectId}/code/file?lane=${lane}&path=${encodeURIComponent(path)}`
    )
    return res.data
  },

  /** Stop a deployed app's container (keeps the db for redeploy). */
  stop: async (projectId: string): Promise<{ status: string }> => {
    const res = await api.post<Envelope<{ status: string }>>(`/code/apps/${projectId}/stop`)
    return res.data
  },

  /** Read-only tail of the deployed container's runtime logs. */
  logs: async (projectId: string, tail = 200): Promise<AppLogs> => {
    const res = await api.get<Envelope<AppLogs>>(`/code/apps/${projectId}/logs?tail=${tail}`)
    return res.data
  },

  /** Re-probe + persist the deployed app's health. */
  refreshHealth: async (projectId: string): Promise<HealthProbe> => {
    const res = await api.post<Envelope<HealthProbe>>(`/code/apps/${projectId}/health/refresh`)
    return res.data
  },
}
