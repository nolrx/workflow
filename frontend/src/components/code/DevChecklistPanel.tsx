/**
 * Dev Mode functional checklist board (右栏进度窗口).
 *
 * The persistent, user-visible/editable progress board — now rendering the FULL
 * sprint task state machine (pending → queued → in_progress → verifying → done,
 * plus blocked / failed / skipped / cancelled), retry counts, blocked reasons and
 * acceptance-criteria hints. Turns/sprints advance items live via
 * CHECKLIST_UPDATED events → devStore.applyBoard; the user can still toggle
 * items (scheduler-owned in-flight states are locked) and add their own.
 */
import { useState } from "react"
import { useTranslation } from "react-i18next"
import {
  Ban,
  Check,
  Circle,
  CircleDot,
  Clock,
  Image as ImageIcon,
  ListChecks,
  Loader2,
  OctagonAlert,
  Plus,
  XCircle,
} from "lucide-react"

import type { DevBoard, DevTask, DevTaskStatus } from "@/api/dev"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"

interface Props {
  board: DevBoard | null
  onToggle: (taskId: string, status: string) => void
  onAdd: (title: string) => void
}

/** Scheduler-owned in-flight states — the user must not toggle these. */
const LOCKED: ReadonlySet<DevTaskStatus> = new Set(["queued", "in_progress", "verifying"])

function StatusIcon({ status }: { status: DevTask["status"] }) {
  switch (status) {
    case "done":
      return <Check className="h-4 w-4 text-green-600" />
    case "queued":
      return <Clock className="h-4 w-4 text-amber-500" />
    case "in_progress":
      return <CircleDot className="h-4 w-4 text-amber-500" />
    case "verifying":
      return <Loader2 className="h-4 w-4 animate-spin text-sky-500" />
    case "blocked":
      return <OctagonAlert className="h-4 w-4 text-red-500" />
    case "failed":
      return <XCircle className="h-4 w-4 text-red-500" />
    case "cancelled":
      return <Ban className="h-4 w-4 text-muted-foreground/50" />
    case "skipped":
      return <Circle className="h-4 w-4 text-muted-foreground/40" />
    default:
      return <Circle className="h-4 w-4 text-muted-foreground" />
  }
}

