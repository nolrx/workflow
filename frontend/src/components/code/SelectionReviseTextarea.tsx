import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { useTranslation } from "react-i18next"
import { Loader2, Sparkles, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

const PANEL_WIDTH = 300
const DEFAULT_DWELL_MS = 3000
const CHANGED_HIGHLIGHT_MS = 6000

// Shared box metrics for the textarea and its highlight backdrop so they wrap
// text identically — the basis of "highlight ranges inside a textarea" (a native
// textarea exposes no per-range DOM rect, so a mirror layer renders the marks).
const BASE =
  "w-full rounded-md border px-3 py-2 text-sm leading-relaxed whitespace-pre-wrap break-words"

// Computed-style props mirrored onto the hidden caret-measuring div.
const MIRROR_PROPS = [
  "boxSizing",
  "width",
  "height",
  "overflowX",
  "overflowY",
  "borderTopWidth",
  "borderRightWidth",
  "borderBottomWidth",
  "borderLeftWidth",
  "paddingTop",
  "paddingRight",
  "paddingBottom",
  "paddingLeft",
  "fontStyle",
  "fontVariant",
  "fontWeight",
  "fontStretch",
  "fontSize",
  "lineHeight",
  "fontFamily",
  "textAlign",
  "textTransform",
  "textIndent",
  "letterSpacing",
  "wordSpacing",
  "tabSize",
] as const

/** Caret coordinates (relative to the textarea border box) at ``position``. */
function getCaretCoordinates(el: HTMLTextAreaElement, position: number) {
  const computed = window.getComputedStyle(el)
  const div = document.createElement("div")
  div.style.position = "absolute"
  div.style.visibility = "hidden"
  div.style.whiteSpace = "pre-wrap"
  div.style.wordWrap = "break-word"
  div.style.top = "0"
  div.style.left = "0"
  for (const prop of MIRROR_PROPS) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    div.style[prop as any] = computed[prop as any]
  }
  div.style.height = "auto"
  div.textContent = el.value.slice(0, position)
  const span = document.createElement("span")
  span.textContent = el.value.slice(position) || "."
  div.appendChild(span)
  document.body.appendChild(div)
  const top = span.offsetTop + parseInt(computed.borderTopWidth || "0", 10)
  const left = span.offsetLeft + parseInt(computed.borderLeftWidth || "0", 10)
  const height = parseInt(computed.lineHeight || "0", 10) || parseInt(computed.fontSize || "16", 10)
  document.body.removeChild(div)
  return { top, left, height }
}

/** Viewport (x, y) just below the caret at ``position`` (x clamped to viewport). */
function caretAnchor(el: HTMLTextAreaElement, position: number) {
  const coords = getCaretCoordinates(el, position)
  const rect = el.getBoundingClientRect()
  const x = Math.min(
    Math.max(8, rect.left + coords.left - el.scrollLeft + 4),
    Math.max(8, window.innerWidth - PANEL_WIDTH - 8)
  )
  const y = rect.top + coords.top - el.scrollTop + coords.height + 6
  return { x, y }
}

interface Range {
  start: number
  end: number
}

interface OpenState extends Range {
  x: number
  y: number
  selectedText: string
}

export interface SelectionReviseTextareaProps {
  value: string
  onChange: (event: React.ChangeEvent<HTMLTextAreaElement>) => void
  /**
   * Apply the revision for the selected span (asynchronous). Resolves with the
   * changed range so the new content can be highlighted, or ``ok: false`` on
   * failure. The selected span is locked (read-only) while this is in flight.
   */
  onReviseSelection: (args: {
    selectedText: string
    instruction: string
    start: number
    end: number
  }) => Promise<{ ok: boolean; change?: Range | null }>
  rows?: number
  className?: string
  placeholder?: string
  disabled?: boolean
  /** Dwell (ms) the selection must stay still before the input floats up. */
  dwellMs?: number
}

/** Split text into [before, <mark>highlighted</mark>, after] for the backdrop. */
function renderHighlight(value: string, range: Range | null, markClass: string) {
  if (!range || range.end <= range.start) return value
  const start = Math.max(0, Math.min(range.start, value.length))
  const end = Math.max(start, Math.min(range.end, value.length))
  return (
    <>
      {value.slice(0, start)}
      <mark className={markClass}>{value.slice(start, end)}</mark>
      {value.slice(end)}
    </>
  )
}

