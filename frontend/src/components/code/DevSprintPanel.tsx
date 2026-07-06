/**
 * Dev Mode sprint strip — the serial task scheduler's control surface + the
 * task state-machine log (状态机日志).
 *
 * The sprint's orchestrating run has its OWN event stream (separate from the
 * per-turn stream agentStore renders), so this panel opens a lightweight SSE
 * reader on `sprint.run_id`: the server replays the stored events (past turns'
 * transitions) then pushes live ones. Board/sprint snapshots carried on
 * CHECKLIST_UPDATED events are folded into devStore so the checklist and the
 * strip stay live; every narrated event becomes a log line.
 */
import { useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  Ban,
  ChevronDown,
  ChevronRight,
  ListTodo,
  Loader2,
  Pause,
  Play,
  Rocket,
} from "lucide-react"

import { AGENT_API_BASE } from "@/api/agent"
import type { DevBoard, DevSprint, DevSprintStatus } from "@/api/dev"
import { tokenManager } from "@/api/client"
import { Button } from "@/components/ui/button"
import { useDevStore } from "@/stores/devStore"

interface LogEntry {
  seq: number
  time: string
  level: "info" | "warning" | "error"
  message: string
}

const STATUS_STYLE: Record<DevSprintStatus, string> = {
  planned: "bg-muted text-muted-foreground",
  running: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  pausing: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
  paused: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
  completed: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  blocked: "bg-red-500/10 text-red-600 dark:text-red-400",
  failed: "bg-red-500/10 text-red-600 dark:text-red-400",
  cancelled: "bg-muted text-muted-foreground",
}

const ACTIVE: ReadonlySet<DevSprintStatus> = new Set(["planned", "running", "pausing"])
const LOG_TYPES = new Set([
  "run_started",
  "step_started",
  "step_completed",
  "progress",
  "warning",
  "error",
  "checklist_updated",
  "run_completed",
])
const MAX_LOG = 300

interface StreamEvent {
  sequence: number
  event_type: string
  level?: string
  message?: string
  created_at?: string
  payload?: { board?: DevBoard; sprint?: DevSprint | null }
}

