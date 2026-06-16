/**
 * Standalone live preview of the generated frontend project.
 *
 * Self-contained on purpose (drop it anywhere): it resolves the generated
 * Sandpack bundle from either the current agent run in the store (live, right
 * after generation) or — on reload — the latest `code_frontend_generation` run
 * for the project, then renders it with Sandpack for a fully-interactive,
 * in-browser preview. It also exposes the "generate frontend" trigger that
 * starts the `code_frontend_generation` workflow.
 *
 * Wiring (left to the page owner, e.g. as a 5th preview tab):
 *   <CodeAppPreview />
 */
import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { Bot, Loader2, RefreshCw, ShieldCheck, ShieldAlert } from "lucide-react"
import { toast } from "sonner"
import { Sandpack } from "@codesandbox/sandpack-react"

import { agentApi, type AgentRun } from "@/api/agent"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { useAgentStore } from "@/stores/agentStore"
import { useCodeStore } from "@/stores/codeStore"

interface FrontendBundle {
  files: Record<string, string>
  entry: string
  components: string[]
  review_passed?: boolean
  template?: string
}

const FRONTEND_WORKFLOW = "code_frontend_generation"

/** Pull the published frontend bundle artifact out of a run snapshot, if any. */
function bundleFromRun(run: AgentRun | null): FrontendBundle | null {
  if (!run?.artifacts) return null
  const artifact = run.artifacts.find(
    (item) => item.domain_ref_type === "code_frontend" && item.artifact_type === "json"
  )
  const data = artifact?.content_json as FrontendBundle | undefined
  if (!data || !data.files || !data.files["/App.tsx"]) return null
  return data
}

export function CodeAppPreview() {
  const { t } = useTranslation("codeapp")

  const project = useCodeStore((state) => state.project)
  const run = useAgentStore((state) => state.run)
  const isStreaming = useAgentStore((state) => state.isStreaming)
  const startRun = useAgentStore((state) => state.startRun)

  const [historyBundle, setHistoryBundle] = useState<FrontendBundle | null>(null)
  const [loadingHistory, setLoadingHistory] = useState(false)

  // Live bundle from the run currently in the store (right after generation).
  const liveBundle = useMemo(() => bundleFromRun(run), [run])
  const bundle = liveBundle ?? historyBundle

  const frontendRunActive =
    isStreaming && run?.workflow === FRONTEND_WORKFLOW

  // On reload (no live bundle yet), look up the latest frontend run for this project.
  useEffect(() => {
    let cancelled = false
    if (liveBundle || !project?.id) return
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
        setHistoryBundle(bundleFromRun(full))
      } catch {
        // Non-critical: the live flow still works; history is best-effort.
      } finally {
        if (!cancelled) setLoadingHistory(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [liveBundle, project?.id])

  const handleGenerate = async () => {
    if (!project?.id) return
    try {
      await startRun({
        domain: "code",
        workflow: FRONTEND_WORKFLOW,
        resource_type: "code_project",
        resource_id: project.id,
      })
      setHistoryBundle(null) // the live run becomes the source of truth
      toast.success(t("toast.started"))
    } catch (err) {
      const message =
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("toast.startFailed")
      toast.error(message)
    }
  }

  const sandpackFiles = useMemo(() => {
    if (!bundle) return null
    // Sandpack accepts { [path]: code }. Our bundle already stores that shape.
    return bundle.files
  }, [bundle])

  const canGenerate = !!project && !isStreaming
  const notConfirmed = project && project.status !== "ui_confirmed"

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold">{t("title")}</h3>
          {bundle?.review_passed === true && (
            <Badge variant="secondary" className="gap-1">
              <ShieldCheck className="h-3.5 w-3.5" />
              {t("reviewPassed")}
            </Badge>
          )}
          {bundle?.review_passed === false && (
            <Badge variant="outline" className="gap-1">
              <ShieldAlert className="h-3.5 w-3.5" />
              {t("reviewRepaired")}
            </Badge>
          )}
          {bundle && (
            <Badge variant="outline">
              {t("filesCount", { count: Object.keys(bundle.files).length })}
            </Badge>
          )}
        </div>
        <Button size="sm" onClick={handleGenerate} disabled={!canGenerate}>
          {frontendRunActive ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : bundle ? (
            <RefreshCw className="mr-2 h-4 w-4" />
          ) : (
            <Bot className="mr-2 h-4 w-4" />
          )}
          {bundle ? t("regenerate") : t("generate")}
        </Button>
      </div>

      {notConfirmed && !bundle && (
        <p className="text-xs text-amber-600">{t("needConfirmHint")}</p>
      )}

      <div className="min-h-0 flex-1 overflow-hidden rounded-md border">
        {frontendRunActive ? (
          <div className="flex h-full min-h-64 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-6 w-6 animate-spin" />
            <span>{t("generating")}</span>
          </div>
        ) : sandpackFiles ? (
          <Sandpack
            template="react-ts"
            files={sandpackFiles}
            options={{
              activeFile: bundle?.entry || "/App.tsx",
              showTabs: true,
              showLineNumbers: true,
              editorHeight: 540,
              editorWidthPercentage: 45,
            }}
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
