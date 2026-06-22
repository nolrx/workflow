/**
 * Shared visual wrapper for every canvas node: a titled card whose border
 * reflects selection and live run status. Specific node components supply the
 * icon, accent, body, and handles.
 */
import type { ReactNode } from "react"

import { cn } from "@/lib/utils"
import { useCanvasStore } from "@/stores/canvasStore"

export type NodeRunStatus = "running" | "completed" | "failed" | "skipped" | undefined

const STATUS_RING: Record<NonNullable<NodeRunStatus>, string> = {
  running: "ring-2 ring-blue-400 animate-pulse",
  completed: "ring-2 ring-emerald-400",
  failed: "ring-2 ring-red-400",
  skipped: "ring-2 ring-muted-foreground/40 opacity-60",
}

interface NodeShellProps {
  nodeId: string
  title: string
  icon: ReactNode
  accent?: string
  selected?: boolean
  subtitle?: string
  children?: ReactNode
}

export function NodeShell({
  nodeId,
  title,
  icon,
  accent = "bg-slate-100 text-slate-600",
  selected,
  subtitle,
  children,
}: NodeShellProps) {
  const status = useCanvasStore((s) => s.nodeRunStatus[nodeId])
  return (
    <div
      className={cn(
        "w-56 rounded-lg border bg-card shadow-sm transition",
        selected ? "border-primary" : "border-border",
        status ? STATUS_RING[status] : ""
      )}
    >
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <span className={cn("flex h-6 w-6 items-center justify-center rounded", accent)}>
          {icon}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">{title}</div>
          {subtitle ? (
            <div className="truncate text-xs text-muted-foreground">{subtitle}</div>
          ) : null}
        </div>
      </div>
      {children ? <div className="px-3 py-2 text-xs text-muted-foreground">{children}</div> : null}
    </div>
  )
}
