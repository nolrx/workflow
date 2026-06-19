import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { AlertTriangle, Bot, Check, ListChecks, Loader2, Plus, Send, Sparkles } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { LiveStageCard } from "@/components/code/LiveStageCard"
import { selectRequirementsQuestions } from "@/components/code/clarify"
import { RequirementsClarifyDialog } from "@/components/code/RequirementsClarifyDialog"
import { StageArtifactCard, type ArtifactStage } from "@/components/code/StageArtifactCard"
import { useStickToBottom } from "@/hooks/use-stick-to-bottom"
import {
  deriveConversation,
  useAgentStore,
  type ConversationMessage,
} from "@/stores/agentStore"

interface ConversationRailProps {
  requirementDraft: string
  onRequirementChange: (value: string) => void
  onStart: () => void
  onApprove: () => void
  onRevise: (instruction: string) => void
  onNewProject: () => void
}

/** Running agent-step key -> how to render its live output inline. */
const LIVE_STAGE: Record<string, { variant: "text" | "thinking"; tab: ArtifactStage }> = {
  requirements: { variant: "text", tab: "requirements" },
  flow: { variant: "text", tab: "flow" },
  documents: { variant: "thinking", tab: "documents" },
  style: { variant: "text", tab: "style" },
}

function Avatar({ role }: { role: ConversationMessage["role"] }) {
  if (role === "user") {
    return (
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/10 text-[11px] font-medium text-primary">
        <span className="sr-only">user</span>
        <span aria-hidden>U</span>
      </div>
    )
  }
  return (
    <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground">
      <Bot className="h-4 w-4" />
    </div>
  )
}

/** User bubbles and lightweight system markers (the rich assistant turns become cards). */
function Message({ message }: { message: ConversationMessage }) {
  const { t } = useTranslation("code")

  if (message.role === "system") {
    if (message.kind === "error") {
      return (
        <div className="flex items-center justify-center gap-1.5 text-xs text-destructive">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          <span>{message.text}</span>
        </div>
      )
    }
    const text =
      message.kind === "completed" ? t("conversation.completedTitle") : t("conversation.resolved")
    return (
      <div className="flex items-center justify-center gap-1.5 text-xs text-muted-foreground">
        <Check className="h-3.5 w-3.5 shrink-0 text-primary" />
        <span>{text}</span>
      </div>
    )
  }

  // user
  return (
    <div className="flex flex-row-reverse gap-2">
      <Avatar role="user" />
      <div className="max-w-[85%] whitespace-pre-wrap break-words rounded-lg bg-primary/10 px-3 py-2 text-sm text-foreground">
        {message.text}
      </div>
    </div>
  )
}

/**
 * Single-column conversational Code workspace. The transcript is event-sourced
 * (opening requirement, each reviewed stage, adjustments, confirmations); each
 * reviewed stage renders as an inline collapsible artifact card (the live
 * preview folded into the chat). While a step streams, its output shows in an
 * auto-expanded live card; once the run finishes, the app-preview card is
 * appended. A state-aware composer sits at the bottom.
 */
