/**
 * Standalone live preview of the generated frontend project.
 *
 * Self-contained on purpose (drop it anywhere): it resolves the generated
 * HTML artifact from either the current agent run in the store (live, right
 * after generation) or — on reload — the latest `code_frontend_generation` run
 * for the project, then renders it in an iframe for a fully-interactive,
 * in-browser preview. It also exposes the "generate HTML" trigger that starts
 * the `code_frontend_generation` workflow.
 *
 * Wiring (left to the page owner, e.g. as a 5th preview tab):
 *   <CodeAppPreview />
 */
import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { Bot, Download, Loader2, RefreshCw, ShieldCheck, ShieldAlert } from "lucide-react"
import { toast } from "sonner"

import { agentApi, type AgentRun } from "@/api/agent"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { useAgentStore } from "@/stores/agentStore"
import { useCodeStore } from "@/stores/codeStore"

interface FrontendHtml {
  artifactId: string
  html: string
  fileUrl: string | null
  reviewPassed?: boolean
  htmlChars?: number
}

const FRONTEND_WORKFLOW = "code_frontend_generation"

/** Pull the published frontend HTML artifact out of a run snapshot, if any. */
function htmlFromRun(run: AgentRun | null): FrontendHtml | null {
  if (!run?.artifacts) return null
  const htmlArtifact = run.artifacts.find(
    (item) =>
      item.artifact_type === "text" &&
      (item.domain_ref_type === "code_frontend_html" ||
        item.domain_ref_type === "code_frontend") &&
      !!item.content_text
  )
  if (!htmlArtifact?.content_text) return null
  const metaArtifact = run.artifacts.find(
    (item) => item.domain_ref_type === "code_frontend_meta" && item.artifact_type === "json"
  )
  const meta = metaArtifact?.content_json as
    | { review_passed?: boolean; html_chars?: number }
    | undefined
  return {
    artifactId: htmlArtifact.id,
    html: htmlArtifact.content_text,
    fileUrl: htmlArtifact.file_url,
    reviewPassed: meta?.review_passed,
    htmlChars: meta?.html_chars ?? htmlArtifact.content_text.length,
  }
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

  const project = useCodeStore((state) => state.project)
  const run = useAgentStore((state) => state.run)
  const isStreaming = useAgentStore((state) => state.isStreaming)
  const startRun = useAgentStore((state) => state.startRun)

  const [historyHtml, setHistoryHtml] = useState<FrontendHtml | null>(null)
  const [loadingHistory, setLoadingHistory] = useState(false)
  const [downloading, setDownloading] = useState(false)

  // Live HTML from the run currently in the store (right after generation).
  const liveHtml = useMemo(() => htmlFromRun(run), [run])
  const htmlApp = liveHtml ?? historyHtml

  const frontendRunActive =
    isStreaming && run?.workflow === FRONTEND_WORKFLOW

  // On reload (no live HTML yet), look up the latest frontend run for this project.
  useEffect(() => {
    let cancelled = false
    if (liveHtml || !project?.id) return
    setLoadingHistory(true)
    void (async () => {
      try {
        const runs = await agentApi.listRuns({ domain: "code", resourceId: project.id, limit: 20 })
        const latest = runs
          .filter(
            (item) =>
              item.workflow === FRONTEND_WORKFLOW &&
              item.resource_id === project.id &&
              (item.status === "completed" || item.status === "partial")
          )
          .sort((a, b) => (a.created_at && b.created_at ? b.created_at.localeCompare(a.created_at) : 0))[0]
        if (!latest || cancelled) return
        const full = await agentApi.fetchRun(latest.id)
        if (cancelled) return
        setHistoryHtml(htmlFromRun(full))
      } catch {
        // Non-critical: the live flow still works; history is best-effort.
      } finally {
        if (!cancelled) setLoadingHistory(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [liveHtml, project?.id])

  const handleGenerate = async () => {
    if (!project?.id) return
    try {
      await startRun({
        domain: "code",
        workflow: FRONTEND_WORKFLOW,
        resource_type: "code_project",
        resource_id: project.id,
      })
      setHistoryHtml(null) // the live run becomes the source of truth
      toast.success(t("toast.started"))
    } catch (err) {
      const message =
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("toast.startFailed")
      toast.error(message)
    }
  }

  const handleDownload = async () => {
    if (!htmlApp) return
    setDownloading(true)
    try {
      const blob = await agentApi.downloadArtifact(htmlApp.artifactId)
      downloadBlob(blob, "index.html")
    } catch {
      downloadBlob(new Blob([htmlApp.html], { type: "text/html;charset=utf-8" }), "index.html")
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
          {htmlApp?.reviewPassed === true && (
            <Badge variant="secondary" className="gap-1">
              <ShieldCheck className="h-3.5 w-3.5" />
              {t("reviewPassed")}
            </Badge>
          )}
          {htmlApp?.reviewPassed === false && (
            <Badge variant="outline" className="gap-1">
              <ShieldAlert className="h-3.5 w-3.5" />
              {t("reviewRepaired")}
            </Badge>
          )}
          {htmlApp && (
            <Badge variant="outline">
              {t("htmlReady", { count: htmlApp.htmlChars ?? htmlApp.html.length })}
            </Badge>
          )}
        </div>
        <div className="flex items-center gap-2">
          {htmlApp && (
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
            ) : htmlApp ? (
              <RefreshCw className="mr-2 h-4 w-4" />
            ) : (
              <Bot className="mr-2 h-4 w-4" />
            )}
            {htmlApp ? t("regenerate") : t("generate")}
          </Button>
        </div>
      </div>

      {notConfirmed && !htmlApp && (
        <p className="text-xs text-amber-600">{t("needConfirmHint")}</p>
      )}

      <div className="min-h-0 flex-1 overflow-hidden rounded-md border">
        {frontendRunActive ? (
          <div className="flex h-full min-h-64 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-6 w-6 animate-spin" />
            <span>{t("generating")}</span>
          </div>
        ) : htmlApp ? (
          <iframe
            title={t("iframeTitle")}
            srcDoc={htmlApp.html}
            sandbox="allow-forms allow-modals allow-scripts"
            className="h-full min-h-[540px] w-full bg-white"
          />
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
