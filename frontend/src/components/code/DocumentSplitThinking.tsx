import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Check, Loader2, Sparkles } from "lucide-react"

import { cn } from "@/lib/utils"

const STEP_INTERVAL_MS = 1800

/**
 * "AI thinking" animation shown while the document-split agent is running.
 *
 * That step streams a raw JSON array token by token, which is noise to the
 * user, so its live preview shows an animated sequence of working stages
 * instead of the raw text. The progression is purely time-driven (the real
 * per-stage timing isn't known) and loops until the run lands the editable
 * documents and this view is replaced by the document editor.
 */
export function DocumentSplitThinking() {
  const { t } = useTranslation("code")
  const raw = t("workspace.splitThinking.steps", { returnObjects: true })
  const steps = Array.isArray(raw) ? (raw as string[]) : []
  const [tick, setTick] = useState(0)

  useEffect(() => {
    if (steps.length === 0) return
    const timer = setInterval(() => setTick((prev) => prev + 1), STEP_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [steps.length])

  const active = steps.length ? tick % steps.length : 0

  return (
    <div className="flex h-full min-h-48 flex-col gap-6 rounded-md border border-dashed p-6">
      <div className="flex items-center gap-3">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary/10">
          <Sparkles className="h-4 w-4 animate-pulse text-primary" />
        </span>
        <span className="flex items-center text-sm font-medium">
          {t("workspace.splitThinking.title")}
          <span className="ml-1 inline-flex items-end gap-0.5">
            <Dot delay="0ms" />
            <Dot delay="150ms" />
            <Dot delay="300ms" />
          </span>
        </span>
      </div>

      <ul className="space-y-3">
        {steps.map((label, index) => {
          const done = index < active
          const isActive = index === active
          return (
            <li
              key={index}
              className={cn(
                "flex items-center gap-3 text-sm transition-colors duration-500",
                isActive
                  ? "text-foreground"
                  : done
                    ? "text-muted-foreground"
                    : "text-muted-foreground/40"
              )}
            >
              <span className="flex h-5 w-5 shrink-0 items-center justify-center">
                {isActive ? (
                  <Loader2 className="h-4 w-4 animate-spin text-primary" />
                ) : done ? (
                  <Check className="h-4 w-4 text-emerald-500" />
                ) : (
                  <span className="h-1.5 w-1.5 rounded-full bg-current" />
                )}
              </span>
              <span className={cn("truncate", isActive && "font-medium")}>{label}</span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function Dot({ delay }: { delay: string }) {
  return (
    <span
      className="mb-0.5 h-1 w-1 animate-pulse rounded-full bg-current"
      style={{ animationDelay: delay }}
    />
  )
}

export default DocumentSplitThinking
