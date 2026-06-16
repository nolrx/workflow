import { memo } from "react"
import { cn } from "@/lib/utils"

interface StreamingTextProps {
  text: string
  /** When true, show a blinking caret at the end (model is still streaming). */
  active?: boolean
  className?: string
}

/**
 * Renders live-streamed model text with a blinking caret. Memoized because the
 * underlying string updates on every token delta and we don't want unrelated
 * re-renders to thrash.
 */
export const StreamingText = memo(function StreamingText({
  text,
  active = false,
  className,
}: StreamingTextProps) {
  return (
    <pre
      className={cn(
        "whitespace-pre-wrap break-words font-mono text-sm leading-relaxed text-foreground",
        className
      )}
    >
      {text}
      {active && (
        <span className="ml-0.5 inline-block h-4 w-[2px] translate-y-[3px] animate-pulse bg-primary" />
      )}
    </pre>
  )
})

export default StreamingText
