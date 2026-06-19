import { useState } from "react"
import { useTranslation } from "react-i18next"
import { ChevronDown, Loader2 } from "lucide-react"

import { cn } from "@/lib/utils"
import { StreamingText } from "@/components/code/StreamingText"
import { DocumentSplitThinking } from "@/components/code/DocumentSplitThinking"

type LiveVariant = "text" | "thinking" | "spinner"

interface LiveStageCardProps {
  title: string
  variant: LiveVariant
  /** Live-streamed model text (only used by the "text" variant). */
  text?: string
}

/**
 * The currently-streaming step rendered inline in the conversation. It auto-opens
 * so the live output is visible by default ("expand to see the real-time
 * content"), but the user can collapse it to keep the transcript tidy. When the
 * step settles, the parent swaps this for the editable StageArtifactCard.
 */
export function LiveStageCard({ title, variant, text = "" }: LiveStageCardProps) {
  const { t } = useTranslation("code")
  const [open, setOpen] = useState(true)

  return (
    <div className="overflow-hidden rounded-xl border border-primary/40 bg-card shadow-sm">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className={cn(
          "flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-muted/60",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
        )}
      >
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
          <Loader2 className="h-4 w-4 animate-spin" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium text-foreground">{title}</span>
          <span className="block truncate text-xs text-muted-foreground">
            {t("conversation.liveLabel")}
          </span>
        </span>
        <ChevronDown
          className={cn(
            "h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-200",
            open && "rotate-180"
          )}
        />
      </button>
      {open && (
        <div className="border-t p-3 sm:p-4">
          {variant === "thinking" ? (
            <DocumentSplitThinking />
          ) : variant === "text" ? (
            <StreamingText text={text || t("workspace.streamingHint")} active />
          ) : (
            <span className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t("conversation.generating")}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

export default LiveStageCard
