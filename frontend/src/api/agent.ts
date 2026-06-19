/**
 * Agent Swarm API client.
 *
 * The run lifecycle is: createRun -> stream (SSE, handled in the store) with
 * fetchRun as the reconnect / refresh fallback. Artifacts are displayed inline
 * (content returned in the run snapshot); downloads go through the authenticated
 * apiClient so the owner-only file endpoint receives the bearer token.
 */
import { api, apiClient } from "@/api/client"

export const AGENT_API_BASE = import.meta.env.VITE_API_URL || "/api"

export type AgentRunStatus =
  | "queued"
  | "running"
  | "paused"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled"

export type AgentStepStatus = "pending" | "running" | "completed" | "failed" | "skipped"

export interface AgentRunProgress {
  total_steps: number
  completed_steps: number
  failed_steps: number
  current_step: string | null
  /** Stage currently awaiting user confirmation (set while the run is paused). */
  review_stage?: string | null
  /** Next stage to run when the user approves (resume cursor). */
  cursor?: string | null
}

export interface AgentStep {
  id: string
  run_id: string
  parent_step_id: string | null
  agent_key: string
  agent_name: string
  role: string | null
  order_index: number
  attempt: number
  status: AgentStepStatus
  input_summary: string | null
  output_summary: string | null
  reasoning_summary: string | null
  decision_notes: string | null
  self_check: string | null
  next_action: string | null
  model_provider: string | null
  model_name: string | null
  prompt_snapshot: string | null
  model_response: string | null
  /** Internal / debug-only: context-ledger snapshot + verification recorded for this step. */
  context_snapshot?: { injected_text?: string; ledger?: Record<string, unknown> } | null
  context_check?: {
    deterministic?: { ok?: boolean; level?: string; checks?: unknown[]; summary?: string }
    ai_gate?: { conflict?: boolean; conflicts?: unknown[]; summary?: string; degraded?: boolean } | null
  } | null
  error_message: string | null
  created_at: string | null
  started_at: string | null
  completed_at: string | null
}

export interface AgentEvent {
  id: string
  run_id: string
  step_id: string | null
  sequence: number
  event_type: string
  level: "info" | "warning" | "error"
  message: string | null
  payload: Record<string, unknown>
  created_at: string | null
}

export interface AgentArtifact {
  id: string
  run_id: string
  step_id: string | null
  artifact_type: "markdown" | "json" | "text" | "image"
  title: string
  filename: string | null
  mime_type: string | null
  preview_url: string | null
  file_url: string | null
  domain_ref_type: string | null
  domain_ref_id: string | null
  version: number
  created_at: string | null
  content_text?: string | null
  content_json?: unknown
}

export interface AgentRun {
  id: string
  user_id: string
  team_id: string | null
  domain: string
  workflow: string
  resource_type: string | null
  resource_id: string | null
  title: string | null
  status: AgentRunStatus
  input_snapshot: Record<string, unknown>
  config: Record<string, unknown>
  progress: AgentRunProgress
  /** Internal / debug-only: the run's evolving consensus ledger. */
  context_ledger?: Record<string, unknown>
  credit_reserved: number
  credit_used: number
  error_message: string | null
  created_at: string | null
  started_at: string | null
  completed_at: string | null
  steps?: AgentStep[]
  events?: AgentEvent[]
  artifacts?: AgentArtifact[]
}

export interface CreateRunBody {
  domain: string
  workflow: string
  resource_type?: string | null
  resource_id?: string | null
  team_id?: string | null
  config?: Record<string, unknown>
}

export interface CreateRunResult {
  run_id: string
  status: AgentRunStatus
  stream_url: string
}

interface Envelope<T> {
  data: T
  message?: string
}

export const agentApi = {
  createRun: async (body: CreateRunBody): Promise<CreateRunResult> => {
    const response = await api.post<Envelope<CreateRunResult>>("/agent/runs", body)
    return response.data
  },
  fetchRun: async (runId: string): Promise<AgentRun> => {
    const response = await api.get<Envelope<{ run: AgentRun }>>(`/agent/runs/${runId}`)
    return response.data.run
  },
  /** List the current user's runs, optionally scoped to a domain or a resource (for replay). */
  listRuns: async (params?: {
    domain?: string
    resourceId?: string
    limit?: number
  }): Promise<AgentRun[]> => {
    const q = new URLSearchParams()
    if (params?.domain) q.set("domain", params.domain)
    if (params?.resourceId) q.set("resource_id", params.resourceId)
    if (params?.limit) q.set("limit", String(params.limit))
    const response = await api.get<Envelope<{ runs: AgentRun[] }>>(
      `/agent/runs?${q.toString()}`
    )
    return response.data.runs
  },
  cancelRun: async (runId: string): Promise<void> => {
    await api.post<Envelope<unknown>>(`/agent/runs/${runId}/cancel`)
  },
  /** Resume a paused run: approve the reviewed document or revise it. */
  resumeRun: async (
    runId: string,
    body: { action: "approve" | "revise"; stage?: string | null; instruction?: string }
  ): Promise<CreateRunResult> => {
    const response = await api.post<Envelope<CreateRunResult>>(
      `/agent/runs/${runId}/resume`,
      body
    )
    return response.data
  },
  /** Download an artifact's file via the authenticated client (returns a Blob). */
  downloadArtifact: async (artifactId: string): Promise<Blob> => {
    const response = await apiClient.get(`/agent/artifacts/${artifactId}/file?download=1`, {
      responseType: "blob",
    })
    return response.data as Blob
  },
}
