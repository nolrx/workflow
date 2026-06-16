import { useEffect, useRef } from "react"
import { useTranslation } from "react-i18next"
import {
  CheckCircle2,
  Circle,
  Loader2,
  MinusCircle,
  X,
  XCircle,
} from "lucide-react"
import { toast } from "sonner"

import type { AgentArtifact, AgentRunStatus, AgentStepStatus } from "@/api/agent"
import { agentApi } from "@/api/agent"
import { ArtifactViewer } from "@/components/agent/ArtifactViewer"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent } from "@/components/ui/dialog"
import { Progress } from "@/components/ui/progress"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { cn } from "@/lib/utils"
import { selectCurrentStep, useAgentStore } from "@/stores/agentStore"

const RUN_STATUS_VARIANT: Record<
  AgentRunStatus,
  "default" | "secondary" | "destructive" | "outline"
> = {
  queued: "secondary",
  running: "secondary",
  completed: "default",
  partial: "secondary",
  failed: "destructive",
  cancelled: "outline",
}

function StepStatusIcon({ status }: { status: AgentStepStatus }) {
  switch (status) {
    case "running":
      return <Loader2 className="h-4 w-4 animate-spin text-primary" />
    case "completed":
      return <CheckCircle2 className="h-4 w-4 text-emerald-500" />
    case "failed":
      return <XCircle className="h-4 w-4 text-red-500" />
    case "skipped":
      return <MinusCircle className="h-4 w-4 text-muted-foreground" />
    default:
      return <Circle className="h-4 w-4 text-muted-foreground" />
  }
}

const LEVEL_DOT: Record<string, string> = {
  info: "bg-sky-500",
  warning: "bg-amber-500",
  error: "bg-red-500",
}

function formatTime(iso: string | null): string {
  if (!iso) return ""
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ""
  return date.toLocaleTimeString()
}

function Field({ label, value }: { label: string; value: string | null | undefined }) {
  if (!value) return null
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">{value}</p>
    </div>
  )
}

