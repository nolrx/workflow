import { useState } from "react"
import { useTranslation } from "react-i18next"
import { AlertCircle, Check, Loader2, RotateCcw } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { useAgentStore } from "@/stores/agentStore"

/** The five user-facing stages of the Code workflow, in order. */
const DISPLAY_STAGES = ["requirements", "flow", "documents", "style", "app"] as const

/** Map any internal step / progress key onto its display-stage index. */
const STAGE_INDEX: Record<string, number> = {
  planner: 0,
  requirements: 0,
  flow: 1,
  documents: 2,
  style_select: 3,
  style: 3,
  preview: 3,
  publish: 4,
  publisher: 4,
  app: 4,
  build: 4,
  critic: 4,
  repair: 4,
  done: 4,
  // The frontend-project build is a separate run that also binds to the store;
  // its whole pipeline maps onto the "app" stage so a build failure surfaces here.
  fe_planner: 4,
  fe_project_build: 4,
  fe_publish: 4,
}

/**
 * Horizontal stage progress for the conversational Code workspace. Derived from
 * the run's progress (the review_stage takes precedence while paused). Done
 * stages get a filled check, the active stage is outlined (with a pulse dot when
 * the run is paused awaiting confirmation), the rest are muted. When a run fails,
 * the stage that failed turns red and offers a one-click retry that re-runs just
 * that stage (re-using everything earlier stages already produced).
 */
export function CodeStepper() {
  const { t } = useTranslation("code")
  const run = useAgentStore((state) => state.run)
  const retryRun = useAgentStore((state) => state.retryRun)
  const [retrying, setRetrying] = useState(false)

  const progress = run?.progress
  const status = run?.status
  const allDone = status === "completed" || status === "partial"
  const paused = status === "paused"

  const current =
    progress?.review_stage || progress?.current_step || (run ? "requirements" : null)
  const currentIdx = current ? STAGE_INDEX[current] ?? 0 : -1

  // A terminal-failed run surfaces the stage that failed (the step left `failed`)
  // as a red, retryable step. Gated on the run still being failed/partial so a
  // successful retry — which leaves the old failed step in history — stops showing
  // red. When the worker died before any step failed (e.g. a server restart marked
  // the run failed), fall back to the current stage so a retry is still offered.
  const failedStep =
    status === "failed" || status === "partial"
      ? run?.steps?.find((step) => step.status === "failed")
      : undefined
  let failedIdx = -1
  if (failedStep) failedIdx = STAGE_INDEX[failedStep.agent_key] ?? Math.max(currentIdx, 0)
  else if (status === "failed") failedIdx = Math.max(currentIdx, 0)

  // Anchor progress on the failed stage so the stages before it read as done.
  const activeIdx = failedIdx >= 0 ? failedIdx : currentIdx

  const handleRetry = async () => {
    if (retrying) return
    setRetrying(true)
    try {
      await retryRun(failedStep?.agent_key)
    } catch (err) {
      const message =
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("stepper.retryFailed")
      toast.error(message)
    } finally {
      setRetrying(false)
    }
  }

  return (
    <ol className="flex items-center gap-1.5 sm:gap-2">
      {DISPLAY_STAGES.map((stage, index) => {
        const isFailed = index === failedIdx
        const done = !isFailed && (allDone || index < activeIdx)
        const active = !allDone && !isFailed && index === activeIdx
        return (
          <li
            key={stage}
            className="flex flex-1 items-center gap-1.5 last:flex-none sm:gap-2"
          >
            <div className="flex items-center gap-2">
              <span
                className={cn(
                  "relative flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-xs font-medium transition-colors",
                  done && "border-primary bg-primary text-primary-foreground",
                  active && "border-primary text-primary",
                  isFailed && "border-destructive bg-destructive/10 text-destructive",
                  !done && !active && !isFailed && "border-border text-muted-foreground"
                )}
              >
                {isFailed ? (
                  <AlertCircle className="h-4 w-4" />
                ) : done ? (
                  <Check className="h-4 w-4" />
                ) : (
                  index + 1
                )}
                {active && paused && (
                  <span className="absolute -right-0.5 -top-0.5 h-2 w-2 animate-pulse rounded-full bg-primary" />
                )}
              </span>
              <span
                className={cn(
                  "hidden whitespace-nowrap text-xs font-medium sm:inline",
                  isFailed
                    ? "text-destructive"
                    : active
                      ? "text-foreground"
                      : "text-muted-foreground"
                )}
              >
                {t(`workspace.tabs.${stage}`)}
              </span>
              {isFailed && (
                <button
                  type="button"
                  onClick={handleRetry}
                  disabled={retrying}
                  title={t("stepper.retry")}
                  className="inline-flex shrink-0 items-center gap-1 rounded-md border border-destructive/40 px-1.5 py-0.5 text-[11px] font-medium text-destructive transition-colors hover:bg-destructive/10 disabled:opacity-60"
                >
                  {retrying ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <RotateCcw className="h-3 w-3" />
                  )}
                  <span className="hidden sm:inline">{t("stepper.retry")}</span>
                </button>
              )}
            </div>
            {index < DISPLAY_STAGES.length - 1 && (
              <span
                className={cn("h-px flex-1 transition-colors", done ? "bg-primary" : "bg-border")}
              />
            )}
          </li>
        )
      })}
    </ol>
  )
}

export default CodeStepper