export function ConversationRail({
  requirementDraft,
  onRequirementChange,
  onStart,
  onApprove,
  onRevise,
  onNewProject,
}: ConversationRailProps) {
  const { t } = useTranslation("code")
  const run = useAgentStore((state) => state.run)
  const events = useAgentStore((state) => state.events)
  const isStreaming = useAgentStore((state) => state.isStreaming)
  const streamingByStep = useAgentStore((state) => state.streamingByStep)
  // The clarification questionnaire is delivered as an artifact on the latest
  // requirements step; read it straight from the run snapshot.
  const clarifyQuestions = selectRequirementsQuestions(run)
  const clarifyCount = clarifyQuestions.length

  const [instruction, setInstruction] = useState("")
  const [clarifyOpen, setClarifyOpen] = useState(false)
  // Per-stage expand state; user toggles override the auto-open defaults.
  const [openStages, setOpenStages] = useState<Record<string, boolean>>({})

  const messages = deriveConversation(run, events)
  const status = run?.status
  const reviewStage = run?.progress?.review_stage ?? null
  const isPaused = status === "paused"
  const isBusy = isStreaming || status === "running" || status === "queued"
  const isCompleted = status === "completed" || status === "partial"
  const isFailed = status === "failed" || status === "cancelled"

  const runningStep = run?.steps?.find((step) => step.status === "running")
  const liveText = runningStep ? streamingByStep[runningStep.id] ?? "" : null

  // When the review gate advances, focus the new stage and auto-collapse the
  // now-settled ones (the "auto-collapse once done" behaviour). Manual toggles
  // within a gate persist because the effect only fires when the gate changes.
  const prevReview = useRef<string | null>(null)
  useEffect(() => {
    if (reviewStage && reviewStage !== prevReview.current) {
      setOpenStages({ [reviewStage]: true })
    }
    prevReview.current = reviewStage
  }, [reviewStage])

  // Each requirements review gate (fresh + every revise round) increments this;
  // it keys the one-shot auto-open of the clarification dialog so a new round
  // re-opens it, but a manual close within the same round does not.
  const requirementsReviewRound = events.filter(
    (event) =>
      event.event_type === "step_awaiting_review" &&
      (event.payload?.stage as string | undefined) === "requirements"
  ).length
  const showClarify = isPaused && reviewStage === "requirements" && clarifyCount > 0
  // Auto-open the questionnaire once per (run, requirements round). Keying on the
  // run id means it re-opens for a new revise round AND for a different run (this
  // component stays mounted across run switches, e.g. 新建会话), while a manual
  // close within the same round/run does not re-trigger. Render-phase "adjust
  // state" pattern (no effect) so it never fights the user's toggle.
  const [autoOpenedKey, setAutoOpenedKey] = useState<string | null>(null)
  const autoOpenKey = run?.id ? `${run.id}:${requirementsReviewRound}` : null
  if (showClarify && autoOpenKey && autoOpenedKey !== autoOpenKey) {
    setAutoOpenedKey(autoOpenKey)
    setClarifyOpen(true)
  }

  const scrollRef = useStickToBottom(messages.length + (liveText?.length ?? 0))

  const applyClarify = (compiled: string) => {
    onRevise(compiled)
    setClarifyOpen(false)
  }

  const submitRevise = () => {
    const text = instruction.trim()
    if (!text) return
    onRevise(text)
    setInstruction("")
  }

  // Only the latest awaiting-review turn per stage carries the editable card; an
  // earlier turn (superseded by a revision) collapses to a plain note.
  const latestAwaitingId: Record<string, string> = {}
  for (const message of messages) {
    if (message.role === "assistant" && message.kind === "awaiting_review" && message.stage) {
      latestAwaitingId[message.stage] = message.id
    }
  }

  const renderItem = (message: ConversationMessage) => {
    if (message.role === "assistant" && message.kind === "awaiting_review") {
      const stage = message.stage as ArtifactStage | null
      if (stage && latestAwaitingId[stage] === message.id) {
        const state = isPaused && reviewStage === stage ? "review" : "done"
        const open = openStages[stage] ?? state === "review"
        return (
          <StageArtifactCard
            key={message.id}
            stage={stage}
            state={state}
            open={open}
            onToggle={() => setOpenStages((prev) => ({ ...prev, [stage]: !open }))}
          />
        )
      }
      // Superseded turn: a compact, muted note.
      return (
        <div key={message.id} className="flex gap-2">
          <Avatar role="assistant" />
          <div className="min-w-0 flex-1 rounded-lg border bg-card/60 px-3 py-2 text-sm text-muted-foreground">
            {message.text}
          </div>
        </div>
      )
    }
    return <Message key={message.id} message={message} />
  }

  const liveMeta = runningStep ? LIVE_STAGE[runningStep.agent_key] : undefined
  const appOpen = openStages.app ?? true

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div ref={scrollRef} className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4">
        {!run && (
          <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
            <Sparkles className="mb-2 h-4 w-4 text-primary" />
            {t("conversation.intro")}
          </div>
        )}

        {messages.map(renderItem)}

        {/* Live, streaming step folded into the conversation. */}
        {runningStep && (
          <LiveStageCard
            title={
              liveMeta
                ? t("conversation.generatingStage", { stage: t(`workspace.tabs.${liveMeta.tab}`) })
                : t("conversation.generating")
            }
            variant={liveMeta?.variant ?? "spinner"}
            text={liveMeta?.variant === "text" ? liveText ?? "" : ""}
          />
        )}

        {/* Once the build finishes, the previewable app is the final inline card. */}
        {isCompleted && (
          <StageArtifactCard
            stage="app"
            state="done"
            open={appOpen}
            onToggle={() => setOpenStages((prev) => ({ ...prev, app: !appOpen }))}
          />
        )}
      </div>

      <div className="mt-3 border-t px-4 py-3">
        {!run || isFailed ? (
          <div className="space-y-2">
            {isFailed && (
              <p className="text-xs text-destructive">
                {run?.error_message || t("conversation.failed")}
              </p>
            )}
            <Textarea
              value={requirementDraft}
              onChange={(event) => onRequirementChange(event.target.value)}
              placeholder={t("input.placeholder")}
              rows={4}
              className="resize-none text-sm"
              disabled={isBusy}
            />
            <Button className="w-full" onClick={onStart} disabled={!requirementDraft.trim() || isBusy}>
              <Bot className="mr-2 h-4 w-4" />
              {isFailed ? t("conversation.retry") : t("conversation.start")}
            </Button>
          </div>
        ) : isBusy ? (
          <div className="flex items-center justify-center gap-2 py-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            {t("conversation.running")}
          </div>
        ) : isPaused ? (
          <div className="space-y-2">
            {showClarify && (
              <Button
                variant="secondary"
                className="w-full"
                onClick={() => setClarifyOpen(true)}
              >
                <ListChecks className="mr-2 h-4 w-4" />
                {t("clarify.openButton", { count: clarifyCount })}
              </Button>
            )}
            <p className="text-xs font-medium text-muted-foreground">
              {t("conversation.reviseLabel")}
            </p>
            <Textarea
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) submitRevise()
              }}
              placeholder={t("conversation.revisePlaceholder")}
              rows={3}
              className="resize-none text-sm"
            />
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                className="min-w-[8rem] flex-1"
                onClick={submitRevise}
                disabled={!instruction.trim()}
              >
                <Send className="mr-2 h-4 w-4" />
                {t("conversation.revise")}
              </Button>
              <Button className="min-w-[8rem] flex-1" onClick={onApprove}>
                <Check className="mr-2 h-4 w-4" />
                {t("conversation.approve")}
              </Button>
            </div>
          </div>
        ) : isCompleted ? (
          <div className="space-y-2">
            <p className="flex items-center gap-2 text-sm font-medium text-foreground">
              <Check className="h-4 w-4 text-primary" />
              {t("conversation.completedTitle")}
            </p>
            <p className="text-xs text-muted-foreground">{t("conversation.completedHint")}</p>
            <Button variant="outline" className="w-full" onClick={onNewProject}>
              <Plus className="mr-2 h-4 w-4" />
              {t("conversation.newSession")}
            </Button>
          </div>
        ) : null}
      </div>

      <RequirementsClarifyDialog
        open={clarifyOpen && clarifyCount > 0}
        onOpenChange={setClarifyOpen}
        questions={clarifyQuestions}
        round={requirementsReviewRound}
        onApply={applyClarify}
        submitting={isBusy}
      />
    </div>
  )
}

export default ConversationRail
