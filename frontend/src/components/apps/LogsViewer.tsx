/**
 * Read-only runtime log viewer (应用运行日志) for a deployed app — tails the
 * deployed container's stdout/stderr via the backend (`docker logs`). No exec.
 */
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Loader2, RefreshCw } from "lucide-react"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { appsApi } from "@/api/apps"

const TAIL_OPTIONS = [100, 200, 500, 1000]

export function LogsViewer({ projectId }: { projectId: string }) {
  const { t } = useTranslation("apps")
  const [tail, setTail] = useState(200)
  const [logs, setLogs] = useState<string>("")
  const [available, setAvailable] = useState(true)
  const [loading, setLoading] = useState(true)
  // Bumped by the refresh button to re-trigger the fetch effect without calling a
  // state-setting function directly inside an effect (set-state-in-effect lint).
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let alive = true
    const run = async () => {
      setLoading(true)
      try {
        const res = await appsApi.logs(projectId, tail)
        if (alive) {
          setLogs(res.logs || "")
          setAvailable(res.available)
        }
      } finally {
        if (alive) setLoading(false)
      }
    }
    void run()
    return () => {
      alive = false
    }
  }, [projectId, tail, reloadKey])

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <Select value={String(tail)} onValueChange={(v) => setTail(Number(v))}>
          <SelectTrigger className="w-[130px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {TAIL_OPTIONS.map((n) => (
              <SelectItem key={n} value={String(n)}>
                {t("logs.tail", { n })}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button variant="outline" size="sm" onClick={() => setReloadKey((k) => k + 1)} disabled={loading}>
          {loading ? (
            <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="mr-1 h-3.5 w-3.5" />
          )}
          {t("logs.refresh")}
        </Button>
      </div>

      <Card className="p-0">
        {loading && !logs ? (
          <div className="flex justify-center py-12 text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />
          </div>
        ) : !available ? (
          <p className="py-12 text-center text-sm text-muted-foreground">{t("logs.unavailable")}</p>
        ) : !logs.trim() ? (
          <p className="py-12 text-center text-sm text-muted-foreground">{t("logs.empty")}</p>
        ) : (
          <pre className="max-h-[60vh] overflow-auto p-3 text-xs leading-relaxed">
            <code>{logs}</code>
          </pre>
        )}
      </Card>
    </div>
  )
}
