/**
 * Dev Mode (交互式开发模式) — a Claude-Code-CLI-like workbench in the browser.
 *
 * Two columns: LEFT = requirement/dev conversation (drives bounded turn runs,
 * streamed via agentStore), RIGHT = the LIVE preview (an iframe onto the running
 * dev container's Vite server, with HMR) + the persistent functional checklist.
 *
 * The session + checklist live in devStore; each turn is a normal agent run so
 * cancel/继续 reuse agentStore.cancelRun / openRun. A "在浏览器打开" button opens
 * the same preview full-page in a new tab.
 */
import { useEffect, useMemo, useRef, useState } from "react"
import { useNavigate, useParams, useSearchParams } from "react-router-dom"
import { useTranslation } from "react-i18next"
import {
  ExternalLink,
  FileText,
  FlaskConical,
  GitBranch,
  Loader2,
  Power,
  RefreshCw,
  Send,
  Server,
  Square,
} from "lucide-react"

import type { DevBoard } from "@/api/dev"
import { tokenManager } from "@/api/client"
import { AppLayout } from "@/components/layout/AppLayout"
import { DevLogsDialog } from "@/components/code/DevLogsDialog"
import { DevSprintPanel } from "@/components/code/DevSprintPanel"
import { DevTaskPlannerPanel } from "@/components/code/DevTaskPlannerPanel"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { useAgentStore } from "@/stores/agentStore"
import { useCodeStore } from "@/stores/codeStore"
import { useDevStore } from "@/stores/devStore"

const FEED_TYPES = new Set([
  "progress",
  "file_created",
  "tool_call",
  "warning",
  "error",
  "step_completed",
  "dev_preview_ready",
  "checklist_updated",
])