export function AgentRunPanel() {
  const { t } = useTranslation("agent")
  const run = useAgentStore((state) => state.run)
  const events = useAgentStore((state) => state.events)
  const selectedStepId = useAgentStore((state) => state.selectedStepId)
  const isStreaming = useAgentStore((state) => state.isStreaming)
  const debugMode = useAgentStore((state) => state.debugMode)
  const selectStep = useAgentStore((state) => state.selectStep)
  const setDebugMode = useAgentStore((state) => state.setDebugMode)
  const cancelRun = useAgentStore((state) => state.cancelRun)
  const closePanel = useAgentStore((state) => state.closePanel)
  const panelOpen = useAgentStore((state) => state.panelOpen)

  const selectedStep = useAgentStore(selectCurrentStep)
  const timelineRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const node = timelineRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [events.length])

  if (!run || !panelOpen) return null

  const steps = run.steps || []
  const stepArtifacts: AgentArtifact[] = selectedStep
    ? (run.artifacts || []).filter((artifact) => artifact.step_id === selectedStep.id)
    : []
  const progress = run.progress
  const progressValue = progress.total_steps
    ? Math.round((progress.completed_steps / progress.total_steps) * 100)
    : 0
  const isActive = run.status === "queued" || run.status === "running"

  const handleDownload = async (artifact: AgentArtifact) => {
    try {
      const blob = await agentApi.downloadArtifact(artifact.id)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement("a")
      anchor.href = url
      anchor.download = artifact.filename || `${artifact.title}.txt`
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(url)
    } catch {
      toast.error(t("artifact.downloadFailed"))
    }
  }

  const handleCancel = async () => {
    try {
      await cancelRun()
      toast.message(t("toast.cancelRequested"))
    } catch {
      toast.error(t("toast.cancelFailed"))
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && closePanel()}>
      <DialogContent
        className="flex h-[85vh] w-[95vw] max-w-6xl flex-col gap-0 overflow-hidden p-0"
        onInteractOutside={(event) => event.preventDefault()}
      >
        {/* Header */}
        <div className="flex flex-col gap-3 border-b px-5 py-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2">
              <h2 className="truncate text-lg font-semibold">
                {run.title || t("panel.title")}
              </h2>
              <Badge variant={RUN_STATUS_VARIANT[run.status]}>
                {t(`status.${run.status}`, { defaultValue: run.status })}
              </Badge>
              {isStreaming && (
                <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
              )}
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant={debugMode ? "secondary" : "outline"}
                size="sm"
                onClick={() => setDebugMode(!debugMode)}
              >
                {t("panel.debug")}: {debugMode ? t("panel.on") : t("panel.off")}
              </Button>
              {isActive && (
                <Button variant="outline" size="sm" onClick={handleCancel}>
                  {t("panel.cancel")}
                </Button>
              )}
              <Button variant="ghost" size="icon" className="h-8 w-8" onClick={closePanel}>
                <X className="h-4 w-4" />
              </Button>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <Progress value={progressValue} className="h-2 flex-1" />
            <span className="shrink-0 text-xs text-muted-foreground">
              {progress.completed_steps}/{progress.total_steps}
            </span>
          </div>
          {run.error_message && (
            <p className="rounded bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {run.error_message}
            </p>
          )}
        </div>

        {/* Body: steps | timeline | detail */}
        <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[220px_minmax(0,1fr)_360px]">
          {/* Steps */}
          <div className="flex min-h-0 flex-col border-r">
            <div className="border-b px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {t("panel.steps")}
            </div>
            <div className="min-h-0 flex-1 space-y-1 overflow-y-auto p-2">
              {steps.map((step) => (
                <button
                  key={step.id}
                  type="button"
                  onClick={() => selectStep(step.id)}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left text-sm transition-colors",
                    selectedStepId === step.id
                      ? "border-primary bg-primary/10"
                      : "border-transparent hover:bg-muted"
                  )}
                >
                  <StepStatusIcon status={step.status} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{step.agent_name}</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {t(`status.${step.status}`, { defaultValue: step.status })}
                    </span>
                  </span>
                </button>
              ))}
              {steps.length === 0 && (
                <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                  {t("panel.waiting")}
                </p>
              )}
            </div>
          </div>

          {/* Timeline */}
          <div className="flex min-h-0 flex-col border-r">
            <div className="border-b px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {t("panel.timeline")}
            </div>
            <div ref={timelineRef} className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
              {events.map((event) => (
                <div key={event.id} className="flex gap-2 text-sm">
                  <span
                    className={cn(
                      "mt-1.5 h-2 w-2 shrink-0 rounded-full",
                      LEVEL_DOT[event.level] || "bg-muted-foreground"
                    )}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-[11px] font-medium text-muted-foreground">
                        {t(`event.${event.event_type}`, { defaultValue: event.event_type })}
                      </span>
                      <span className="text-[10px] text-muted-foreground/70">
                        {formatTime(event.created_at)}
                      </span>
                    </div>
                    {event.message && (
                      <p className="break-words text-sm leading-snug">{event.message}</p>
                    )}
                  </div>
                </div>
              ))}
              {events.length === 0 && (
                <p className="py-6 text-center text-xs text-muted-foreground">
                  {t("panel.waiting")}
                </p>
              )}
            </div>
          </div>

          {/* Detail */}
          <div className="flex min-h-0 flex-col">
            <div className="border-b px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {t("panel.detail")}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-3">
              {!selectedStep ? (
                <p className="py-6 text-center text-xs text-muted-foreground">
                  {t("panel.selectStep")}
                </p>
              ) : (
                <Tabs key={selectedStep.id} defaultValue="summary" className="w-full">
                  <TabsList className="flex w-full flex-wrap">
                    <TabsTrigger value="summary">{t("tabs.summary")}</TabsTrigger>
                    {stepArtifacts.length > 0 && (
                      <TabsTrigger value="artifacts">
                        {t("tabs.artifacts")} ({stepArtifacts.length})
                      </TabsTrigger>
                    )}
                    {debugMode && selectedStep.prompt_snapshot && (
                      <TabsTrigger value="prompt">{t("tabs.prompt")}</TabsTrigger>
                    )}
                    {debugMode && selectedStep.model_response && (
                      <TabsTrigger value="response">{t("tabs.response")}</TabsTrigger>
                    )}
                    {selectedStep.error_message && (
                      <TabsTrigger value="error">{t("tabs.error")}</TabsTrigger>
                    )}
                  </TabsList>

                  <TabsContent value="summary" className="space-y-4 pt-3">
                    {(selectedStep.model_provider || selectedStep.model_name) && (
                      <p className="text-xs text-muted-foreground">
                        {t("field.model")}: {selectedStep.model_provider || "?"} /{" "}
                        {selectedStep.model_name || "?"}
                      </p>
                    )}
                    <Field label={t("field.output")} value={selectedStep.output_summary} />
                    <Field label={t("field.reasoning")} value={selectedStep.reasoning_summary} />
                    <Field label={t("field.decision")} value={selectedStep.decision_notes} />
                    <Field label={t("field.selfCheck")} value={selectedStep.self_check} />
                    <Field label={t("field.nextAction")} value={selectedStep.next_action} />
                    <Field label={t("field.input")} value={selectedStep.input_summary} />
                    {!debugMode && (
                      <p className="text-[11px] text-muted-foreground/70">
                        {t("panel.debugHint")}
                      </p>
                    )}
                  </TabsContent>

                  {stepArtifacts.length > 0 && (
                    <TabsContent value="artifacts" className="space-y-3 pt-3">
                      {stepArtifacts.map((artifact) => (
                        <ArtifactViewer
                          key={artifact.id}
                          artifact={artifact}
                          onDownload={handleDownload}
                        />
                      ))}
                    </TabsContent>
                  )}

                  {debugMode && selectedStep.prompt_snapshot && (
                    <TabsContent value="prompt" className="pt-3">
                      <pre className="whitespace-pre-wrap break-words rounded-md border bg-muted/40 p-3 text-xs leading-relaxed">
                        {selectedStep.prompt_snapshot}
                      </pre>
                    </TabsContent>
                  )}

                  {debugMode && selectedStep.model_response && (
                    <TabsContent value="response" className="pt-3">
                      <pre className="whitespace-pre-wrap break-words rounded-md border bg-muted/40 p-3 text-xs leading-relaxed">
                        {selectedStep.model_response}
                      </pre>
                    </TabsContent>
                  )}

                  {selectedStep.error_message && (
                    <TabsContent value="error" className="pt-3">
                      <pre className="whitespace-pre-wrap break-words rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
                        {selectedStep.error_message}
                      </pre>
                    </TabsContent>
                  )}
                </Tabs>
              )}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default AgentRunPanel
