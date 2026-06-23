/**
 * Standalone live preview of the generated frontend PROJECT.
 *
 * The file-generation stage runs the containerized coding agent
 * (`code_frontend_project_generation`): a headless Claude Code CLI builds a
 * complete multi-file React + Vite + TypeScript project inside a throwaway
 * Docker container (when the UI needs real imagery it triggers the bundled
 * image-assets skill, which has Codex generate raster assets via the image
 * model), builds it, and publishes the source (zip) plus the built
 * `dist`. This pane resolves that deliverable from either the current agent run
 * in the store (live, right after generation) or — on reload — the latest
 * project run for the Code project. Rather than embedding the heavy live app
 * inside the chat transcript, it surfaces the result as a compact card and opens
 * the build in a real browser tab via the session-bound deployed route
 * (`/preview/<projectId>/`) — a native, full-page experience — while still
 * offering the source zip for download. It also exposes the "generate" trigger.
 *
 * Wiring (left to the page owner, e.g. as a 5th preview tab):
 *   <CodeAppPreview />
 */
import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { Bot, Download, ExternalLink, FileCode2, Loader2, RefreshCw } from "lucide-react"
import { toast } from "sonner"

import { agentApi, type AgentRun } from "@/api/agent"
import { tokenManager } from "@/api/client"
import { figmaApi } from "@/api/figma"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { useAgentStore } from "@/stores/agentStore"
import { useCodeStore } from "@/stores/codeStore"

interface FrontendProject {
  runId: string
  zipArtifactId: string | null
  sourceCount: number
  distCount: number
  costUsd?: number
}

const FRONTEND_WORKFLOW = "code_frontend_project_generation"

/** Pull the published frontend project (meta + source zip) out of a run snapshot. */
function projectFromRun(run: AgentRun | null): FrontendProject | null {
  if (!run?.artifacts) return null
  const meta = run.artifacts.find(
    (item) =>
      item.artifact_type === "json" && item.domain_ref_type === "code_frontend_project_meta"
  )
  const metaJson = meta?.content_json as
    | { preview_url?: string; source_files?: string[]; dist_files?: string[]; cost_usd?: number }
    | undefined
  if (!metaJson?.preview_url) return null
  const zip = run.artifacts.find((item) => item.domain_ref_type === "code_frontend_project_zip")
  return {
    runId: run.id,
    zipArtifactId: zip?.id ?? null,
    sourceCount: metaJson.source_files?.length ?? 0,
    distCount: metaJson.dist_files?.length ?? 0,
    costUsd: metaJson.cost_usd,
  }
}

/**
 * Open the project's deployed frontend build in a new browser tab via the
 * session-bound route `/preview/<projectId>/`. The route is keyed by the Code
 * project (not a run id), so it always resolves to the latest built run on the
 * backend. A one-shot `?token=` proves ownership on entry; the backend pins it
 * into a path-scoped cookie and redirects to a token-less URL, so the JWT never
 * lingers in the address bar while relative asset requests stay authenticated.
 */
