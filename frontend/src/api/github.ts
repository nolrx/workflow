/**
 * GitHub integration API client (Code domain).
 *
 * Read-only: syncing is automatic (the agent runtime pushes each session's
 * deliverables to its repo when a `code_*` run completes). These endpoints just
 * surface whether the org-level GitHub App is configured and the per-session
 * repo link + push history for status display.
 */
import { api } from "@/api/client"

export interface GitHubStatus {
  configured: boolean
  connected: boolean
  owner?: string
  installation_id?: string
  error?: string
}

export interface GitHubRepo {
  id: string
  project_id: string
  repo_owner: string
  repo_name: string
  full_name: string
  default_branch: string
  html_url: string | null
  visibility: string
  last_commit_sha: string | null
  last_pushed_at: string | null
  last_status: string | null
  created_at?: string | null
  updated_at?: string | null
  // The branch the platform forks (once) for secondary development + a clone URL.
  dev_branch?: string | null
  clone_url?: string | null
}

export interface GitHubSyncResult {
  status: string
  repo_url?: string | null
  full_name?: string
  branch?: string
  commit_sha?: string
  files?: number
  dev_branch?: string
  skipped?: { path: string; size: number }[]
  error?: string
}

export interface GitHubPush {
  id: string
  project_id: string
  run_id: string | null
  status: "pending" | "success" | "failed"
  branch: string | null
  commit_sha: string | null
  files_count: number | null
  message: string | null
  error_message: string | null
  created_at?: string | null
  finished_at?: string | null
}

export interface GitHubProjectRepo {
  configured: boolean
  linked: boolean
  repo: GitHubRepo | null
  last_push: GitHubPush | null
}

interface Envelope<T> {
  data: T
  message?: string
}

export const githubApi = {
  getStatus: async (): Promise<GitHubStatus> => {
    const response = await api.get<Envelope<GitHubStatus>>("/code/github/status")
    return response.data
  },
  getProjectRepo: async (projectId: string): Promise<GitHubProjectRepo> => {
    const response = await api.get<Envelope<GitHubProjectRepo>>(
      `/code/github/projects/${projectId}/repo`
    )
    return response.data
  },
  getProjectPushes: async (projectId: string): Promise<GitHubPush[]> => {
    const response = await api.get<Envelope<{ pushes: GitHubPush[] }>>(
      `/code/github/projects/${projectId}/pushes`
    )
    return response.data.pushes
  },
  // Idempotent self-service re-push (reuses the auto-sync path server-side).
  syncProject: async (projectId: string): Promise<GitHubSyncResult> => {
    const response = await api.post<Envelope<GitHubSyncResult>>(
      `/code/github/projects/${projectId}/sync`
    )
    return response.data
  },
}
