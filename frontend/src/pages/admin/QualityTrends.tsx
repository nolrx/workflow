import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import { AppLayout } from "@/components/layout/AppLayout"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { getQualityTrends, type QualityTrends as Trends } from "@/api/quality"

const WINDOWS = [7, 30, 90, 0]
const METRIC_KEYS = [
  "count",
  "passRate",
  "meanScore",
  "meanRounds",
  "degradedRate",
  "featurePassRate",
] as const

const pct = (x: number | null | undefined) => (x == null ? "—" : `${Math.round(x * 100)}%`)
const num = (x: number | null | undefined) => (x == null ? "—" : x.toFixed(2))

/** Admin dashboard over the generation-quality samples (eval framework, P0-B):
 *  success rate / rubric score / repair rounds / degraded rate, by lane and day. */
export function QualityTrends() {
  const { t } = useTranslation("admin")
  const [trends, setTrends] = useState<Trends | null>(null)
  const [loading, setLoading] = useState(true)
  const [lane, setLane] = useState("")
  const [windowDays, setWindowDays] = useState(30)
  const [scopeAll, setScopeAll] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setTrends(await getQualityTrends({ lane: lane || undefined, windowDays, scopeAll }))
    } catch {
      toast.error(t("quality.loadError"))
    } finally {
      setLoading(false)
    }
  }, [lane, windowDays, scopeAll, t])

  useEffect(() => {
    // Standard fetch-on-filter-change with a loading flag; the synchronous setLoading
    // is intentional (show the spinner immediately), not a cascading-render bug.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load()
  }, [load])

  const laneLabel = (ln: string) => {
    const map: Record<string, string> = {
      frontend: "quality.filters.frontend",
      backend: "quality.filters.backend",
      full_generation: "quality.filters.full_generation",
    }
    return map[ln] ? t(map[ln]) : ln
  }

  const o = trends?.overall
  const byDay = trends?.by_day ?? []
  const metricValue = (key: string): string => {
    if (!o) return "—"
    switch (key) {
      case "count":
        return String(o.count)
      case "passRate":
        return pct(o.pass_rate)
      case "meanScore":
        return num(o.mean_weighted_score)
      case "meanRounds":
        return num(o.mean_verify_rounds)
      case "degradedRate":
        return pct(o.degraded_rate)
      case "featurePassRate":
        return pct(o.feature_pass_rate)
      default:
        return "—"
    }
  }

  return (
    <AppLayout title={t("quality.title")}>
      <div className="space-y-6">
        <p className="text-sm text-muted-foreground">{t("quality.subtitle")}</p>

        {/* filters */}
        <div className="flex flex-wrap items-center gap-3">
          <Select
            value={lane || "__all__"}
            onValueChange={(v) => setLane(v === "__all__" ? "" : v)}
          >
            <SelectTrigger className="w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all__">{t("quality.filters.allLanes")}</SelectItem>
              <SelectItem value="frontend">{t("quality.filters.frontend")}</SelectItem>
              <SelectItem value="backend">{t("quality.filters.backend")}</SelectItem>
              <SelectItem value="full_generation">
                {t("quality.filters.full_generation")}
              </SelectItem>
            </SelectContent>
          </Select>
          <Select value={String(windowDays)} onValueChange={(v) => setWindowDays(Number(v))}>
            <SelectTrigger className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {WINDOWS.map((w) => (
                <SelectItem key={w} value={String(w)}>
                  {w === 0 ? t("quality.filters.allTime") : t("quality.filters.days", { n: w })}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            variant={scopeAll ? "default" : "outline"}
            size="sm"
            onClick={() => setScopeAll((s) => !s)}
          >
            {scopeAll ? t("quality.filters.scopeAll") : t("quality.filters.scopeOwn")}
          </Button>
        </div>

        {loading ? (
          <p className="text-sm text-muted-foreground">{t("loading")}</p>
        ) : !o || o.count === 0 ? (
          <Card className="p-8 text-center text-sm text-muted-foreground">
            {t("quality.empty")}
          </Card>
        ) : (
          <>
            {/* headline metrics */}
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
              {METRIC_KEYS.map((key) => (
                <Card key={key} className="p-4">
                  <div className="text-2xl font-semibold tabular-nums">{metricValue(key)}</div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    {t(`quality.metrics.${key}`)}
                  </div>
                </Card>
              ))}
            </div>

            {/* by-day trend (bar height = pass rate) */}
            {byDay.length > 0 && (
              <Card className="p-4">
                <div className="mb-3 text-sm font-medium">{t("quality.byDay")}</div>
                <div className="flex items-end gap-2 overflow-x-auto pb-1">
                  {byDay.map((d) => (
                    <div
                      key={d.day}
                      className="flex w-10 shrink-0 flex-col items-center gap-1"
                      title={`${d.day} · ${t("quality.metrics.passRate")} ${pct(d.pass_rate)} · ${t("quality.metrics.count")} ${d.count}`}
                    >
                      <div className="flex h-28 w-full items-end rounded bg-muted/40">
                        <div
                          className="w-full rounded bg-primary/70"
                          style={{ height: `${Math.round((d.pass_rate ?? 0) * 100)}%` }}
                        />
                      </div>
                      <div className="text-[10px] text-muted-foreground">{d.day.slice(5)}</div>
                      <div className="text-[10px] tabular-nums text-muted-foreground">{d.count}</div>
                    </div>
                  ))}
                </div>
              </Card>
            )}

            <div className="grid gap-4 lg:grid-cols-2">
              {/* by lane */}
              <Card className="p-4">
                <div className="mb-3 text-sm font-medium">{t("quality.byLane")}</div>
                <div className="space-y-2">
                  {Object.entries(trends?.by_lane ?? {}).map(([ln, b]) => (
                    <div key={ln} className="flex items-center justify-between gap-3 text-sm">
                      <span className="font-medium">{laneLabel(ln)}</span>
                      <span className="text-muted-foreground">
                        {t("quality.metrics.count")} {b.count} · {t("quality.metrics.passRate")}{" "}
                        {pct(b.pass_rate)} · {t("quality.metrics.meanScore")}{" "}
                        {num(b.mean_weighted_score)}
                      </span>
                    </div>
                  ))}
                </div>
              </Card>

              {/* verdict histogram */}
              <Card className="p-4">
                <div className="mb-3 text-sm font-medium">{t("quality.verdicts")}</div>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(o.verdicts ?? {}).map(([v, n]) => (
                    <Badge key={v} variant="outline" className="tabular-nums">
                      {v}: {n}
                    </Badge>
                  ))}
                </div>
              </Card>
            </div>
          </>
        )}
      </div>
    </AppLayout>
  )
}
