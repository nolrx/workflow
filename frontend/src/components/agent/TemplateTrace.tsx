import { useTranslation } from "react-i18next"
import { ExternalLink, GitBranch, PackageCheck, TriangleAlert } from "lucide-react"

import type { AgentEvent } from "@/api/agent"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

type TemplatePayload = {
  selected?: boolean
  lane?: string
  template_repo?: string
  template_path?: string
  template_name?: string
  score?: number
  files?: number
  warning?: string
}

function asTemplatePayload(event: AgentEvent): TemplatePayload | null {
  const payload = (event.payload || {}) as TemplatePayload
  if (
    !payload.template_repo &&
    !payload.template_path &&
    !payload.template_name &&
    typeof payload.files !== "number"
  ) {
    return null
  }
  return payload
}

function templateKey(event: AgentEvent): string {
  const payload = asTemplatePayload(event)
  return `${payload?.lane || ""}:${payload?.template_repo || ""}:${payload?.template_path || ""}`
}

function selectTemplateEvents(events: AgentEvent[]): AgentEvent[] {
  const byKey = new Map<string, AgentEvent>()
  for (const event of events) {
    const payload = asTemplatePayload(event)
    if (!payload) continue
    byKey.set(templateKey(event), event)
  }
  return [...byKey.values()].sort((a, b) => a.sequence - b.sequence)
}

export function TemplateTraceCard({
  event,
  compact = false,
}: {
  event: AgentEvent
  compact?: boolean
}) {
  const { t } = useTranslation("agent")
  const payload = asTemplatePayload(event)
  if (!payload) return null

  const selected = payload.selected !== false && !payload.warning
  const repo = payload.template_repo || ""
  const repoHref = /^https?:\/\//.test(repo) ? repo : ""
  const title = payload.template_name || payload.template_path || t("template.unknown")
  const lane = payload.lane ? t(`template.lane.${payload.lane}`, { defaultValue: payload.lane }) : ""

  return (
    <div
      className={cn(
        "rounded-md border bg-muted/35 text-xs",
        compact ? "mt-1 p-2" : "p-3"
      )}
    >
      <div className="flex min-w-0 items-start gap-2">
        {selected ? (
          <PackageCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
        ) : (
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
        )}
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <span className="truncate font-medium">{title}</span>
            {lane && <Badge variant="outline">{lane}</Badge>}
            <Badge variant={selected ? "secondary" : "outline"}>
              {selected ? t("template.selected") : t("template.fallback")}
            </Badge>
          </div>

          <div className="grid gap-1 text-[11px] text-muted-foreground">
            {repo && (
              <div className="flex min-w-0 items-center gap-1.5">
                <GitBranch className="h-3.5 w-3.5 shrink-0" />
                {repoHref ? (
                  <a
                    href={repoHref}
                    target="_blank"
                    rel="noreferrer"
                    className="min-w-0 truncate font-mono text-primary hover:underline"
                    title={repo}
                  >
                    {repo}
                  </a>
                ) : (
                  <span className="min-w-0 truncate font-mono" title={repo}>
                    {repo}
                  </span>
                )}
                {repoHref && <ExternalLink className="h-3 w-3 shrink-0 text-primary" />}
              </div>
            )}
            {payload.template_path && (
              <div className="min-w-0 truncate font-mono" title={payload.template_path}>
                {t("template.path")}: {payload.template_path}
              </div>
            )}
            <div className="flex flex-wrap gap-x-3 gap-y-1">
              {typeof payload.files === "number" && (
                <span>{t("template.files", { count: payload.files })}</span>
              )}
              {typeof payload.score === "number" && (
                <span>{t("template.score", { score: payload.score })}</span>
              )}
            </div>
            {payload.warning && (
              <div className="break-words text-amber-700 dark:text-amber-400">
                {payload.warning}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export function TemplateTraceList({
  events,
  compact = false,
}: {
  events: AgentEvent[]
  compact?: boolean
}) {
  const templateEvents = selectTemplateEvents(events)
  if (!templateEvents.length) return null
  return (
    <div className={cn("space-y-2", compact && "space-y-1.5")}>
      {templateEvents.map((event) => (
        <TemplateTraceCard key={event.id} event={event} compact={compact} />
      ))}
    </div>
  )
}
