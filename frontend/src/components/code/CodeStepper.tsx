import { useState } from "react"
import { useTranslation } from "react-i18next"
import { AlertCircle, Check, Loader2, RotateCcw } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { useAgentStore } from "@/stores/agentStore"
import { DISPLAY_STAGES, deriveStageNav, type DisplayStage } from "@/components/code/stages"

interface CodeStepperProps {
  /** The stage window currently shown. When provided the stepper acts as a tab bar. */
  viewStage?: DisplayStage
  /** Switch the visible stage window (only reachable stages are clickable). */
  onSelect?: (stage: DisplayStage) => void
}

/**
 * Horizontal stage navigation for the windowed Code workspace. It doubles as the
 * progress indicator (done = filled check, the live stage outlined with a pulse
 * dot while paused, a failed stage in red with a one-click per-stage retry) and
 * as the tab bar: clicking a reached stage switches the conversation to that
 * stage's window. The stage being viewed gets a ring so it reads apart from the
 * live stage — the two are independent now (you can review a done stage while a
 * later one is mid-generation).
 */
export function CodeStepper({ viewStage, onSelect }: CodeStepperProps) {
  const { t } = useTranslation("code")
  const run = useAgentStore((state) => state.run)
  const retryRun = useAgentStore((state) => state.retryRun)
  const [retrying, setRetrying] = useState(false)

  const nav = deriveStageNav(run)
  const { activeIdx, failedIdx, failedStepKey, maxReachedIdx, allDone, paused } = nav
  const viewIdx = viewStage ? DISPLAY_STAGES.indexOf(viewStage) : -1

  const handleRetry = async () => {
    if (retrying) return
    setRetrying(true)
    try {
      await retryRun(failedStepKey)
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
        const viewing = index === viewIdx
        const reachable = index <= maxReachedIdx && !!onSelect
        const label = t(`workspace.tabs.${stage}`)
        return (
          <li
            key={stage}
            className="flex flex-1 items-center gap-1.5 last:flex-none sm:gap-2"
          >
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => reachable && onSelect?.(stage)}
                disabled={!reachable}
                aria-current={viewing ? "step" : undefined}
                aria-label={t("stepper.view", { stage: label })}
                title={reachable ? t("stepper.view", { stage: label }) : undefined}
                className={cn(
                  // -my-1 py-1 widens the tap target without growing the row.
                  "group -my-1 flex items-center gap-2 rounded-full py-1 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1",
                  reachable ? "cursor-pointer" : "cursor-default"
                )}
              >
                <span
                  className={cn(
                    "relative flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-xs font-medium transition-all",
                    done && "border-primary bg-primary text-primary-foreground",
                    active && "border-primary text-primary",
                    isFailed && "border-destructive bg-destructive/10 text-destructive",
                    !done && !active && !isFailed && "border-border text-muted-foreground",
                    viewing && "ring-2 ring-primary ring-offset-2 ring-offset-card",
                    reachable && !viewing && "group-hover:border-primary"
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
                    "hidden whitespace-nowrap text-xs transition-colors sm:inline",
                    viewing
                      ? "font-semibold text-foreground"
                      : "font-medium",
                    !viewing && isFailed
                      ? "text-destructive"
                      : !viewing && active
                        ? "text-foreground"
                        : !viewing && "text-muted-foreground"
                  )}
                >
                  {label}
                </span>
              </button>
              {isFailed && (
                <button
                  type="button"
                  onClick={handleRetry}
                  disabled={retrying}
                  title={t("stepper.retry")}
                  className="inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-md border border-destructive/40 px-1.5 py-0.5 text-[11px] font-medium text-destructive transition-colors hover:bg-destructive/10 disabled:opacity-60"
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
