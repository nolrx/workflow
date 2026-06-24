/**
 * Full-stack generation panel.
 *
 * One button starts the THREE concurrent runs (frontend + backend + middleware)
 * off a single shared API contract; their progress streams live, side by side,
 * in one execution-detail surface. Once all three finish, an atomic deploy brings
 * the app up behind a reverse proxy and the preview opens in a real browser tab —
 * where the generated frontend calls the generated backend for real.
 */
import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  Boxes,
  CheckCircle2,
  ChevronDown,
  Circle,
  Database,
  Download,
  ExternalLink,
  Loader2,
  Rocket,
  Server,
  XCircle,
} from "lucide-react"
import { toast } from "sonner"

import { agentApi, type AgentRunStatus, type AgentStepStatus } from "@/api/agent"
import { tokenManager } from "@/api/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import { cn } from "@/lib/utils"
import { useCodeStore } from "@/stores/codeStore"
import {
  GEN_LANES,
  deployReady,
  type Lane,
  useFullstackStore,
} from "@/stores/fullstackStore"

const LANE_ICON: Record<Lane, typeof Server> = {
  frontend: Boxes,
  backend: Server,
  middleware: Database,
  deploy: Rocket,
}

const STATUS_VARIANT: Record<AgentRunStatus, "default" | "secondary" | "destructive" | "outline"> = {
  queued: "secondary", running: "secondary", paused: "default",
  completed: "default", partial: "secondary", failed: "destructive", cancelled: "outline",
}

