import { useTranslation } from "react-i18next"
import {
  AlertCircle,
  CheckCircle2,
  Circle,
  Loader2,
  Maximize2,
  MinusCircle,
  X,
} from "lucide-react"
import type { AgentStepStatus } from "@/api/agent"
import { TemplateTraceList } from "@/components/agent/TemplateTrace"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import { cn } from "@/lib/utils"
import { useAgentStore } from "@/stores/agentStore"
import type { PreviewTab } from "@/components/code/previewTabs"
import { STEP_TAB } from "@/components/code/previewTabs"

function StatusIcon({ status }: { status: AgentStepStatus }) {
  switch (status) {
    case "running":
      return <Loader2 className="h-4 w-4 animate-spin text-primary" />
    case "completed":
      return <CheckCircle2 className="h-4 w-4 text-emerald-500" />
    case "failed":
      return <AlertCircle className="h-4 w-4 text-red-500" />
    case "skipped":
      return <MinusCircle className="h-4 w-4 text-muted-foreground" />
    default:
      return <Circle className="h-4 w-4 text-muted-foreground" />
  }
}

interface CodeAgentTimelineProps {
  onSelectTab: (tab: PreviewTab) => void
}

/**
 * Left-pane crewAI-style execution timeline. Reads the live agent run and lists
 * each agent step with its status; clicking a step focuses the matching preview
 * tab. The full prompt/response/artifact detail lives in the modal AgentRunPanel
 * opened via "view detail".
 */
export function CodeAgentTimeline({ onSelectTab }: CodeAgentTimelineProps) {
  const { t } = useTranslation("agent")
  const run = useAgentStore((state) => state.run)
  const events = useAgentStore((state) => state.events)
  const isStreaming = useAgentStore((state) => state.isStreaming)
  const selectedStepId = useAgentStore((state) => state.selectedStepId)
  const selectStep = useAgentStore((state) => state.selectStep)
  const openPanel = useAgentStore((state) => state.openPanel)
  const cancelRun = useAgentStore((state) => state.cancelRun)

  if (!run) {
    return (
      <p className="px-1 py-8 text-center text-sm text-muted-foreground">
        {t("swarm.idleHint")}
      </p>
    )
  }

  const steps = run.steps || []
  const progress = run.progress
  const progressValue = progress.total_steps
    ? Math.round((progress.completed_steps / progress.total_steps) * 100)
    : 0
  const isActive = run.status === "queued" || run.status === "running"

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <Progress value={progressValue} className="h-2 flex-1" />
        <span className="shrink-0 text-xs text-muted-foreground">
          {progress.completed_steps}/{progress.total_steps}
        </span>
      </div>

      <TemplateTraceList events={events} compact />

      <div className="space-y-1">
        {steps.map((step) => {
          const tab = STEP_TAB[step.agent_key]
          return (
            <button
              key={step.id}
              type="button"
              onClick={() => {
                selectStep(step.id)
                if (tab) onSelectTab(tab)
              }}
              className={cn(
                "flex w-full items-start gap-2.5 rounded-md border px-3 py-2 text-left transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
                selectedStepId === step.id
                  ? "border-primary bg-primary/10"
                  : "border-transparent hover:bg-muted"
              )}
            >
              <span className="mt-0.5">
                <StatusIcon status={step.status} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">{step.agent_name}</span>
                <span className="block truncate text-xs text-muted-foreground">
                  {step.output_summary ||
                    t(`status.${step.status}`, { defaultValue: step.status })}
                </span>
              </span>
            </button>
          )
        })}
        {steps.length === 0 && (
          <p className="px-1 py-4 text-center text-xs text-muted-foreground">
            {t("panel.waiting")}
          </p>
        )}
      </div>

      <div className="flex items-center gap-2 pt-1">
        <Button variant="outline" size="sm" onClick={openPanel}>
          <Maximize2 className="mr-1.5 h-3.5 w-3.5" />
          {t("panel.viewDetail")}
        </Button>
        {isActive && (
          <Button variant="ghost" size="sm" onClick={() => void cancelRun()}>
            <X className="mr-1.5 h-3.5 w-3.5" />
            {t("panel.cancel")}
          </Button>
        )}
        {isStreaming && (
          <Loader2 className="ml-auto h-4 w-4 animate-spin text-muted-foreground" />
        )}
      </div>
    </div>
  )
}

export default CodeAgentTimeline