function openProjectPreview(projectId: string): void {
  const token = tokenManager.getAccessToken() ?? ""
  const url = `/preview/${encodeURIComponent(projectId)}/?token=${encodeURIComponent(token)}`
  window.open(url, "_blank", "noopener,noreferrer")
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

export function CodeAppPreview() {
  const { t } = useTranslation("codeapp")
  const { t: tf } = useTranslation("code")

  const project = useCodeStore((state) => state.project)
  const run = useAgentStore((state) => state.run)
  const isStreaming = useAgentStore((state) => state.isStreaming)
  const startRun = useAgentStore((state) => state.startRun)

  const [historyProject, setHistoryProject] = useState<FrontendProject | null>(null)
  const [loadingHistory, setLoadingHistory] = useState(false)
  const [downloading, setDownloading] = useState(false)
  // Attached Figma design (if any) — generation will follow it.
  const [figmaFrameCount, setFigmaFrameCount] = useState(0)

  useEffect(() => {
    if (!project?.id) return
    let cancelled = false
    void figmaApi
      .getDesign(project.id)
      .then((d) => !cancelled && setFigmaFrameCount(d?.count ?? 0))
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [project?.id])

  // Live project from the run currently in the store (right after generation).
  const liveProject = useMemo(() => projectFromRun(run), [run])
  const builtProject = liveProject ?? historyProject

  const frontendRunActive = isStreaming && run?.workflow === FRONTEND_WORKFLOW

  // On reload (no live project yet), look up the latest project run for this Code project.
  useEffect(() => {
    let cancelled = false
    if (liveProject || !project?.id) return
    const projectId = project.id
    void (async () => {
      setLoadingHistory(true)
      try {
        const runs = await agentApi.listRuns({ domain: "code", resourceId: projectId, limit: 20 })
        const latest = runs
          .filter(
            (item) =>
              item.workflow === FRONTEND_WORKFLOW &&
              item.resource_id === projectId &&
              (item.status === "completed" || item.status === "partial")
          )
          .sort((a, b) =>
            a.created_at && b.created_at ? b.created_at.localeCompare(a.created_at) : 0
          )[0]
        if (!latest || cancelled) return
        const full = await agentApi.fetchRun(latest.id)
        if (cancelled) return
        setHistoryProject(projectFromRun(full))
      } catch {
        // Non-critical: the live flow still works; history is best-effort.
      } finally {
        if (!cancelled) setLoadingHistory(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [liveProject, project?.id])

  const handleGenerate = async () => {
    if (!project?.id) return
    try {
      await startRun({
        domain: "code",
        workflow: FRONTEND_WORKFLOW,
        resource_type: "code_project",
        resource_id: project.id,
      })
      setHistoryProject(null) // the live run becomes the source of truth
      toast.success(t("toast.started"))
    } catch (err) {
      const message =
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("toast.startFailed")
      toast.error(message)
    }
  }

  const handleDownload = async () => {
    if (!builtProject?.zipArtifactId) return
    setDownloading(true)
    try {
      const blob = await agentApi.downloadArtifact(builtProject.zipArtifactId)
      downloadBlob(blob, "frontend_project.zip")
    } catch {
      toast.error(t("toast.downloadFailed"))
    } finally {
      setDownloading(false)
    }
  }

  const canGenerate = !!project && !isStreaming
  const notConfirmed = project && project.status !== "ui_confirmed"

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold">{t("title")}</h3>
          {builtProject && (
            <Badge variant="secondary" className="gap-1">
              <FileCode2 className="h-3.5 w-3.5" />
              {t("filesReady", {
                source: builtProject.sourceCount,
                dist: builtProject.distCount,
              })}
            </Badge>
          )}
          {builtProject?.costUsd != null && builtProject.costUsd > 0 && (
            <Badge variant="outline">{t("cost", { cost: builtProject.costUsd.toFixed(2) })}</Badge>
          )}
          {figmaFrameCount > 0 && (
            <Badge variant="secondary">{tf("figma.attached", { count: figmaFrameCount })}</Badge>
          )}
        </div>
        <div className="flex items-center gap-2">
          {builtProject && project?.id && (
            <Button size="sm" variant="outline" onClick={() => openProjectPreview(project.id)}>
              <ExternalLink className="mr-2 h-4 w-4" />
              {t("openInBrowser")}
            </Button>
          )}
          {builtProject?.zipArtifactId && (
            <Button size="sm" variant="outline" onClick={handleDownload} disabled={downloading}>
              {downloading ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <Download className="mr-2 h-4 w-4" />
              )}
              {t("download")}
            </Button>
          )}
          <Button size="sm" onClick={handleGenerate} disabled={!canGenerate}>
            {frontendRunActive ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : builtProject ? (
              <RefreshCw className="mr-2 h-4 w-4" />
            ) : (
              <Bot className="mr-2 h-4 w-4" />
            )}
            {builtProject ? t("regenerate") : t("generate")}
          </Button>
        </div>
      </div>

      {notConfirmed && !builtProject && (
        <p className="text-xs text-amber-600">{t("needConfirmHint")}</p>
      )}

      <div className="min-h-0 flex-1 overflow-hidden rounded-md border">
        {frontendRunActive ? (
          <div className="flex h-full min-h-64 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-6 w-6 animate-spin" />
            <span>{t("generating")}</span>
          </div>
        ) : builtProject && project?.id ? (
          // Native preview: open the deployed build in a real browser tab instead
          // of embedding the heavy live app inside the chat transcript.
          <div className="flex h-full min-h-64 flex-col items-center justify-center gap-4 p-8 text-center">
            <div className="flex h-14 w-14 items-center justify-center rounded-full bg-primary/10 text-primary">
              <ExternalLink className="h-7 w-7" />
            </div>
            <div className="space-y-1">
              <p className="text-sm font-medium">{t("readyTitle")}</p>
              <p className="max-w-sm text-xs text-muted-foreground">{t("readyHint")}</p>
            </div>
            <Button onClick={() => openProjectPreview(project.id)}>
              <ExternalLink className="mr-2 h-4 w-4" />
              {t("openInBrowser")}
            </Button>
          </div>
        ) : loadingHistory ? (
          <div className="flex h-full min-h-64 items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />
            {t("loadingHistory")}
          </div>
        ) : (
          <div className="flex h-full min-h-64 items-center justify-center p-8 text-center text-sm text-muted-foreground">
            {t("empty")}
          </div>
        )}
      </div>
    </div>
  )
}

export default CodeAppPreview
