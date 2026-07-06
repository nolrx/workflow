/**
 * Dev Mode backlog planner panel (P1) — AI task drafts, user-confirmed.
 *
 * Flow: 生成任务列表 (one planner run) → draft appears here (grouped by
 * parent_feature_id, warnings surfaced) → the user edits/removes/reorders
 * tasks → 应用 folds it onto the task board through the same guarded bulk
 * path as tasks/bulk (409 = stale → offer 强制应用). While the plan is
 * `planning` the panel polls; the run itself streams on the left pane like
 * any other run.
 */
import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  Check,
  ChevronDown,
  ChevronRight,
  Image as ImageIcon,
  ListPlus,
  Loader2,
  Sparkles,
  Trash2,
  X,
} from "lucide-react"

import type { DevPlanTask } from "@/api/dev"
import { DevChecklistDialog } from "@/components/code/DevChecklistDialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { useDevStore } from "@/stores/devStore"

export function DevTaskPlannerPanel({ onPlanApplied }: { onPlanApplied?: () => void }) {
  const { t } = useTranslation("code")
  const taskPlan = useDevStore((s) => s.taskPlan)
  const plannerBusy = useDevStore((s) => s.plannerBusy)
  const startTaskPlanner = useDevStore((s) => s.startTaskPlanner)
  const refreshTaskPlan = useDevStore((s) => s.refreshTaskPlan)
  const loadLatestTaskPlan = useDevStore((s) => s.loadLatestTaskPlan)
  const editTaskPlan = useDevStore((s) => s.editTaskPlan)
  const applyTaskPlan = useDevStore((s) => s.applyTaskPlan)
  const rejectTaskPlan = useDevStore((s) => s.rejectTaskPlan)
  const session = useDevStore((s) => s.session)

  const [open, setOpen] = useState(true)
  const [instruction, setInstruction] = useState("")
  const [expanded, setExpanded] = useState<number | null>(null)

  // Reattach to a live draft after a refresh.
  useEffect(() => {
    if (session && !taskPlan) void loadLatestTaskPlan()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.id])

  // Poll while the planner run is generating (it is short: one model call).
  useEffect(() => {
    if (taskPlan?.status !== "planning" && taskPlan?.status !== "applying") return
    const timer = setInterval(() => void refreshTaskPlan(), 5000)
    return () => clearInterval(timer)
  }, [taskPlan?.status, refreshTaskPlan])

  const tasks: DevPlanTask[] = useMemo(
    () => taskPlan?.plan?.tasks ?? [],
    [taskPlan],
  )
  const groups = useMemo(() => {
    const by = new Map<string, { key: string; items: { task: DevPlanTask; idx: number }[] }>()
    tasks.forEach((task, idx) => {
      const key = task.parent_feature_id || t("dev.planner.ungrouped")
      if (!by.has(key)) by.set(key, { key, items: [] })
      by.get(key)!.items.push({ task, idx })
    })
    return [...by.values()]
  }, [tasks, t])

  const isDraft = taskPlan?.status === "draft"
  const isStale = taskPlan?.status === "stale"
  const showDraft = isDraft || isStale || taskPlan?.status === "applying"

  const mutateTasks = (fn: (list: DevPlanTask[]) => DevPlanTask[]) => {
    void editTaskPlan({ tasks: fn([...tasks]) })
  }
  const removeTask = (idx: number) => mutateTasks((list) => list.filter((_, i) => i !== idx))
  const moveTask = (idx: number, delta: number) =>
    mutateTasks((list) => {
      const j = idx + delta
      if (j < 0 || j >= list.length) return list
      const copy = [...list]
      ;[copy[idx], copy[j]] = [copy[j], copy[idx]]
      return copy
    })
  const patchTask = (idx: number, patch: Partial<DevPlanTask>) =>
    mutateTasks((list) => list.map((task, i) => (i === idx ? { ...task, ...patch } : task)))

  const apply = async (force = false) => {
    const ok = await applyTaskPlan({ force })
    if (ok) onPlanApplied?.()
  }

  return (
    <div className="rounded-xl border bg-card px-4 py-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Sparkles className="h-4 w-4 shrink-0 text-muted-foreground" />
          <span className="text-sm font-semibold">{t("dev.planner.title")}</span>
          {taskPlan ? (
            <span
              className={
                "rounded-full px-2 py-0.5 text-[11px] " +
                (isDraft
                  ? "bg-sky-500/10 text-sky-600 dark:text-sky-400"
                  : isStale || taskPlan.status === "failed"
                    ? "bg-red-500/10 text-red-600 dark:text-red-400"
                    : taskPlan.status === "applied"
                      ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                      : "bg-muted text-muted-foreground")
              }
            >
              {t(`dev.planner.status.${taskPlan.status}`)}
            </span>
          ) : null}
          {showDraft ? (
            <span className="text-[11px] text-muted-foreground">
              {t("dev.planner.taskCount", { count: tasks.length })}
            </span>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {/* 功能清单(任务板)——从右栏移到这里,点击弹框查看。 */}
          <DevChecklistDialog />
          <Button variant="ghost" size="sm" onClick={() => setOpen((v) => !v)}>
            {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          </Button>
        </div>
      </div>

      {open ? (
        <div className="mt-2 space-y-2">
          {/* Generate bar (idle / after terminal states) */}
          {!taskPlan || ["applied", "rejected", "failed"].includes(taskPlan.status) ? (
            <div className="flex items-center gap-2">
              <Input
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                placeholder={t("dev.planner.instructionPlaceholder")}
                className="h-9 flex-1 text-sm"
              />
              <Button
                size="sm"
                onClick={() => void startTaskPlanner({ instruction: instruction.trim() || undefined })}
                disabled={plannerBusy}
              >
                {plannerBusy ? (
                  <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                ) : (
                  <ListPlus className="mr-1 h-4 w-4" />
                )}
                {t("dev.planner.generate")}
              </Button>
            </div>
          ) : null}

          {taskPlan?.status === "planning" ? (
            <div className="flex items-center gap-2 rounded-md border bg-muted/30 px-3 py-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t("dev.planner.generating")}
            </div>
          ) : null}

          {taskPlan?.status === "failed" && taskPlan.error_message ? (
            <p className="text-xs text-destructive">{taskPlan.error_message}</p>
          ) : null}

          {showDraft ? (
            <>
              {taskPlan?.plan?.summary ? (
                <p className="text-xs text-muted-foreground">{taskPlan.plan.summary}</p>
              ) : null}
              {isStale ? (
                <p className="rounded-md bg-amber-500/10 px-2 py-1 text-xs text-amber-700 dark:text-amber-400">
                  {t("dev.planner.staleHint")}
                </p>
              ) : null}
              {(taskPlan?.warnings ?? []).length > 0 ? (
                <details className="text-xs text-muted-foreground">
                  <summary className="cursor-pointer">
                    {t("dev.planner.warnings", { count: taskPlan!.warnings.length })}
                  </summary>
                  <ul className="mt-1 list-inside list-disc space-y-0.5">
                    {taskPlan!.warnings.slice(0, 20).map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                </details>
              ) : null}

              {/* Draft tasks grouped by parent feature */}
              <div className="max-h-72 space-y-2 overflow-y-auto rounded-md border bg-muted/20 p-2">
                {groups.map((group) => (
                  <div key={group.key}>
                    <p className="px-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/70">
                      {group.key}
                    </p>
                    <ul className="mt-0.5 space-y-1">
                      {group.items.map(({ task, idx }) => (
                        <li key={`${task.feature_id}-${idx}`} className="rounded-md bg-background px-2 py-1.5">
                          <div className="flex items-start gap-2">
                            <button
                              type="button"
                              className="mt-0.5 shrink-0 text-muted-foreground hover:text-foreground"
                              onClick={() => setExpanded(expanded === idx ? null : idx)}
                            >
                              {expanded === idx ? (
                                <ChevronDown className="h-3.5 w-3.5" />
                              ) : (
                                <ChevronRight className="h-3.5 w-3.5" />
                              )}
                            </button>
                            <div className="min-w-0 flex-1">
                              <p className="break-words text-sm">
                                {task.category === "asset" ? (
                                  <ImageIcon className="mr-1 inline h-3.5 w-3.5 text-violet-500" />
                                ) : null}
                                {task.title}
                              </p>
                              <p className="text-[10px] text-muted-foreground/70">
                                {task.feature_id} · {task.lane}
                                {task.depends_on.length
                                  ? ` · ${t("dev.planner.deps")}: ${task.depends_on.join(", ")}`
                                  : ""}
                                {` · AC×${task.acceptance_criteria.length}`}
                                {task.category === "asset" && task.resource_spec?.outputs?.length
                                  ? ` · ${t("dev.planner.outputs", { count: task.resource_spec.outputs.length })}`
                                  : ""}
                              </p>
                            </div>
                            {isDraft ? (
                              <div className="flex shrink-0 items-center gap-1 text-muted-foreground">
                                <button type="button" title="↑" onClick={() => moveTask(idx, -1)}>
                                  <ChevronRight className="h-3.5 w-3.5 -rotate-90" />
                                </button>
                                <button type="button" title="↓" onClick={() => moveTask(idx, 1)}>
                                  <ChevronRight className="h-3.5 w-3.5 rotate-90" />
                                </button>
                                <button
                                  type="button"
                                  title={t("dev.planner.remove")}
                                  onClick={() => removeTask(idx)}
                                >
                                  <Trash2 className="h-3.5 w-3.5 hover:text-destructive" />
                                </button>
                              </div>
                            ) : null}
                          </div>
                          {expanded === idx ? (
                            <div className="mt-2 space-y-1.5 border-t pt-2">
                              {isDraft ? (
                                <>
                                  <Input
                                    defaultValue={task.title}
                                    className="h-7 text-xs"
                                    onBlur={(e) => {
                                      const v = e.target.value.trim()
                                      if (v && v !== task.title) patchTask(idx, { title: v })
                                    }}
                                  />
                                  <Textarea
                                    defaultValue={task.acceptance_criteria.join("\n")}
                                    rows={3}
                                    className="text-xs"
                                    placeholder={t("dev.planner.acPlaceholder")}
                                    onBlur={(e) => {
                                      const list = e.target.value
                                        .split("\n")
                                        .map((s) => s.trim())
                                        .filter(Boolean)
                                      patchTask(idx, { acceptance_criteria: list })
                                    }}
                                  />
                                </>
                              ) : (
                                <ul className="list-inside list-disc text-xs text-muted-foreground">
                                  {task.acceptance_criteria.map((c, i) => (
                                    <li key={i}>{c}</li>
                                  ))}
                                </ul>
                              )}
                              {task.resource_spec?.outputs?.length ? (
                                <ul className="text-[11px] text-muted-foreground">
                                  {task.resource_spec.outputs.map((o) => (
                                    <li key={o.path}>
                                      <ImageIcon className="mr-1 inline h-3 w-3" />
                                      {o.path}
                                      {o.size ? ` (${o.size})` : ""}
                                      {o.required === false ? ` · ${t("dev.planner.optional")}` : ""}
                                    </li>
                                  ))}
                                </ul>
                              ) : null}
                            </div>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>

              <div className="flex items-center justify-end gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void rejectTaskPlan()}
                  disabled={plannerBusy}
                >
                  <X className="mr-1 h-4 w-4" />
                  {t("dev.planner.reject")}
                </Button>
                {isStale ? (
                  <Button size="sm" onClick={() => void apply(true)} disabled={plannerBusy}>
                    <Check className="mr-1 h-4 w-4" />
                    {t("dev.planner.forceApply")}
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    onClick={() => void apply(false)}
                    disabled={plannerBusy || !isDraft || tasks.length === 0}
                  >
                    {plannerBusy ? (
                      <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                    ) : (
                      <Check className="mr-1 h-4 w-4" />
                    )}
                    {t("dev.planner.apply", { count: tasks.length })}
                  </Button>
                )}
              </div>
            </>
          ) : null}

          {taskPlan?.status === "applied" ? (
            <p className="text-xs text-emerald-600 dark:text-emerald-400">
              {t("dev.planner.appliedSummary", {
                inserted: taskPlan.inserted_count ?? 0,
                updated: taskPlan.updated_count ?? 0,
                skipped: taskPlan.skipped_count ?? 0,
              })}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
