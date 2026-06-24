import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Copy, GitBranch, Github, Loader2, RefreshCw } from "lucide-react"
import { toast } from "sonner"

import { githubApi, type GitHubProjectRepo } from "@/api/github"
import { Button } from "@/components/ui/button"

/**
 * Full-stack delivery → GitHub.
 *
 * Shows where the validated full-stack source landed: the session's repo, the
 * platform-owned `main` snapshot branch, and the `dev` branch the platform forks
 * (once, after a successful deploy) for secondary development — plus a ready-to-run
 * clone command and an idempotent manual re-push. Renders nothing until a repo
 * link exists (so it never clutters sessions where GitHub sync isn't set up).
 */
export function GitHubDeliveryCard({ projectId }: { projectId: string }) {
  const { t } = useTranslation("fullstack")
  const [data, setData] = useState<GitHubProjectRepo | null>(null)
  const [syncing, setSyncing] = useState(false)

  const load = useCallback(
    () =>
      githubApi
        .getProjectRepo(projectId)
        .then(setData)
        .catch(() => {}),
    [projectId]
  )
  useEffect(() => {
    void load()
  }, [load])

  const repo = data?.repo ?? null
  if (!repo) return null

  const devBranch = repo.dev_branch || "dev"
  const cloneUrl = repo.clone_url || (repo.html_url ? `${repo.html_url}.git` : null)
  const cloneCmd = cloneUrl ? `git clone -b ${devBranch} ${cloneUrl}` : null
  const shortSha = repo.last_commit_sha ? repo.last_commit_sha.slice(0, 7) : null

  const copy = (text: string) => {
    void navigator.clipboard?.writeText(text).then(
      () => toast.success(t("git.copied")),
      () => {}
    )
  }

  const resync = async () => {
    setSyncing(true)
    try {
      const result = await githubApi.syncProject(projectId)
      if (result.status === "success") {
        toast.success(t("git.synced"))
        await load()
      } else {
        toast.error(result.error || t("git.syncFailed"))
      }
    } catch {
      toast.error(t("git.syncFailed"))
    } finally {
      setSyncing(false)
    }
  }

  return (
    <div className="mt-2 flex flex-col gap-2 rounded-md border bg-background/60 p-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <a
          href={repo.html_url ?? "#"}
          target="_blank"
          rel="noreferrer noopener"
          className="inline-flex items-center gap-1.5 text-xs font-medium hover:underline"
          title={repo.full_name}
        >
          <Github className="h-3.5 w-3.5" />
          <span className="max-w-[14rem] truncate">{repo.full_name}</span>
          {shortSha && <span className="font-mono text-muted-foreground">@{shortSha}</span>}
        </a>
        <Button size="sm" variant="ghost" className="h-7 px-2" onClick={resync} disabled={syncing}>
          {syncing ? (
            <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="mr-1 h-3.5 w-3.5" />
          )}
          {t("git.resync")}
        </Button>
      </div>

      <p className="text-[11px] text-muted-foreground">
        {t("git.branchHint", { main: repo.default_branch, dev: devBranch })}
      </p>

      {cloneCmd && (
        <div className="flex items-center gap-2">
          <code className="flex-1 truncate rounded bg-muted px-2 py-1 font-mono text-[11px]">
            {cloneCmd}
          </code>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2"
            onClick={() => copy(cloneCmd)}
            aria-label={t("git.copy")}
          >
            <Copy className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}

      <p className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
        <GitBranch className="h-3 w-3" />
        {t("git.devNote", { dev: devBranch })}
      </p>
    </div>
  )
}

export default GitHubDeliveryCard
