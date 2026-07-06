/**
 * Dev container logs dialog.
 *
 * Shows the FULL merged stdout+stderr of a dev container (all log types: dependency
 * install, dev-server / hot-reload output, the app's own runtime logging incl. access
 * logs + stack traces, the retry-proxy). Read-only, timestamped, auto-refreshing while
 * open. Lane-agnostic — used by the backend co-dev strip (and reusable for frontend).
 */
import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { Loader2, RefreshCw } from "lucide-react"

import { devApi } from "@/api/dev"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

interface Props {
  projectId: string
  sessionId: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
  title?: string
}

const TAIL_OPTIONS = [200, 500, 1000, 2000]

export function DevLogsDialog({ projectId, sessionId, open, onOpenChange, title }: Props) {
  const { t } = useTranslation("code")
  const [logs, setLogs] = useState("")
  const [loading, setLoading] = useState(false)
  const [tail, setTail] = useState(500)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const preRef = useRef<HTMLPreElement>(null)
  const atBottomRef = useRef(true)

  // Async fetch: sets state only AFTER the await (never synchronously in an effect).
  const fetchLogs = useCallback(async () => {
    if (!sessionId) return
    try {
      const res = await devApi.getLogs(projectId, sessionId, tail)
      setLogs(res.logs || "")
      setError(res.available ? null : t("dev.logs.unavailable"))
    } catch (e) {
      setError(e instanceof Error ? e.message : t("dev.logs.error"))
    }
  }, [projectId, sessionId, tail, t])

  // Manual refresh (event handler → showing the spinner synchronously is fine here).
  const manualRefresh = () => {
    setLoading(true)
    void fetchLogs().finally(() => setLoading(false))
  }

  // Fetch on open + whenever the tail changes. fetchLogs only setState after its
  // await (async), so this is a legitimate data-fetch-on-open, not a cascading render.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (open) void fetchLogs()
  }, [open, tail, fetchLogs])

  // Auto-refresh while open (only when the user hasn't scrolled up to read history).
  useEffect(() => {
    if (!open || !autoRefresh) return
    const id = setInterval(() => {
      if (atBottomRef.current) void fetchLogs()
    }, 3000)
    return () => clearInterval(id)
  }, [open, autoRefresh, fetchLogs])

  // Keep the view pinned to the newest line unless the user scrolled up.
  useEffect(() => {
    const el = preRef.current
    if (el && atBottomRef.current) el.scrollTop = el.scrollHeight
  }, [logs])

  const onScroll = () => {
    const el = preRef.current
    if (!el) return
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85vh] max-w-4xl flex-col gap-3">
        <DialogHeader>
          <DialogTitle>{title || t("dev.logs.title")}</DialogTitle>
        </DialogHeader>

        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">{t("dev.logs.tail")}</span>
          <select
            value={tail}
            onChange={(e) => setTail(Number(e.target.value))}
            className="h-8 rounded-md border bg-background px-2 text-xs outline-none"
          >
            {TAIL_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            {t("dev.logs.autoRefresh")}
          </label>
          <div className="ml-auto flex items-center gap-2">
            {loading ? <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /> : null}
            <Button variant="outline" size="sm" onClick={manualRefresh} disabled={loading}>
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
              {t("dev.logs.refresh")}
            </Button>
          </div>
        </div>

        {error ? (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-700 dark:text-amber-400">
            {error}
          </div>
        ) : null}

        <pre
          ref={preRef}
          onScroll={onScroll}
          className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words rounded-md bg-zinc-950 p-3 font-mono text-[11px] leading-relaxed text-zinc-100"
        >
          {logs || (loading ? "" : t("dev.logs.empty"))}
        </pre>
      </DialogContent>
    </Dialog>
  )
}
