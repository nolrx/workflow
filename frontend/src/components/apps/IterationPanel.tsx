/**
 * 二次开发 (secondary development) panel for a deployed app.
 *
 * Flow: state a change → impact analysis run → review the analysis + execution
 * plan → confirm → the requested lanes regenerate → deploy the new version. The
 * online version stays untouched until the user explicitly confirms and deploys.
 * Run-driven state is polled while an iteration is in an active phase.
 */
import { useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import { Loader2, Plus, Wand2, Rocket, ListChecks } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Textarea } from "@/components/ui/textarea"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  IterationStatusBadge,
  RiskBadge,
  RunStatusBadge,
} from "@/components/apps/badges"
import { useAppStore } from "@/stores/appStore"
import { useAgentStore } from "@/stores/agentStore"
import { fullstackApi } from "@/api/fullstack"
import type {
  AppIteration,
  ChangeType,
  ImpactScope,
  IterationAnalysis,
} from "@/api/apps"

const CHANGE_TYPES: ChangeType[] = [
  "bug_fix",
  "new_feature",
  "ui_change",
  "backend_logic",
  "data_model",
  "other",
]
const SCOPES: ImpactScope[] = [
  "frontend",
  "backend",
  "frontend_backend",
  "backend_middleware",
  "fullstack",
]
const ACTIVE_STATUSES = new Set([
  "analyzing",
  "generating",
  "staging_deploying",
])

/** The *_change analysis flags rendered as small chips. */
function analysisFlags(a: IterationAnalysis): { key: string; on: boolean }[] {
  return [
    { key: "requirement", on: !!a.requirement_change },
    { key: "ui", on: !!a.ui_change },
    { key: "frontend", on: !!a.frontend_change },
    { key: "backend", on: !!a.backend_change },
    { key: "middleware", on: !!a.middleware_change },
    { key: "contract", on: !!a.contract_change },
    { key: "database", on: !!a.database_change },
    { key: "assets", on: !!a.asset_generation_required },
  ]
}