export function DevSprintPanel() {
  const { t } = useTranslation("code")
  const sprint = useDevStore((s) => s.sprint)
  const board = useDevStore((s) => s.board)
  const sprintBusy = useDevStore((s) => s.sprintBusy)
  const startSprint = useDevStore((s) => s.startSprint)
  const pauseSprint = useDevStore((s) => s.pauseSprint)
  const resumeSprint = useDevStore((s) => s.resumeSprint)
  const cancelSprint = useDevStore((s) => s.cancelSprint)
  const refreshSprint = useDevStore((s) => s.refreshSprint)
  const applyBoard = useDevStore((s) => s.applyBoard)
  const applySprint = useDevStore((s) => s.applySprint)

  const [logOpen, setLogOpen] = useState(true)
  const [log, setLog] = useState<LogEntry[]>([])
  const logRef = useRef<HTMLDivElement>(null)
  const runIdRef = useRef<string | null>(null)

  const pendingCount = useMemo(
    () => (board?.items ?? []).filter((task) => task.status === "pending").length,
    [board],
  )
  const isActive = !!sprint && ACTIVE.has(sprint.status)

  // --- the state-machine log stream (replay + live) --------------------------
  useEffect(() => {
    const runId = sprint?.run_id ?? null
    if (!runId || runId === runIdRef.current) return
    runIdRef.current = runId
    setLog([])
    const abort = new AbortController()

    void (async () => {
      const token = tokenManager.getAccessToken()
      try {
        const response = await fetch(`${AGENT_API_BASE}/agent/runs/${runId}/stream`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
          signal: abort.signal,
        })
        if (!response.ok || !response.body) return
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ""
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          let boundary = buffer.indexOf("\n\n")
          while (boundary !== -1) {
            const block = buffer.slice(0, boundary)
            buffer = buffer.slice(boundary + 2)
            boundary = buffer.indexOf("\n\n")
            let eventType = "message"
            let dataStr = ""
            for (const line of block.split("\n")) {
              if (line.startsWith(":")) continue
              if (line.startsWith("event:")) eventType = line.slice(6).trim()
              else if (line.startsWith("data:")) dataStr += line.slice(5).trim()
            }
            if (eventType === "done") {
              void refreshSprint()
              return
            }
            if (!dataStr || eventType === "agent_delta") continue
            try {
              const ev = JSON.parse(dataStr) as StreamEvent
              if (ev.payload?.board) applyBoard(ev.payload.board)
              if (ev.payload?.sprint) applySprint(ev.payload.sprint)
              if (LOG_TYPES.has(ev.event_type) && ev.message) {
                const entry: LogEntry = {
                  seq: ev.sequence,
                  time: ev.created_at
                    ? new Date(ev.created_at).toLocaleTimeString()
                    : new Date().toLocaleTimeString(),
                  level:
                    ev.event_type === "error"
                      ? "error"
                      : ev.event_type === "warning" || ev.level === "warning"
                        ? "warning"
                        : "info",
                  message: ev.message,
                }
                setLog((prev) => {
                  if (prev.some((l) => l.seq === entry.seq)) return prev
                  const next = [...prev, entry].sort((a, b) => a.seq - b.seq)
                  return next.length > MAX_LOG ? next.slice(next.length - MAX_LOG) : next
                })
              }
              if (ev.event_type === "run_completed") {
                void refreshSprint()
                return
              }
            } catch {
              /* malformed chunk — the periodic refresh keeps state correct */
            }
          }
        }
      } catch {
        /* aborted / transient — the poll below reconciles */
      }
    })()

    return () => {
      abort.abort()
      runIdRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sprint?.run_id])

  // Belt-and-braces poll while active (SSE drop / page in background).
  useEffect(() => {
    if (!isActive) return
    const timer = setInterval(() => void refreshSprint(), 30_000)
    return () => clearInterval(timer)
  }, [isActive, refreshSprint])

  // Auto-scroll the log.
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight })
  }, [log, logOpen])

  const snap = sprint?.progress_snapshot ?? {}
  const statusChip = sprint ? (
    <span className={`rounded-full px-2 py-0.5 text-[11px] ${STATUS_STYLE[sprint.status]}`}>
      {t(`dev.sprint.status.${sprint.status}`)}
    </span>
  ) : (
    <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
      {t("dev.sprint.off")}
    </span>
  )

  return (
    <div className="rounded-xl border bg-card px-4 py-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <ListTodo className="h-4 w-4 shrink-0 text-muted-foreground" />
          <span className="text-sm font-semibold">{t("dev.sprint.title")}</span>
          {statusChip}
          {sprint ? (
            <span className="text-[11px] text-muted-foreground">
              {t("dev.sprint.progress", {
                turns: sprint.turn_count,
                max: sprint.max_turns ?? "∞",
                done: snap.done ?? board?.functional_done ?? 0,
                total: snap.total ?? board?.total ?? 0,
              })}
            </span>
          ) : null}
          {sprint?.status === "blocked" && snap.reason ? (
            <span
              className="max-w-[360px] truncate text-[11px] text-red-500/90"
              title={snap.reason}
            >
              {t("dev.sprint.reason", { reason: snap.reason })}
            </span>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setLogOpen((v) => !v)}
            title={t("dev.sprint.logHint")}
          >
            {logOpen ? (
              <ChevronDown className="mr-1 h-4 w-4" />
            ) : (
              <ChevronRight className="mr-1 h-4 w-4" />
            )}
            {t("dev.sprint.log")}
          </Button>
          {isActive ? (
            <>
              {sprint!.status === "running" || sprint!.status === "planned" ? (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void pauseSprint()}
                  disabled={sprintBusy}
                >
                  <Pause className="mr-1 h-4 w-4" />
                  {t("dev.sprint.pause")}
                </Button>
              ) : (
                <Button variant="outline" size="sm" disabled>
                  <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                  {t("dev.sprint.status.pausing")}
                </Button>
              )}
              <Button
                variant="destructive"
                size="sm"
                onClick={() => void cancelSprint()}
                disabled={sprintBusy}
              >
                <Ban className="mr-1 h-4 w-4" />
                {t("dev.sprint.cancel")}
              </Button>
            </>
          ) : sprint?.status === "paused" ? (
            <>
              <Button size="sm" onClick={() => void resumeSprint()} disabled={sprintBusy}>
                <Play className="mr-1 h-4 w-4" />
                {t("dev.sprint.resume")}
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={() => void cancelSprint()}
                disabled={sprintBusy}
              >
                <Ban className="mr-1 h-4 w-4" />
                {t("dev.sprint.cancel")}
              </Button>
            </>
          ) : (
            <Button
              size="sm"
              onClick={() => void startSprint()}
              disabled={sprintBusy || pendingCount === 0}
              title={pendingCount === 0 ? t("dev.sprint.noPending") : t("dev.sprint.startHint")}
            >
              {sprintBusy ? (
                <Loader2 className="mr-1 h-4 w-4 animate-spin" />
              ) : (
                <Rocket className="mr-1 h-4 w-4" />
              )}
              {t("dev.sprint.start", { count: pendingCount })}
            </Button>
          )}
        </div>
      </div>

      {logOpen ? (
        <div
          ref={logRef}
          className="mt-2 max-h-44 overflow-y-auto rounded-md border bg-muted/30 px-3 py-2 font-mono text-[11px] leading-5"
        >
          {log.length === 0 ? (
            <p className="text-muted-foreground">{t("dev.sprint.logEmpty")}</p>
          ) : (
            log.map((entry) => (
              <div
                key={entry.seq}
                className={
                  entry.level === "error"
                    ? "text-destructive"
                    : entry.level === "warning"
                      ? "text-amber-600 dark:text-amber-400"
                      : "text-foreground/80"
                }
              >
                <span className="mr-2 text-muted-foreground/60">{entry.time}</span>
                {entry.message}
              </div>
            ))
          )}
        </div>
      ) : null}
    </div>
  )
}