// Lanes whose run publishes a downloadable source zip, keyed by the publish
// step's artifact domain_ref_type. The backend project zip mirrors the frontend
// project's download (CodeAppPreview); the deploy builds the image from this same
// source. Frontend keeps its own download in CodeAppPreview; middleware has no
// single source bundle, so neither is listed here.
const LANE_SOURCE_ZIP: Partial<Record<Lane, { type: string; filename: string }>> = {
  backend: { type: "code_backend_project_zip", filename: "backend_project.zip" },
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

function StepIcon({ status }: { status: AgentStepStatus }) {
  switch (status) {
    case "running":
      return <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
    case "completed":
      return <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
    case "failed":
      return <XCircle className="h-3.5 w-3.5 text-red-500" />
    default:
      return <Circle className="h-3.5 w-3.5 text-muted-foreground" />
  }
}

function openProjectPreview(projectId: string): void {
  const token = tokenManager.getAccessToken() ?? ""
  const url = `/preview/${encodeURIComponent(projectId)}/?token=${encodeURIComponent(token)}`
  window.open(url, "_blank", "noopener,noreferrer")
}

function LaneCard({ lane }: { lane: Lane }) {
  const { t } = useTranslation("fullstack")
  const state = useFullstackStore((s) => s.lanes[lane])
  const [open, setOpen] = useState(false)
  const Icon = LANE_ICON[lane]

  const run = state.run
  const progress = run?.progress
  const pct = progress?.total_steps
    ? Math.round((progress.completed_steps / progress.total_steps) * 100)
    : run?.status === "completed" || run?.status === "partial"
      ? 100
      : 0
  const steps = run?.steps || []
  const events = state.events

  // Source-zip download (backend lane): the publish step attaches the project's
  // source bundle as an artifact; offer it for download once the run carries it.
  const sourceZip = LANE_SOURCE_ZIP[lane]
  const zipArtifactId = sourceZip
    ? (run?.artifacts?.find((a) => a.domain_ref_type === sourceZip.type)?.id ?? null)
    : null
  const [downloading, setDownloading] = useState(false)
  const handleDownloadSource = async () => {
    if (!zipArtifactId || !sourceZip) return
    setDownloading(true)
    try {
      const blob = await agentApi.downloadArtifact(zipArtifactId)
      downloadBlob(blob, sourceZip.filename)
    } catch {
      toast.error(t("toast.downloadFailed"))
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div className="rounded-lg border bg-card">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left"
      >
        <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{t(`lane.${lane}`)}</span>
        </span>
        {state.isStreaming && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
        {run ? (
          <Badge variant={STATUS_VARIANT[run.status]} className="shrink-0">
            {t(`status.${run.status}`, { defaultValue: run.status })}
          </Badge>
        ) : (
          <Badge variant="outline" className="shrink-0">
            {t("status.idle")}
          </Badge>
        )}
        <ChevronDown className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-180")} />
      </button>

      <div className="px-3 pb-2">
        <div className="flex items-center gap-2">
          <Progress value={pct} className="h-1.5 flex-1" />
          <span className="shrink-0 text-[10px] text-muted-foreground">
            {progress ? `${progress.completed_steps}/${progress.total_steps}` : "—"}
          </span>
          {zipArtifactId && (
            <Button
              size="sm"
              variant="outline"
              className="h-6 shrink-0 gap-1 px-2 text-[11px]"
              onClick={handleDownloadSource}
              disabled={downloading}
              title={t("downloadSource")}
            >
              {downloading ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Download className="h-3 w-3" />
              )}
              {t("downloadSource")}
            </Button>
          )}
        </div>
      </div>

      {open && (
        <div className="border-t px-3 py-2">
          {/* Steps */}
          <div className="space-y-1">
            {steps.map((step) => (
              <div key={step.id} className="flex items-center gap-2 text-xs">
                <StepIcon status={step.status} />
                <span className="min-w-0 flex-1 truncate">{step.agent_name}</span>
                {step.output_summary && (
                  <span className="hidden max-w-[40%] truncate text-muted-foreground sm:block">
                    {step.output_summary}
                  </span>
                )}
              </div>
            ))}
            {steps.length === 0 && (
              <p className="py-2 text-center text-[11px] text-muted-foreground">{t("waiting")}</p>
            )}
          </div>
          {/* Recent events (tail) */}
          {events.length > 0 && (
            <div className="mt-2 max-h-40 space-y-1 overflow-y-auto border-t pt-2">
              {events.slice(-40).map((e) => (
                <div key={e.id} className="flex gap-1.5 text-[11px]">
                  <span
                    className={cn(
                      "mt-1 h-1.5 w-1.5 shrink-0 rounded-full",
                      e.level === "error" ? "bg-red-500" : e.level === "warning" ? "bg-amber-500" : "bg-sky-500"
                    )}
                  />
                  <span className="min-w-0 flex-1 break-words leading-snug">{e.message}</span>
                </div>
              ))}
            </div>
          )}
          {run?.error_message && (
            <p className="mt-2 rounded bg-destructive/10 px-2 py-1 text-[11px] text-destructive">
              {run.error_message}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

export function CodeFullstackPanel() {
  const { t } = useTranslation("fullstack")
  const project = useCodeStore((s) => s.project)

  const lanes = useFullstackStore((s) => s.lanes)
  const contract = useFullstackStore((s) => s.contract)
  const deployment = useFullstackStore((s) => s.deployment)
  const starting = useFullstackStore((s) => s.starting)
  const deploying = useFullstackStore((s) => s.deploying)
  const startFullstack = useFullstackStore((s) => s.startFullstack)
  const startDeploy = useFullstackStore((s) => s.startDeploy)
  const hydrate = useFullstackStore((s) => s.hydrate)
  const reset = useFullstackStore((s) => s.reset)

  // Bind to the current project: hydrate any existing pipeline, reset on change.
  useEffect(() => {
    if (!project?.id) return
    void hydrate(project.id)
    return () => reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.id])

  const hasRuns = GEN_LANES.some((lane) => lanes[lane].runId)
  const anyStreaming = (["frontend", "backend", "middleware", "deploy"] as Lane[]).some(
    (lane) => lanes[lane].isStreaming
  )
  const ready = useMemo(() => deployReady(lanes), [lanes])
  // The generation pipeline has settled (every lane reached a terminal state), so
  // any deploy decision is final rather than taken mid-run.
  const genSettled = useMemo(
    () =>
      GEN_LANES.every((lane) => {
        const s = lanes[lane].run?.status
        return s === "completed" || s === "partial" || s === "failed" || s === "cancelled"
      }),
    [lanes]
  )
  // A non-backend lane failed but the backend is ready: deploy is still possible,
  // just with a heads-up (the db init / live frontend preview may be incomplete).
  const genHadFailure = useMemo(
    () =>
      GEN_LANES.some((lane) => {
        const s = lanes[lane].run?.status
        return s === "failed" || s === "cancelled"
      }),
    [lanes]
  )
  const backendFailed =
    lanes.backend.run?.status === "failed" || lanes.backend.run?.status === "cancelled"
  const deployState = lanes.deploy
  const deployRunning = deployment?.status === "running"
  // Backend requires BOTH the requirements doc and the development flow (mirrors
  // the server-side `start_fullstack` precondition) before the pipeline can start.
  const prereqsReady = !!project?.requirements_doc && !!project?.development_flow
  const canStart = prereqsReady && !starting && !anyStreaming
  const ts = contract?.api_contract?.tech_stack

  const handleStart = async () => {
    if (!project?.id) return
    try {
      await startFullstack(project.id)
      toast.success(t("toast.started"))
    } catch (err) {
      const message =
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("toast.startFailed")
      toast.error(message)
    }
  }

  const handleDeploy = async () => {
    if (!project?.id) return
    try {
      await startDeploy(project.id)
      toast.success(t("toast.deployStarted"))
    } catch (err) {
      const message =
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("toast.deployFailed")
      toast.error(message)
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold">{t("title")}</h3>
          {contract?.contract_status === "ready" && (
            <Badge variant="secondary" className="gap-1">
              {t("contractReady")}
              {ts?.language ? ` · ${ts.language}/${ts.framework}` : ""}
            </Badge>
          )}
          {deployRunning && <Badge variant="default">{t("deployed")}</Badge>}
        </div>
        <div className="flex items-center gap-2">
          {deployRunning && project?.id && (
            <Button size="sm" variant="outline" onClick={() => openProjectPreview(project.id)}>
              <ExternalLink className="mr-2 h-4 w-4" />
              {t("openInBrowser")}
            </Button>
          )}
          <Button size="sm" onClick={handleStart} disabled={!canStart}>
            {starting || anyStreaming ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Boxes className="mr-2 h-4 w-4" />
            )}
            {hasRuns ? t("regenerate") : t("generate")}
          </Button>
        </div>
      </div>

      {!prereqsReady && (
        <p className="text-xs text-amber-600">{t("needFlowHint")}</p>
      )}

      <p className="text-xs text-muted-foreground">{t("intro")}</p>

      {hasRuns && (
        <div className="grid gap-2">
          {GEN_LANES.map((lane) => (
            <LaneCard key={lane} lane={lane} />
          ))}
        </div>
      )}

      {/* Deploy section */}
      {hasRuns && (
        <div className="rounded-lg border bg-muted/30 p-3">
          {deployState.runId ? (
            <LaneCard lane="deploy" />
          ) : ready ? (
            <div className="flex flex-col gap-2">
              {genHadFailure && (
                <p className="text-[11px] text-amber-600">{t("deployDespiteFailure")}</p>
              )}
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs text-muted-foreground">{t("deployHint")}</p>
                <Button size="sm" onClick={handleDeploy} disabled={deploying}>
                  {deploying ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <Rocket className="mr-2 h-4 w-4" />
                  )}
                  {t("deploy")}
                </Button>
              </div>
            </div>
          ) : genSettled && backendFailed ? (
            <p className="text-xs text-amber-600">{t("backendRequired")}</p>
          ) : (
            <p className="text-xs text-muted-foreground">{t("waitingAll")}</p>
          )}

          {deployment?.error_message && deployment.status !== "running" && (
            <p className="mt-2 rounded bg-destructive/10 px-2 py-1 text-[11px] text-destructive">
              {deployment.error_message}
            </p>
          )}
          {deployRunning && deployment?.api_base_path && (
            <p className="mt-2 text-[11px] text-muted-foreground">
              {t("apiBase", { base: deployment.api_base_path })}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

export default CodeFullstackPanel
