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
import {
  Bot,
  Check,
  Copy,
  Download,
  ExternalLink,
  FileCode2,
  Globe,
  History,
  Loader2,
  Lock,
  RefreshCw,
} from "lucide-react"
import { toast } from "sonner"

import { agentApi, type AgentRun } from "@/api/agent"
import { tokenManager } from "@/api/client"
import { codeApi } from "@/api/code"
import { figmaApi } from "@/api/figma"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
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
  // Match the publish step's project-meta artifact. Guard on preview_url too: a
  // run can carry other JSON artifacts tagged code_frontend_project_meta (e.g. an
  // older acceptance-review row), and only the real meta has a preview_url — so
  // picking by type alone could grab the wrong one and render no preview.
  const meta = run.artifacts.find(
    (item) =>
      item.artifact_type === "json" &&
      item.domain_ref_type === "code_frontend_project_meta" &&
      Boolean((item.content_json as { preview_url?: string } | undefined)?.preview_url)
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

/**
 * Open a SPECIFIC built run's site in a new tab (used when an older version is
 * selected). The `/preview/<projectId>/` route only serves the latest run, so a
 * non-latest version goes through the run-scoped site route, whose relative
 * assets resolve under that run's path.
 */
function openRunSite(runId: string): void {
  const token = tokenManager.getAccessToken() ?? ""
  const url = `/api/agent/runs/${encodeURIComponent(runId)}/site/index.html?token=${encodeURIComponent(token)}`
  window.open(url, "_blank", "noopener,noreferrer")
}

/** "2026-06-23T08:17:54…" -> "06-23 08:17" (as stored, UTC — order-stable). */
function fmtVersionTime(iso: string | null): string {
  if (!iso) return ""
  const m = iso.match(/\d{4}-(\d{2}-\d{2})T(\d{2}:\d{2})/)
  return m ? `${m[1]} ${m[2]}` : iso.slice(0, 16)
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
  const updateProjectDraft = useCodeStore((state) => state.updateProjectDraft)
  const run = useAgentStore((state) => state.run)
  const isStreaming = useAgentStore((state) => state.isStreaming)
  const startRun = useAgentStore((state) => state.startRun)

  // Built frontend versions (runs) for this project; the selected version drives
  // the preview / download. Selection is DERIVED (see below) so we never sync
  // state inside an effect — `pickedRunId` is just the user's explicit override.
  const [versions, setVersions] = useState<AgentRun[]>([])
  const [pickedRunId, setPickedRunId] = useState<string | null>(null)
  // Fetched deliverable for the last resolved run, tagged with its id so we can
  // tell whether it still matches the current selection.
  const [resolved, setResolved] = useState<{
    runId: string
    project: FrontendProject | null
  } | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [togglingPublic, setTogglingPublic] = useState(false)
  const [copied, setCopied] = useState(false)
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
  const liveRunId = liveProject?.runId ?? null
  const frontendRunActive = isStreaming && run?.workflow === FRONTEND_WORKFLOW

  // Built versions for THIS project, newest first (guard against a stale list from
  // a previous project while the new fetch is in flight).
  const projectId = project?.id
  const projectVersions = useMemo(
    () =>
      projectId
        ? versions
            .filter((v) => v.resource_id === projectId)
            .slice()
            .sort((a, b) =>
              a.created_at && b.created_at ? b.created_at.localeCompare(a.created_at) : 0
            )
        : [],
    [versions, projectId]
  )

  // Selection is DERIVED (no effect): a still-valid manual pick wins; otherwise the
  // default = a just-finished live run, else the newest version (latest output).
  const defaultRunId =
    (liveRunId && projectVersions.some((v) => v.id === liveRunId)
      ? liveRunId
      : projectVersions[0]?.id) ?? null
  const selectedRunId =
    pickedRunId && projectVersions.some((v) => v.id === pickedRunId) ? pickedRunId : defaultRunId

  const selectedProject = resolved?.runId === selectedRunId ? resolved.project : null
  // Selected version's deliverable; fall back to the live snapshot when the live
  // run IS the selection, so a just-finished build shows instantly.
  const builtProject = selectedProject ?? (liveRunId === selectedRunId ? liveProject : null)
  const loadingHistory = !!selectedRunId && !builtProject
  const isLatestSelected =
    projectVersions.length > 0 && projectVersions[0]?.id === selectedRunId

  // Fetch the version list (best-effort). Re-runs when a frontend run finishes
  // (its id/status changes) so a fresh build shows up as a new version.
  useEffect(() => {
    if (!projectId) return
    let cancelled = false
    void agentApi
      .listRuns({ domain: "code", resourceId: projectId, limit: 50 })
      .then((runs) => {
        if (cancelled) return
        setVersions(
          runs.filter(
            (item) =>
              item.workflow === FRONTEND_WORKFLOW &&
              item.resource_id === projectId &&
              (item.status === "completed" || item.status === "partial")
          )
        )
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [projectId, run?.id, run?.status])

  // Resolve the selected version's deliverable (meta + zip). setState only runs in
  // the async callback, never synchronously in the effect body.
  useEffect(() => {
    if (!selectedRunId) return
    let cancelled = false
    void agentApi
      .fetchRun(selectedRunId)
      .then((full) => {
        if (!cancelled) setResolved({ runId: selectedRunId, project: projectFromRun(full) })
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [selectedRunId])

  const handleGenerate = async () => {
    if (!project?.id) return
    try {
      await startRun({
        domain: "code",
        workflow: FRONTEND_WORKFLOW,
        resource_type: "code_project",
        resource_id: project.id,
      })
      setPickedRunId(null) // follow the newest output once the new run completes
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

  const isPublic = project?.visibility === "public"
  const publicUrl =
    project?.id && typeof window !== "undefined"
      ? `${window.location.origin}/preview/${project.id}/`
      : ""

  const handleTogglePublic = async () => {
    if (!project?.id || togglingPublic) return
    setTogglingPublic(true)
    try {
      const res = await codeApi.setPreviewVisibility(project.id, !isPublic)
      updateProjectDraft({ visibility: res.visibility })
      toast.success(res.public ? t("toast.madePublic") : t("toast.madePrivate"))
    } catch {
      toast.error(t("toast.visibilityFailed"))
    } finally {
      setTogglingPublic(false)
    }
  }

  const handleCopyUrl = async () => {
    if (!publicUrl) return
    try {
      await navigator.clipboard.writeText(publicUrl)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error(t("toast.copyFailed"))
    }
  }

  // Open the SELECTED version. Latest → the clean /preview/<pid>/ route (same URL
  // the public link uses); an older version → its run-scoped site route.
  const openSelectedPreview = () => {
    if (!project?.id || !builtProject) return
    if (isLatestSelected) openProjectPreview(project.id)
    else openRunSite(builtProject.runId)
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
          {projectVersions.length > 0 && selectedRunId && (
            <Select value={selectedRunId} onValueChange={setPickedRunId}>
              <SelectTrigger className="h-8 w-[190px]" title={t("version.label")}>
                <History className="h-3.5 w-3.5 shrink-0 opacity-70" />
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {projectVersions.map((v, i) => (
                  <SelectItem key={v.id} value={v.id}>
                    {fmtVersionTime(v.created_at)}
                    {i === 0 ? ` · ${t("version.latest")}` : ""}
                    {v.status === "partial" ? ` · ${t("version.partial")}` : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          {builtProject && project?.id && (
            <Button
              size="sm"
              variant={isPublic ? "default" : "outline"}
              onClick={handleTogglePublic}
              disabled={togglingPublic}
              title={isPublic ? t("public.onHint") : t("public.offHint")}
            >
              {togglingPublic ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : isPublic ? (
                <Globe className="mr-2 h-4 w-4" />
              ) : (
                <Lock className="mr-2 h-4 w-4" />
              )}
              {isPublic ? t("public.on") : t("public.off")}
            </Button>
          )}
          {builtProject && project?.id && (
            <Button size="sm" variant="outline" onClick={openSelectedPreview}>
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

      {isPublic && builtProject && publicUrl && (
        <div className="flex items-center gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2">
          <Globe className="h-4 w-4 shrink-0 text-primary" />
          <span className="shrink-0 text-xs text-muted-foreground">{t("public.shareLabel")}</span>
          <code className="min-w-0 flex-1 truncate text-xs">{publicUrl}</code>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 shrink-0 px-2"
            onClick={handleCopyUrl}
            title={t("public.copy")}
          >
            {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          </Button>
        </div>
      )}

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
            <Button onClick={openSelectedPreview}>
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