export function IterationPanel({
  projectId,
  onSettled,
}: {
  projectId: string
  onSettled?: () => void
}) {
  const { t } = useTranslation("apps")
  const iterations = useAppStore((s) => s.iterations)
  const selectedId = useAppStore((s) => s.selectedIterationId)
  const selectIteration = useAppStore((s) => s.selectIteration)
  const createIteration = useAppStore((s) => s.createIteration)
  const confirmIteration = useAppStore((s) => s.confirmIteration)
  const refreshIteration = useAppStore((s) => s.refreshIteration)
  const isSubmitting = useAppStore((s) => s.isSubmitting)
  const openRun = useAgentStore((s) => s.openRun)
  const openPanel = useAgentStore((s) => s.openPanel)

  const [showForm, setShowForm] = useState(iterations.length === 0)
  const [instruction, setInstruction] = useState("")
  const [changeType, setChangeType] = useState<ChangeType>("new_feature")
  const [scopeOverride, setScopeOverride] = useState<string>("auto")
  const [allowContract, setAllowContract] = useState(false)
  const [allowDb, setAllowDb] = useState(false)
  const [deployToProd, setDeployToProd] = useState(true)
  const [deploying, setDeploying] = useState(false)

  const selected = useMemo(
    () => iterations.find((it) => it.id === selectedId) || null,
    [iterations, selectedId]
  )

  // Poll the selected iteration while it is in an active phase. Depend only on the
  // id + status (not the whole `selected` object) so a poll that returns a new
  // object reference with the same status doesn't churn the interval every cycle.
  const settledNotified = useRef<Set<string>>(new Set())
  const selectedActiveId = selected && ACTIVE_STATUSES.has(selected.status) ? selected.id : null
  useEffect(() => {
    if (!selectedActiveId) return
    const timer = setInterval(() => {
      void refreshIteration(projectId, selectedActiveId)
    }, 3000)
    return () => clearInterval(timer)
  }, [selectedActiveId, projectId, refreshIteration])

  // Notify the parent once an iteration reaches a terminal/deploy state so it can
  // refresh the app's deployment status.
  useEffect(() => {
    if (!selected) return
    if (
      (selected.status === "released" || selected.status === "failed") &&
      !settledNotified.current.has(selected.id)
    ) {
      settledNotified.current.add(selected.id)
      onSettled?.()
    }
  }, [selected, onSettled])

  const handleCreate = async () => {
    if (!instruction.trim()) {
      toast.error(t("iterate.instructionRequired"))
      return
    }
    const iteration = await createIteration(projectId, {
      instruction: instruction.trim(),
      change_type: changeType,
      impact_scope: scopeOverride === "auto" ? undefined : (scopeOverride as ImpactScope),
      allow_contract_change: allowContract,
      allow_db_change: allowDb,
      deploy_to_prod: deployToProd,
    })
    if (iteration) {
      toast.success(t("toast.iterationCreated"))
      setShowForm(false)
      setInstruction("")
    } else {
      toast.error(useAppStore.getState().error || t("toast.error"))
    }
  }

  const handleConfirm = async (it: AppIteration) => {
    const ok = await confirmIteration(projectId, it.id, {
      impact_scope: it.impact_scope || undefined,
      allow_contract_change: it.allow_contract_change,
      allow_db_change: it.allow_db_change,
    })
    if (ok) {
      toast.success(t("toast.iterationConfirmed"))
      void refreshIteration(projectId, it.id)
    } else {
      toast.error(useAppStore.getState().error || t("toast.error"))
    }
  }

  const handleDeploy = async (it: AppIteration) => {
    setDeploying(true)
    try {
      await fullstackApi.deploy(projectId, it.id)
      toast.success(t("toast.deployStarted"))
      await refreshIteration(projectId, it.id)
    } catch {
      toast.error(t("toast.error"))
    } finally {
      setDeploying(false)
    }
  }

  const replayRun = async (runId: string | null | undefined) => {
    if (!runId) return
    await openRun(runId)
    openPanel()
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="flex items-center gap-2 text-lg font-semibold">
            <Wand2 className="h-4 w-4" />
            {t("iterate.title")}
          </h3>
          <p className="text-sm text-muted-foreground">{t("iterate.subtitle")}</p>
        </div>
        <Button variant="outline" size="sm" onClick={() => setShowForm((v) => !v)}>
          <Plus className="mr-1 h-4 w-4" />
          {t("iterate.new")}
        </Button>
      </div>

      {/* New-iteration form */}
      {showForm && (
        <Card className="space-y-4 p-4">
          <div className="grid gap-2">
            <Label>{t("iterate.changeType")}</Label>
            <Select value={changeType} onValueChange={(v) => setChangeType(v as ChangeType)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CHANGE_TYPES.map((ct) => (
                  <SelectItem key={ct} value={ct}>
                    {t(`iterate.changeTypes.${ct}`)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="grid gap-2">
            <Label>{t("iterate.instruction")}</Label>
            <Textarea
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              placeholder={t("iterate.instructionPlaceholder")}
              rows={4}
            />
          </div>

          <div className="grid gap-2">
            <Label>{t("iterate.impactScope")}</Label>
            <Select value={scopeOverride} onValueChange={setScopeOverride}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">{t("iterate.impactScopeAuto")}</SelectItem>
                {SCOPES.map((s) => (
                  <SelectItem key={s} value={s}>
                    {t(`iterate.scopes.${s}`)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-wrap gap-4">
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={allowContract} onCheckedChange={(v) => setAllowContract(!!v)} />
              {t("iterate.allowContract")}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={allowDb} onCheckedChange={(v) => setAllowDb(!!v)} />
              {t("iterate.allowDb")}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={deployToProd} onCheckedChange={(v) => setDeployToProd(!!v)} />
              {t("iterate.deployToProd")}
            </label>
          </div>

          <Button onClick={handleCreate} disabled={isSubmitting}>
            {isSubmitting ? (
              <Loader2 className="mr-1 h-4 w-4 animate-spin" />
            ) : (
              <Wand2 className="mr-1 h-4 w-4" />
            )}
            {isSubmitting ? t("iterate.submitting") : t("iterate.submit")}
          </Button>
        </Card>
      )}

      {/* History + selected detail */}
      <div className="grid gap-4 lg:grid-cols-[260px_1fr]">
        <div className="space-y-1">
          <div className="px-1 text-xs font-medium uppercase text-muted-foreground">
            {t("iterate.history")}
          </div>
          {iterations.length === 0 ? (
            <p className="px-1 py-2 text-sm text-muted-foreground">{t("iterate.noHistory")}</p>
          ) : (
            iterations.map((it) => (
              <button
                key={it.id}
                onClick={() => selectIteration(it.id)}
                className={`w-full rounded-md border px-3 py-2 text-left text-sm transition-colors ${
                  it.id === selectedId
                    ? "border-primary bg-primary/5"
                    : "border-transparent hover:bg-accent"
                }`}
              >
                <div className="mb-1 flex items-center justify-between gap-2">
                  <IterationStatusBadge status={it.status} />
                  <RiskBadge risk={it.analysis?.risk_level} />
                </div>
                <div className="truncate text-muted-foreground" title={it.instruction}>
                  {it.analysis?.change_summary || it.instruction}
                </div>
              </button>
            ))
          )}
        </div>

        {selected ? (
          <IterationDetail
            iteration={selected}
            deploying={deploying}
            isSubmitting={isSubmitting}
            onConfirm={() => handleConfirm(selected)}
            onDeploy={() => handleDeploy(selected)}
            onReplay={replayRun}
          />
        ) : (
          <Card className="flex items-center justify-center p-8 text-sm text-muted-foreground">
            {t("iterate.history")}
          </Card>
        )}
      </div>
    </div>
  )
}

function IterationDetail({
  iteration,
  deploying,
  isSubmitting,
  onConfirm,
  onDeploy,
  onReplay,
}: {
  iteration: AppIteration
  deploying: boolean
  isSubmitting: boolean
  onConfirm: () => void
  onDeploy: () => void
  onReplay: (runId: string | null | undefined) => void
}) {
  const { t } = useTranslation("apps")
  const a = iteration.analysis || {}
  const plan = iteration.plan || {}
  const flags = analysisFlags(a).filter((f) => f.on)

  return (
    <Card className="space-y-4 p-4">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <IterationStatusBadge status={iteration.status} />
          <RiskBadge risk={a.risk_level} />
        </div>
        {iteration.analysis_run_id && (
          <Button variant="ghost" size="sm" onClick={() => onReplay(iteration.analysis_run_id)}>
            <ListChecks className="mr-1 h-4 w-4" />
            {t("iterate.viewRun")}
          </Button>
        )}
      </div>

      <div>
        <div className="text-sm font-medium">{a.change_summary || iteration.instruction}</div>
        <p className="mt-1 text-sm text-muted-foreground">{iteration.instruction}</p>
      </div>

      {iteration.status === "analyzing" && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          {t("iterationStatus.analyzing")}…
        </div>
      )}

      {/* Impact analysis */}
      {flags.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-medium uppercase text-muted-foreground">
            {t("iterate.changeFlags")}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {flags.map((f) => (
              <span
                key={f.key}
                className="rounded-md border bg-accent px-2 py-0.5 text-xs text-accent-foreground"
              >
                {t(`iterate.flags.${f.key}`)}
              </span>
            ))}
          </div>
        </div>
      )}

      {Array.isArray(a.reasoning) && a.reasoning.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-medium uppercase text-muted-foreground">
            {t("iterate.reasoning")}
          </div>
          <ul className="list-disc space-y-0.5 pl-5 text-sm text-muted-foreground">
            {a.reasoning.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Execution plan */}
      {Array.isArray(plan.steps) && plan.steps.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-medium uppercase text-muted-foreground">
            {t("iterate.steps")}
          </div>
          <ol className="space-y-1 text-sm">
            {plan.steps.map((s, i) => (
              <li key={i} className="flex gap-2">
                <span className="rounded bg-muted px-1.5 py-0.5 text-xs font-medium">
                  {t(`lanes.${s.lane}`, { defaultValue: s.lane })}
                </span>
                <span className="text-muted-foreground">{s.description}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {Array.isArray(plan.risks) && plan.risks.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-medium uppercase text-muted-foreground">
            {t("iterate.risks")}
          </div>
          <ul className="list-disc space-y-0.5 pl-5 text-sm text-amber-600 dark:text-amber-400">
            {plan.risks.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Confirm gate */}
      {iteration.status === "awaiting_plan_approval" && (
        <div className="space-y-2 border-t pt-3">
          {plan.requires_confirmation && (
            <p className="text-sm text-amber-600 dark:text-amber-400">{t("iterate.needsConfirm")}</p>
          )}
          <p className="text-xs text-muted-foreground">{t("iterate.confirmHint")}</p>
          <Button onClick={onConfirm} disabled={isSubmitting}>
            {isSubmitting ? (
              <Loader2 className="mr-1 h-4 w-4 animate-spin" />
            ) : (
              <Rocket className="mr-1 h-4 w-4" />
            )}
            {isSubmitting ? t("iterate.confirming") : t("iterate.confirm")}
          </Button>
        </div>
      )}

      {/* Generation progress */}
      {(iteration.status === "generating" ||
        iteration.status === "staging_deploying" ||
        iteration.status === "released") && (
        <div className="space-y-2 border-t pt-3">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            {(["frontend", "backend", "middleware", "deploy"] as const).map((lane) => {
              const ref = iteration.runs?.[lane]
              if (!ref) return null
              return (
                <button
                  key={lane}
                  onClick={() => onReplay(ref.id)}
                  className="flex items-center gap-1.5 hover:underline"
                >
                  <span className="text-muted-foreground">{t(`lanes.${lane}`)}</span>
                  <RunStatusBadge status={ref.status} />
                </button>
              )
            })}
          </div>

          {iteration.status === "generating" && !iteration.generation_ready && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t("iterate.generating")}
            </div>
          )}

          {iteration.status === "generating" && iteration.generation_ready && (
            <div className="space-y-2">
              <p className="text-sm text-emerald-600 dark:text-emerald-400">
                {t("iterate.generationReady")}
              </p>
              <Button onClick={onDeploy} disabled={deploying}>
                {deploying ? (
                  <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                ) : (
                  <Rocket className="mr-1 h-4 w-4" />
                )}
                {deploying ? t("iterate.deploying") : t("iterate.deploy")}
              </Button>
            </div>
          )}

          {iteration.status === "staging_deploying" && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t("iterate.deploying")}
            </div>
          )}

          {iteration.status === "released" && (
            <p className="text-sm font-medium text-emerald-600 dark:text-emerald-400">
              {t("iterate.released")}
            </p>
          )}
        </div>
      )}

      {iteration.status === "failed" && (
        <div className="border-t pt-3 text-sm text-destructive">
          {t("iterate.failed")}
          {iteration.error_message ? `：${iteration.error_message}` : ""}
        </div>
      )}
    </Card>
  )
}
