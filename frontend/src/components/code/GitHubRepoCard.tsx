import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Github, Loader2 } from "lucide-react"

import { githubApi, type GitHubProjectRepo } from "@/api/github"
import { Badge } from "@/components/ui/badge"
import { useAgentStore } from "@/stores/agentStore"

/**
 * Read-only GitHub sync status for a Code session.
 *
 * Syncing is automatic (fired by the agent runtime when a run completes), so this
 * has no trigger — it surfaces the repo link + latest push outcome. Live state is
 * derived from the run's `github_sync` events (which stream just before
 * run_completed); the persisted repo link is (re)fetched whenever one arrives.
 */
export function GitHubRepoCard({ projectId }: { projectId: string }) {
  const { t } = useTranslation("code")
  const events = useAgentStore((state) => state.events)
  const [data, setData] = useState<GitHubProjectRepo | null>(null)

  // Latest github_sync event in the current run (highest sequence), if any.
  const syncEvents = events.filter((event) => event.event_type === "github_sync")
  const latest = syncEvents.length ? syncEvents[syncEvents.length - 1] : null
  const liveStatus = (latest?.payload?.status as string | undefined) ?? null

  // Returns the latest repo status; setState happens in the effects' async
  // continuations (not synchronously in the effect body).
  const fetchRepo = useCallback(
    () => githubApi.getProjectRepo(projectId).catch(() => null),
    [projectId]
  )

  // Initial load + reload when the live sync settles (success/failed).
  useEffect(() => {
    let cancelled = false
    void fetchRepo().then((result) => {
      if (!cancelled && result) setData(result)
    })
    return () => {
      cancelled = true
    }
  }, [fetchRepo])

  const settledSeqRef = useRef<number | null>(null)
  useEffect(() => {
    if (!latest || (liveStatus !== "success" && liveStatus !== "failed")) return
    if (settledSeqRef.current === latest.sequence) return
    settledSeqRef.current = latest.sequence
    let cancelled = false
    void fetchRepo().then((result) => {
      if (!cancelled && result) setData(result)
    })
    return () => {
      cancelled = true
    }
  }, [latest, liveStatus, fetchRepo])

  const repo = data?.repo ?? null
  const lastPush = data?.last_push ?? null
  // Hide entirely until there's something to show (avoids cluttering sessions
  // where GitHub sync isn't set up).
  if (!repo && !latest) return null

  const pushing = liveStatus === "pending"
  const status = pushing
    ? "pending"
    : liveStatus || lastPush?.status || repo?.last_status || "success"

  const variant =
    status === "failed" ? "destructive" : status === "pending" ? "default" : "outline"
  const label =
    status === "pending"
      ? t("github.syncing")
      : status === "failed"
        ? t("github.failed")
        : t("github.synced")
  const shortSha = repo?.last_commit_sha ? repo.last_commit_sha.slice(0, 7) : null

  return (
    <div className="flex items-center gap-2">
      {repo?.html_url ? (
        <a
          href={repo.html_url}
          target="_blank"
          rel="noreferrer noopener"
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground hover:underline"
          title={repo.full_name}
        >
          <Github className="h-3.5 w-3.5" />
          <span className="max-w-[10rem] truncate">{repo.full_name}</span>
          {shortSha && <span className="font-mono opacity-70">@{shortSha}</span>}
        </a>
      ) : (
        <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <Github className="h-3.5 w-3.5" />
          {t("github.label")}
        </span>
      )}
      <Badge variant={variant} className="gap-1">
        {pushing && <Loader2 className="h-3 w-3 animate-spin" />}
        {label}
      </Badge>
    </div>
  )
}

export default GitHubRepoCard
