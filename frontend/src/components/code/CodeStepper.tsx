import { useTranslation } from "react-i18next"
import { Check } from "lucide-react"
import { cn } from "@/lib/utils"
import { useAgentStore } from "@/stores/agentStore"

/** The five user-facing stages of the Code workflow, in order. */
const DISPLAY_STAGES = ["requirements", "flow", "documents", "style", "app"] as const

/** Map any internal step / progress key onto its display-stage index. */
const STAGE_INDEX: Record<string, number> = {
  requirements: 0,
  flow: 1,
  documents: 2,
  style: 3,
  preview: 3,
  publish: 4,
  publisher: 4,
  app: 4,
  build: 4,
  critic: 4,
  repair: 4,
  done: 4,
}

/**
 * Horizontal stage progress for the conversational Code workspace. Derived from
 * the run's progress (the review_stage takes precedence while paused). Done
 * stages get a filled check, the active stage is outlined (with a pulse dot when
 * the run is paused awaiting confirmation), the rest are muted.
 */
export function CodeStepper() {
  const { t } = useTranslation("code")
  const run = useAgentStore((state) => state.run)
  const progress = run?.progress
  const allDone = run?.status === "completed" || run?.status === "partial"
  const paused = run?.status === "paused"
  const current =
    progress?.review_stage || progress?.current_step || (run ? "requirements" : null)
  const activeIdx = current ? STAGE_INDEX[current] ?? 0 : -1

  return (
    <ol className="flex items-center gap-1.5 sm:gap-2">
      {DISPLAY_STAGES.map((stage, index) => {
        const done = allDone || index < activeIdx
        const active = !allDone && index === activeIdx
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
                  !done && !active && "border-border text-muted-foreground"
                )}
              >
                {done ? <Check className="h-4 w-4" /> : index + 1}
                {active && paused && (
                  <span className="absolute -right-0.5 -top-0.5 h-2 w-2 animate-pulse rounded-full bg-primary" />
                )}
              </span>
              <span
                className={cn(
                  "hidden whitespace-nowrap text-xs font-medium sm:inline",
                  active ? "text-foreground" : "text-muted-foreground"
                )}
              >
                {t(`workspace.tabs.${stage}`)}
              </span>
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