/**
 * A document textarea with inline, asynchronous "revise this part" editing:
 * select text, hold ~3s, and a small floating input rises near the selection.
 * Submitting sends only the selected span (with the whole doc as server-side
 * context) to the model, which rewrites just that span — without blocking the
 * rest of the workspace. The selected span is highlighted while pending, and the
 * rewritten span is highlighted (green, briefly) once applied. Highlights render
 * on a mirror backdrop since a native textarea can't style sub-ranges.
 */
export function SelectionReviseTextarea({
  value,
  onChange,
  onReviseSelection,
  rows,
  className,
  placeholder,
  disabled,
  dwellMs = DEFAULT_DWELL_MS,
}: SelectionReviseTextareaProps) {
  const { t } = useTranslation("code")
  const [open, setOpen] = useState<OpenState | null>(null)
  const [instruction, setInstruction] = useState("")
  const [busy, setBusy] = useState(false)
  const [pendingRange, setPendingRange] = useState<Range | null>(null)
  const [changedRange, setChangedRange] = useState<Range | null>(null)
  const [statusPos, setStatusPos] = useState<{ x: number; y: number } | null>(null)

  const taRef = useRef<HTMLTextAreaElement | null>(null)
  const backdropRef = useRef<HTMLDivElement | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const dwellRef = useRef<number | null>(null)
  const changedTimerRef = useRef<number | null>(null)

  const clearDwell = () => {
    if (dwellRef.current !== null) {
      window.clearTimeout(dwellRef.current)
      dwellRef.current = null
    }
  }
  const clearChangedTimer = () => {
    if (changedTimerRef.current !== null) {
      window.clearTimeout(changedTimerRef.current)
      changedTimerRef.current = null
    }
  }

  const closePopover = () => {
    setOpen(null)
    setInstruction("")
  }

  useEffect(
    () => () => {
      clearDwell()
      clearChangedTimer()
    },
    []
  )

  // Keep the backdrop scrolled in lockstep with the textarea.
  const syncScroll = () => {
    const ta = taRef.current
    const bd = backdropRef.current
    if (ta && bd) {
      bd.scrollTop = ta.scrollTop
      bd.scrollLeft = ta.scrollLeft
    }
  }
  useLayoutEffect(syncScroll)

  const scheduleDwell = () => {
    clearDwell()
    const el = taRef.current
    if (!el || open || busy || disabled) return
    const start = el.selectionStart ?? 0
    const end = el.selectionEnd ?? 0
    if (end <= start) return
    const selectedText = el.value.slice(start, end)
    if (!selectedText.trim()) return
    dwellRef.current = window.setTimeout(() => {
      dwellRef.current = null
      const node = taRef.current
      if (!node || node.selectionStart !== start || node.selectionEnd !== end) return
      const { x, y } = caretAnchor(node, end)
      setChangedRange(null) // a fresh selection supersedes a prior change highlight
      clearChangedTimer()
      setOpen({ x, y, start, end, selectedText })
    }, dwellMs)
  }

  // While the panel (or pending chip) is anchored, follow ancestor/window scroll
  // and resize so it never drifts away from its selection; if the textarea is
  // gone, dismiss. (A textarea's own onScroll can't catch the conversation rail
  // scrolling, so listen on the window in the capture phase.)
  useEffect(() => {
    if (!open && !busy) return
    const reposition = () => {
      const el = taRef.current
      if (!el) {
        closePopover()
        return
      }
      const end = open ? open.end : pendingRange?.end
      if (end == null) return
      const { x, y } = caretAnchor(el, end)
      if (open) setOpen((o) => (o ? { ...o, x, y } : o))
      else setStatusPos({ x, y })
    }
    window.addEventListener("scroll", reposition, true)
    window.addEventListener("resize", reposition)
    return () => {
      window.removeEventListener("scroll", reposition, true)
      window.removeEventListener("resize", reposition)
    }
  }, [open, busy, pendingRange])

  // Dismiss the open panel on outside click or Escape.
  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(event.target as Node)) closePopover()
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closePopover()
    }
    document.addEventListener("mousedown", onDown)
    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("mousedown", onDown)
      document.removeEventListener("keydown", onKey)
    }
  }, [open])

  // Clamp the panel within the viewport using its measured height (flip/clamp up
  // when it would overflow the bottom), instead of guessing the height.
  useLayoutEffect(() => {
    if (!open || !panelRef.current) return
    const height = panelRef.current.offsetHeight
    const maxY = window.innerHeight - height - 8
    if (open.y > maxY) {
      setOpen((o) => (o ? { ...o, y: Math.max(8, maxY) } : o))
    }
  }, [open, instruction])

  const submit = async () => {
    const text = instruction.trim()
    if (!text || !open) return
    const args = { selectedText: open.selectedText, instruction: text, start: open.start, end: open.end }
    setPendingRange({ start: open.start, end: open.end })
    setStatusPos({ x: open.x, y: open.y })
    setBusy(true)
    closePopover()
    const result = await onReviseSelection(args)
    setBusy(false)
    setPendingRange(null)
    setStatusPos(null)
    if (result.ok && result.change) {
      setChangedRange(result.change)
      clearChangedTimer()
      changedTimerRef.current = window.setTimeout(() => setChangedRange(null), CHANGED_HIGHLIGHT_MS)
    }
  }

  const handleChange = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    // A manual edit invalidates a stale "changed" highlight (offsets shift).
    if (changedRange) {
      setChangedRange(null)
      clearChangedTimer()
    }
    onChange(event)
  }

  const amberRange = pendingRange ?? (open ? { start: open.start, end: open.end } : null)
  const highlight = changedRange
    ? { range: changedRange, cls: "rounded-[2px] bg-emerald-300/45 text-transparent" }
    : amberRange
      ? { range: amberRange, cls: "rounded-[2px] bg-amber-300/50 text-transparent" }
      : null

  return (
    <div className="relative">
      <div
        ref={backdropRef}
        aria-hidden
        className={cn(
          BASE,
          "pointer-events-none absolute inset-0 z-0 select-none overflow-hidden border-transparent text-transparent",
          className
        )}
      >
        {renderHighlight(value, highlight?.range ?? null, highlight?.cls ?? "")}
        {"\n"}
      </div>
      <textarea
        ref={taRef}
        value={value}
        onChange={handleChange}
        rows={rows}
        placeholder={placeholder}
        disabled={disabled}
        readOnly={busy}
        spellCheck={false}
        onSelect={scheduleDwell}
        onMouseUp={scheduleDwell}
        onKeyUp={scheduleDwell}
        onScroll={syncScroll}
        onBlur={clearDwell}
        className={cn(
          BASE,
          "relative z-[1] block min-h-[60px] resize-y border-input bg-transparent text-foreground shadow-sm",
          "placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
          "disabled:cursor-not-allowed disabled:opacity-50",
          className
        )}
      />

      {open &&
        createPortal(
          <div
            ref={panelRef}
            style={{ position: "fixed", left: open.x, top: open.y, width: PANEL_WIDTH, zIndex: 60 }}
            className="max-h-[80vh] overflow-auto rounded-lg border bg-background p-3 text-foreground shadow-lg"
          >
            <div className="mb-2 flex items-start justify-between gap-2">
              <span className="flex items-center gap-1.5 text-xs font-medium text-primary">
                <Sparkles className="h-3.5 w-3.5" />
                {t("partialRevise.hint", { count: open.selectedText.trim().length })}
              </span>
              <button
                type="button"
                onClick={closePopover}
                aria-label={t("partialRevise.cancel")}
                className={cn(
                  "rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                )}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            <textarea
              autoFocus
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              placeholder={t("partialRevise.placeholder")}
              rows={3}
              className={cn(
                "w-full resize-y rounded-md border border-input bg-transparent px-2.5 py-1.5 text-sm shadow-sm",
                "placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              )}
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                  event.preventDefault()
                  void submit()
                }
              }}
            />
            <div className="mt-2 flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={closePopover}>
                {t("partialRevise.cancel")}
              </Button>
              <Button size="sm" onClick={() => void submit()} disabled={!instruction.trim()}>
                <Sparkles className="mr-1.5 h-3.5 w-3.5" />
                {t("partialRevise.submit")}
              </Button>
            </div>
          </div>,
          document.body
        )}

      {busy &&
        statusPos &&
        createPortal(
          <div
            style={{ position: "fixed", left: statusPos.x, top: statusPos.y, zIndex: 60 }}
            className="pointer-events-none flex items-center gap-1.5 rounded-md border bg-background px-2.5 py-1 text-xs text-muted-foreground shadow-md"
          >
            <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
            {t("partialRevise.applying")}
          </div>,
          document.body
        )}
    </div>
  )
}

export default SelectionReviseTextarea