export function DevChecklistPanel({ board, onToggle, onAdd }: Props) {
  const { t } = useTranslation("code")
  const [draft, setDraft] = useState("")

  const total = board?.functional_total ?? 0
  const done = board?.functional_done ?? 0
  const pct = total > 0 ? Math.round((done / total) * 100) : 0

  const submitAdd = () => {
    const v = draft.trim()
    if (!v) return
    onAdd(v)
    setDraft("")
  }

  // Toggle target: done ↔ pending; blocked/failed/cancelled → pending (manual
  // unblock + requeue, mirroring the backend's "pending clears the block").
  const toggleTarget = (task: DevTask) => (task.status === "done" ? "pending" : "done")
  const clickTitle = (task: DevTask) => {
    if (LOCKED.has(task.status)) return t(`dev.taskStatus.${task.status}`)
    if (task.status === "blocked" || task.status === "failed" || task.status === "cancelled")
      return t("dev.taskUnblock")
    return t("dev.toggleTask")
  }

  return (
    <div className="flex h-full flex-col">
      <div className="border-b px-4 py-3">
        <div className="mb-2 flex items-center justify-between text-sm font-medium">
          <span>{t("dev.checklistTitle")}</span>
          <span className="text-muted-foreground">
            {done}/{total}
          </span>
        </div>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
          <div
            className="h-full rounded-full bg-green-500 transition-all"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-2 py-2">
        {!board || board.items.length === 0 ? (
          <p className="px-2 py-6 text-center text-sm text-muted-foreground">
            {t("dev.checklistEmpty")}
          </p>
        ) : (
          <ul className="space-y-1">
            {board.items.map((task) => (
              <li
                key={task.id}
                className="flex items-start gap-2 rounded-md px-2 py-1.5 hover:bg-muted/60"
              >
                <button
                  type="button"
                  className="mt-0.5 shrink-0 disabled:cursor-not-allowed"
                  title={clickTitle(task)}
                  disabled={LOCKED.has(task.status)}
                  onClick={() =>
                    onToggle(
                      task.id,
                      task.status === "blocked" ||
                        task.status === "failed" ||
                        task.status === "cancelled"
                        ? "pending"
                        : toggleTarget(task),
                    )
                  }
                >
                  <StatusIcon status={task.status} />
                </button>
                <div className="min-w-0 flex-1">
                  <p
                    className={
                      "break-words text-sm " +
                      (task.status === "done"
                        ? "text-muted-foreground line-through"
                        : task.status === "cancelled" || task.status === "skipped"
                          ? "text-muted-foreground/70"
                          : "text-foreground")
                    }
                  >
                    {task.title}
                  </p>
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    {task.feature_id ? (
                      <span className="text-[10px] uppercase tracking-wide text-muted-foreground/70">
                        {task.feature_id}
                      </span>
                    ) : null}
                    {LOCKED.has(task.status) ||
                    task.status === "blocked" ||
                    task.status === "failed" ? (
                      <span
                        className={
                          "rounded-full px-1.5 text-[10px] " +
                          (task.status === "blocked" || task.status === "failed"
                            ? "bg-red-500/10 text-red-600 dark:text-red-400"
                            : "bg-amber-500/10 text-amber-600 dark:text-amber-400")
                        }
                      >
                        {t(`dev.taskStatus.${task.status}`)}
                      </span>
                    ) : null}
                    {task.retry_count > 0 ? (
                      <span className="rounded-full bg-muted px-1.5 text-[10px] text-muted-foreground">
                        {t("dev.taskRetry", { count: task.retry_count })}
                      </span>
                    ) : null}
                    {task.acceptance_criteria?.length ? (
                      <span
                        className="inline-flex items-center gap-0.5 text-[10px] text-muted-foreground/70"
                        title={task.acceptance_criteria.join("\n")}
                      >
                        <ListChecks className="h-3 w-3" />
                        {task.acceptance_criteria.length}
                      </span>
                    ) : null}
                    {task.category === "asset" && task.resource_spec?.outputs?.length ? (
                      <span
                        className={
                          "inline-flex items-center gap-0.5 rounded-full px-1.5 text-[10px] " +
                          (task.resource_spec.verified_outputs?.some((o) => o.exists)
                            ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                            : "bg-violet-500/10 text-violet-600 dark:text-violet-400")
                        }
                        title={task.resource_spec.outputs
                          .map(
                            (o) =>
                              `${o.path}${
                                task.resource_spec.verified_outputs?.find(
                                  (v) => v.path === o.path && v.exists,
                                )
                                  ? " ✓"
                                  : ""
                              }`,
                          )
                          .join("\n")}
                      >
                        <ImageIcon className="h-3 w-3" />
                        {task.resource_spec.verified_outputs?.filter((o) => o.exists).length ?? 0}/
                        {task.resource_spec.outputs.length}
                      </span>
                    ) : null}
                  </div>
                  {task.status === "blocked" && task.blocked_reason ? (
                    <p
                      className="mt-0.5 line-clamp-2 break-words text-[11px] text-red-500/90"
                      title={task.blocked_reason}
                    >
                      {task.blocked_reason}
                    </p>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="flex items-center gap-2 border-t px-3 py-2">
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submitAdd()
          }}
          placeholder={t("dev.addTaskPlaceholder")}
          className="h-8 text-sm"
        />
        <Button size="sm" variant="outline" onClick={submitAdd} disabled={!draft.trim()}>
          <Plus className="h-4 w-4" />
        </Button>
      </div>
    </div>
  )
}