export function CodeDevMode() {
  const { projectId } = useParams<{ projectId?: string }>()
  const { t } = useTranslation("code")
  const navigate = useNavigate()
  // ?start=plan (来自 Studio 的「生成项目」按钮 / App Space 的「开发模式」按钮):
  // 会话就绪后自动起草一份 Sprint 任务草案,让用户"点生成"即进入增量开发流程。
  const [searchParams, setSearchParams] = useSearchParams()

  const project = useCodeStore((s) => s.project)
  const loadProject = useCodeStore((s) => s.loadProject)

  const session = useDevStore((s) => s.session)
  const sprint = useDevStore((s) => s.sprint)
  const taskPlan = useDevStore((s) => s.taskPlan)
  const startTaskPlanner = useDevStore((s) => s.startTaskPlanner)
  const currentRunId = useDevStore((s) => s.currentRunId)
  const starting = useDevStore((s) => s.starting)
  const devError = useDevStore((s) => s.error)
  const startSession = useDevStore((s) => s.start)
  const sendTurn = useDevStore((s) => s.sendTurn)
  const sendParallelTurn = useDevStore((s) => s.sendParallelTurn)
  const stopSession = useDevStore((s) => s.stop)
  const applyBoard = useDevStore((s) => s.applyBoard)
  const resetDev = useDevStore((s) => s.reset)

  // Backend-lane (full-stack loop) selectors + actions.
  const backendSession = useDevStore((s) => s.backendSession)
  const backendBoard = useDevStore((s) => s.backendBoard)
  const backendStarting = useDevStore((s) => s.backendStarting)
  const startBackend = useDevStore((s) => s.startBackend)
  const sendBackendTurn = useDevStore((s) => s.sendBackendTurn)
  const runTests = useDevStore((s) => s.runTests)
  const stopBackend = useDevStore((s) => s.stopBackend)
  const applyBackendBoard = useDevStore((s) => s.applyBackendBoard)

  const run = useAgentStore((s) => s.run)
  const events = useAgentStore((s) => s.events)
  const isStreaming = useAgentStore((s) => s.isStreaming)
  const openRun = useAgentStore((s) => s.openRun)
  const cancelRun = useAgentStore((s) => s.cancelRun)
  const resetAgent = useAgentStore((s) => s.reset)

  const [input, setInput] = useState("")
  const [backendInput, setBackendInput] = useState("")
  const [manualReload, setManualReload] = useState(0)
  const [userMsgs, setUserMsgs] = useState<string[]>([])
  const [parallelMode, setParallelMode] = useState(false)
  const [backendLogsOpen, setBackendLogsOpen] = useState(false)
  const startedRef = useRef(false)
  const feedRef = useRef<HTMLDivElement>(null)

  // Load the project (title / status).
  useEffect(() => {
    if (projectId) void loadProject(projectId)
  }, [projectId, loadProject])

  // Start (or resume) the dev session once, and attach to the bootstrap run.
  useEffect(() => {
    if (!projectId || startedRef.current) return
    startedRef.current = true
    void (async () => {
      const runId = await startSession(projectId)
      if (runId) await openRun(runId)
    })()
    return () => {
      resetAgent()
      resetDev()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  // Auto-kick the backlog planner when arriving with ?start=plan (the Studio
  // 「生成项目」/ App Space「开发模式」entry). Fires ONCE, only when the session is
  // ready and there is no live plan/sprint yet; the URL flag is then cleared so a
  // refresh doesn't re-trigger it. The planner API doesn't need the container up,
  // so this can run while the dev server is still booting.
  const autoPlanRef = useRef(false)
  useEffect(() => {
    if (searchParams.get("start") !== "plan") return
    if (!session || autoPlanRef.current) return
    // Don't clobber an in-flight plan or a running sprint.
    const planActive =
      taskPlan && ["planning", "draft", "applying"].includes(taskPlan.status)
    const sprintActive =
      sprint && ["planned", "running", "pausing", "paused"].includes(sprint.status)
    autoPlanRef.current = true
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete("start")
      return next
    }, { replace: true })
    if (!planActive && !sprintActive) void startTaskPlanner()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, searchParams])

  // Attach agentStore whenever the active turn run changes.
  useEffect(() => {
    if (currentRunId && currentRunId !== run?.id) void openRun(currentRunId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentRunId])

  // Absorb live checklist boards pushed through the run stream (zustand action,
  // not React setState — safe inside an effect).
  const lastSeqRef = useRef(0)
  useEffect(() => {
    for (const ev of events) {
      if (ev.sequence <= lastSeqRef.current) continue
      lastSeqRef.current = ev.sequence
      if (ev.event_type === "checklist_updated") {
        const payload = ev.payload as { board?: DevBoard; lane?: string }
        if (payload?.board) {
          // Backend turns carry their own (separate) board — route it to the backend
          // panel so it never overwrites the frontend checklist.
          if (payload.lane === "backend") applyBackendBoard(payload.board)
          else applyBoard(payload.board)
        }
      }
    }
  }, [events, applyBoard, applyBackendBoard])

  // On turn completion, reconcile the board from the server (no React setState).
  const prevStatus = useRef<string | null>(null)
  useEffect(() => {
    if (run && run.status !== prevStatus.current) {
      prevStatus.current = run.status
      if (run.status === "completed" || run.status === "partial") {
        void useDevStore.getState().refresh()
        if (useDevStore.getState().backendSession) {
          void useDevStore.getState().refreshBackend()
        }
      }
    }
  }, [run])

  // Auto-scroll the activity feed.
  useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight })
  }, [events, userMsgs])

  const previewUrl = useMemo(() => {
    if (!projectId) return ""
    const token = tokenManager.getAccessToken() ?? ""
    return `/preview/${encodeURIComponent(projectId)}/?token=${encodeURIComponent(token)}`
  }, [projectId])

  // Derived iframe remount key: bumps when a turn completes or the dev server
  // (re)announces readiness — plus a manual-reload counter. Derived (not effect
  // setState) so it never triggers cascading renders.
  const previewVersion = useMemo(() => {
    let v = 0
    for (const ev of events) if (ev.event_type === "dev_preview_ready") v++
    if (run && (run.status === "completed" || run.status === "partial")) v++
    return `${run?.id ?? "none"}-${v}-${manualReload}`
  }, [events, run, manualReload])

  const feed = useMemo(() => events.filter((e) => FEED_TYPES.has(e.event_type)), [events])

  // After a page refresh the locally-typed user bubbles are gone (component state),
  // but we reattach to the in-flight/last turn run — surface its instruction (the run
  // title) as the initiating request so the reconnected conversation isn't headless.
  const resumedTitle = useMemo(() => {
    if (userMsgs.length > 0) return null
    return (run?.title || "").trim() || null
  }, [userMsgs.length, run?.title])

  const submit = async () => {
    const v = input.trim()
    if (!v || isStreaming) return
    if (parallelMode) {
      const lanes = v.split("\n").map((s) => s.trim()).filter(Boolean)
      if (lanes.length === 0) return
      setInput("")
      setUserMsgs((m) => [...m, ...lanes.map((l) => `⇉ ${l}`)])
      const runId = lanes.length > 1 ? await sendParallelTurn(lanes) : await sendTurn(lanes[0])
      if (runId) await openRun(runId)
      return
    }
    setInput("")
    setUserMsgs((m) => [...m, v])
    const runId = await sendTurn(v)
    if (runId) await openRun(runId)
  }

  const openInBrowser = () => {
    if (previewUrl) window.open(previewUrl, "_blank", "noopener,noreferrer")
  }

  const sessionStopped = session?.status === "stopped" || session?.status === "failed"

  // --- backend lane (full-stack loop) --------------------------------------
  const backendActive =
    !!backendSession && backendSession.status !== "stopped" && backendSession.status !== "failed"

  const enableBackend = async () => {
    const runId = await startBackend()
    if (runId) await openRun(runId)
  }

  const submitBackend = async () => {
    const v = backendInput.trim()
    if (!v || isStreaming || !backendActive) return
    setBackendInput("")
    setUserMsgs((m) => [...m, `⚙︎ ${v}`])
    const runId = await sendBackendTurn(v)
    if (runId) await openRun(runId)
  }

  const runBackendTests = async () => {
    if (isStreaming || !backendActive) return
    setUserMsgs((m) => [...m, t("dev.backend.runningTests")])
    const runId = await runTests()
    if (runId) await openRun(runId)
  }

  return (
    <AppLayout title={t("dev.title")}>
      <div className="flex h-[calc(100vh-8rem)] flex-col gap-3">
        {/* Header */}
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border bg-card px-4 py-2.5">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold">
              {t("dev.title")} · {project?.title || projectId}
            </h2>
            <p className="text-xs text-muted-foreground">
              {starting
                ? t("dev.starting")
                : sessionStopped
                  ? t("dev.stopped")
                  : t("dev.subtitle")}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => navigate(`/code/${projectId}`)}>
              {t("dev.backToStudio")}
            </Button>
            <Button variant="outline" size="sm" onClick={openInBrowser} disabled={!previewUrl}>
              <ExternalLink className="mr-1 h-4 w-4" />
              {t("dev.openInBrowser")}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void stopSession()}
              disabled={sessionStopped}
            >
              <Power className="mr-1 h-4 w-4" />
              {t("dev.stopSession")}
            </Button>
          </div>
        </div>

        {devError ? (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {devError}
          </div>
        ) : null}

        {/* Two columns */}
        <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-2">
          {/* LEFT: conversation */}
          <div className="flex min-h-0 flex-col overflow-hidden rounded-xl border bg-card">
            <div ref={feedRef} className="flex-1 space-y-2 overflow-y-auto p-3">
              {userMsgs.length === 0 && feed.length === 0 && !resumedTitle ? (
                <p className="px-2 py-8 text-center text-sm text-muted-foreground">
                  {t("dev.conversationEmpty")}
                </p>
              ) : null}
              {resumedTitle ? (
                <div className="flex justify-end">
                  <div className="max-w-[85%] rounded-lg bg-primary px-3 py-1.5 text-sm text-primary-foreground">
                    {resumedTitle}
                  </div>
                </div>
              ) : null}
              {userMsgs.map((m, i) => (
                <div key={`u-${i}`} className="flex justify-end">
                  <div className="max-w-[85%] rounded-lg bg-primary px-3 py-1.5 text-sm text-primary-foreground">
                    {m}
                  </div>
                </div>
              ))}
              {feed.map((ev) => (
                <div key={ev.id} className="flex justify-start">
                  <div
                    className={
                      "max-w-[92%] rounded-lg px-3 py-1.5 text-xs " +
                      (ev.event_type === "error"
                        ? "bg-destructive/10 text-destructive"
                        : ev.event_type === "warning"
                          ? "bg-amber-500/10 text-amber-700 dark:text-amber-400"
                          : "bg-muted text-foreground")
                    }
                  >
                    {ev.message || ev.event_type}
                  </div>
                </div>
              ))}
              {isStreaming ? (
                <div className="flex items-center gap-2 px-2 text-xs text-muted-foreground">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  {t("dev.working")}
                </div>
              ) : null}
            </div>

            {/* Input footer */}
            <div className="border-t p-2">
              <Textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void submit()
                }}
                placeholder={parallelMode ? t("dev.parallelPlaceholder") : t("dev.inputPlaceholder")}
                rows={parallelMode ? 4 : 2}
                disabled={sessionStopped}
                className="resize-none text-sm"
              />
              <div className="mt-2 flex items-center justify-between gap-2">
                <span className="min-w-0 truncate text-[11px] text-muted-foreground">
                  {parallelMode ? t("dev.parallelHint") : t("dev.inputHint")}
                </span>
                <div className="flex shrink-0 items-center gap-2">
                  <Button
                    variant={parallelMode ? "default" : "outline"}
                    size="sm"
                    title={t("dev.parallelToggleHint")}
                    onClick={() => setParallelMode((v) => !v)}
                  >
                    <GitBranch className="mr-1 h-4 w-4" />
                    {t("dev.parallelToggle")}
                  </Button>
                  {isStreaming ? (
                    <Button variant="destructive" size="sm" onClick={() => void cancelRun()}>
                      <Square className="mr-1 h-4 w-4" />
                      {t("dev.interrupt")}
                    </Button>
                  ) : null}
                  <Button
                    size="sm"
                    onClick={() => void submit()}
                    disabled={!input.trim() || isStreaming || sessionStopped}
                  >
                    <Send className="mr-1 h-4 w-4" />
                    {parallelMode ? t("dev.parallelSend") : t("dev.send")}
                  </Button>
                </div>
              </div>
            </div>
          </div>

          {/* RIGHT: live preview (full height — the功能清单 now opens on demand from
              the task-planning area via DevChecklistDialog). */}
          <div className="relative min-h-0 overflow-hidden rounded-xl border bg-white">
            <div className="absolute right-2 top-2 z-10">
              <Button
                variant="secondary"
                size="icon"
                className="h-7 w-7"
                title={t("dev.reloadPreview")}
                onClick={() => setManualReload((k) => k + 1)}
              >
                <RefreshCw className="h-3.5 w-3.5" />
              </Button>
            </div>
            {previewUrl ? (
              <iframe
                key={previewVersion}
                src={previewUrl}
                title={t("dev.previewTitle")}
                className="h-full w-full border-0"
                sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals"
              />
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                {t("dev.starting")}
              </div>
            )}
          </div>
        </div>

        {/* Backlog planner (P1): AI drafts the sprint task list from the project
            docs; the user confirms before anything touches the board. */}
        <DevTaskPlannerPanel onPlanApplied={() => void useDevStore.getState().refresh()} />

        {/* Sprint scheduler strip: serial task scheduling over the checklist
            backlog + the task state-machine log (streams the sprint run). */}
        <DevSprintPanel />

        {/* Backend co-dev strip (full-stack loop): the live frontend calls this
            backend dev container over /preview/<pid>/api. */}
        <div className="rounded-xl border bg-card px-4 py-2.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2">
              <Server className="h-4 w-4 shrink-0 text-muted-foreground" />
              <span className="text-sm font-semibold">{t("dev.backend.title")}</span>
              {backendActive ? (
                <span className="rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-600 dark:text-emerald-400">
                  {t("dev.backend.running")}
                  {backendSession?.health ? ` · ${backendSession.health}` : ""}
                </span>
              ) : (
                <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
                  {t("dev.backend.off")}
                </span>
              )}
              {backendActive && backendBoard ? (
                <span className="text-[11px] text-muted-foreground">
                  {t("dev.backend.progress", {
                    done: backendBoard.functional_done,
                    total: backendBoard.functional_total,
                  })}
                </span>
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {backendActive ? (
                <>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setBackendLogsOpen(true)}
                    title={t("dev.logs.hint")}
                  >
                    <FileText className="mr-1 h-4 w-4" />
                    {t("dev.logs.view")}
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void runBackendTests()}
                    disabled={isStreaming}
                    title={t("dev.backend.runTestsHint")}
                  >
                    <FlaskConical className="mr-1 h-4 w-4" />
                    {t("dev.backend.runTests")}
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => void stopBackend()}>
                    <Power className="mr-1 h-4 w-4" />
                    {t("dev.backend.stop")}
                  </Button>
                </>
              ) : (
                <Button size="sm" onClick={() => void enableBackend()} disabled={backendStarting}>
                  {backendStarting ? (
                    <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                  ) : (
                    <Server className="mr-1 h-4 w-4" />
                  )}
                  {t("dev.backend.enable")}
                </Button>
              )}
            </div>
          </div>
          {backendActive ? (
            <div className="mt-2 flex items-center gap-2">
              <input
                value={backendInput}
                onChange={(e) => setBackendInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault()
                    void submitBackend()
                  }
                }}
                placeholder={t("dev.backend.inputPlaceholder")}
                disabled={isStreaming}
                className="h-9 flex-1 rounded-md border bg-background px-3 text-sm outline-none focus:ring-1 focus:ring-ring"
              />
              <Button
                size="sm"
                onClick={() => void submitBackend()}
                disabled={!backendInput.trim() || isStreaming}
              >
                <Send className="mr-1 h-4 w-4" />
                {t("dev.send")}
              </Button>
            </div>
          ) : (
            <p className="mt-1 text-[11px] text-muted-foreground">{t("dev.backend.hint")}</p>
          )}
        </div>
      </div>

      {backendSession ? (
        <DevLogsDialog
          projectId={projectId ?? ""}
          sessionId={backendSession.id}
          open={backendLogsOpen}
          onOpenChange={setBackendLogsOpen}
          title={t("dev.logs.backendTitle")}
        />
      ) : null}
    </AppLayout>
  )
}
