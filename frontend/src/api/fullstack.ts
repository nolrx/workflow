/**
 * Full-stack pipeline API client.
 *
 * Drives the concurrent frontend + backend + middleware generation and the
 * atomic deploy. Starting the pipeline returns the three run ids (the store then
 * streams each via the shared /api/agent SSE); deploy returns a single deploy run.
 */
import { api } from "@/api/client"
import type { AgentRun } from "@/api/agent"

interface Envelope<T> {
  success: boolean
  data: T
  message?: string
}

export type ContractStatus = "pending" | "building" | "ready" | "failed"

export interface SharedContract {
  project_id: string
  contract_status: ContractStatus
  version: number
  error_message: string | null
  api_contract?: {
    openapi?: Record<string, unknown>
    api_summary?: string
    tech_stack?: { language?: string; framework?: string; backend?: string }
  }
  middleware_manifest?: {
    datastores?: { type: string; purpose?: string }[]
    cache?: { type: string; purpose?: string } | null
    env?: { name: string; purpose?: string }[]
  }
}

export type DeploymentStatus =
  | "pending"
  | "provisioning"
  | "building"
  | "starting"
  | "running"
  | "failed"
  | "stopped"
  | "rolled_back"

export interface Deployment {
  id: string
  project_id: string
  frontend_run_id: string | null
  backend_run_id: string | null
  middleware_run_id: string | null
  deploy_run_id: string | null
  container_name: string | null
  internal_port: number | null
  api_base_path: string | null
  db_name: string | null
  status: DeploymentStatus
  health: string | null
  error_message: string | null
  deployed_at: string | null
}

export interface StartFullstackResult {
  runs: { frontend: string; backend: string; middleware: string }
  contract: SharedContract
  stream_urls: Record<string, string>
}

export interface FullstackStatus {
  runs: {
    frontend: AgentRun | null
    backend: AgentRun | null
    middleware: AgentRun | null
    deploy: AgentRun | null
  }
  deployment: Deployment | null
  contract_status: ContractStatus
}

export const fullstackApi = {
  /** Synthesize the shared contract and start the three concurrent runs. */
  start: async (projectId: string): Promise<StartFullstackResult> => {
    const res = await api.post<Envelope<StartFullstackResult>>(
      `/code/projects/${projectId}/fullstack/runs`
    )
    return res.data
  },

  /** Start the atomic deploy run (requires the backend run completed). */
  deploy: async (projectId: string): Promise<{ run_id: string; stream_url: string }> => {
    const res = await api.post<Envelope<{ run_id: string; stream_url: string }>>(
      `/code/projects/${projectId}/deploy`
    )
    return res.data
  },

  /** Snapshot of the three pipeline runs + the deployment. */
  status: async (projectId: string): Promise<FullstackStatus> => {
    const res = await api.get<Envelope<FullstackStatus>>(
      `/code/projects/${projectId}/fullstack/status`
    )
    return res.data
  },

  /** The synthesized shared API contract + middleware manifest. */
  contract: async (projectId: string): Promise<SharedContract> => {
    const res = await api.get<Envelope<{ contract: SharedContract }>>(
      `/code/projects/${projectId}/contract`
    )
    return res.data.contract
  },
}
