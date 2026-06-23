import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Check, ListChecks, Loader2, Sparkles } from "lucide-react"

import type { ClarificationQuestion } from "@/api/code"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

// The compiled answers become a revise instruction fed to the Chinese-output
// requirements-revision prompt and shown in the transcript. It is authored in
// Chinese to stay coherent with the (always-Chinese) requirements doc it edits —
// mirroring the backend revision prompts. The dialog's UI chrome stays localized.
const INSTRUCTION_INTRO = "我已通过「需求澄清问卷」确认了以下关键决策，请据此修订需求文档："
const INSTRUCTION_RULES = [
  "修订要求：",
  "- 将以上每条确认逐一落实到需求文档的相应章节（如功能范围、用户流程、权限与账户、数据对象、非功能要求等）。",
  "- 把已确认的问题从「边界与待确认问题」中移除，或标注为「已确认」。",
  "- 仅做与这些决策相关的增量修改，保持文档其余部分稳定、自洽。",
].join("\n")
const MARK_CONFIRMED = "（用户确认）"
const MARK_SUGGESTED = "（采用建议）"
// Empty answer text; the （采用建议）/（用户确认） mark is appended separately, so
// this must NOT itself include a mark (else the suffix would double up).
const NO_PREFERENCE = "暂不限定"

interface RequirementsClarifyDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The questionnaire to render (derived from the run via selectRequirementsQuestions). */
  questions: ClarificationQuestion[]
  /** The current requirements review round; bumps each (re)generation so a new
   * round always re-seeds even when the questionnaire is structurally identical. */
  round: number
  /** Apply the compiled answers as a revise instruction (re-iterates the doc). */
  onApply: (instruction: string) => void
  /** True while the run is busy regenerating after a previous apply. */
  submitting?: boolean
}

/** Per-question working state in the dialog. */
interface Answer {
  selected: string[]
  custom: string
  /** Whether the user changed it from the model's default suggestion. */
  touched: boolean
}

const answersSignature = (questions: ClarificationQuestion[]): string =>
  questions.map((q) => `${q.id}:${q.type}:${q.options.map((o) => o.value).join(",")}`).join("|")

/** Seed each question with its recommended default (the express, no-change path). */
function seedAnswers(questions: ClarificationQuestion[]): Record<string, Answer> {
  const next: Record<string, Answer> = {}
  for (const question of questions) {
    next[question.id] = { selected: [...(question.default ?? [])], custom: "", touched: false }
  }
  return next
}

/**
 * Requirements quick-confirm dialog. Renders the AI-generated clarification
 * questionnaire (single/multi choice + optional free-text per question) so the
 * user can lock in the key decisions in a few clicks instead of writing a
 * free-form instruction. Every question is pre-filled with the model's
 * recommended default, so "apply suggestions" is one click; any answer the user
 * leaves untouched keeps that default. On submit the answers compile into a
 * single Chinese revise instruction that re-iterates the requirements doc.
 */
export function RequirementsClarifyDialog({
  open,
  onOpenChange,
  questions,
  round,
  onApply,
  submitting = false,
}: RequirementsClarifyDialogProps) {
  const { t } = useTranslation("code")
  // Key the seed on the round too, not just the question structure: a new revise
  // round can re-emit a structurally identical questionnaire (the local fallback
  // uses fixed ids), and seeding only on structure would then leak the prior
  // round's edits / touched flags into this round's confirmations.
  const signature = `${round}:${answersSignature(questions)}`

  const [answers, setAnswers] = useState<Record<string, Answer>>(() => seedAnswers(questions))

  // Re-seed when the dialog opens or the round/question set changes, so stale
  // answers never linger. Uses React's "adjust state during render" pattern
  // (tracking the last seeded signature) instead of an effect, which avoids the
  // cascading-render setState-in-effect lint.
  const [seededSignature, setSeededSignature] = useState<string | null>(null)
  const activeSignature = open ? signature : null
  if (activeSignature !== null && activeSignature !== seededSignature) {
    setSeededSignature(activeSignature)
    setAnswers(seedAnswers(questions))
  }

  const setSingle = (question: ClarificationQuestion, value: string) => {
    setAnswers((prev) => ({
      ...prev,
      [question.id]: { ...prev[question.id], selected: [value], touched: true },
    }))
  }

  const toggleMulti = (question: ClarificationQuestion, value: string) => {
    setAnswers((prev) => {
      const current = prev[question.id]?.selected ?? []
      const selected = current.includes(value)
        ? current.filter((item) => item !== value)
        : [...current, value]
      return { ...prev, [question.id]: { ...prev[question.id], selected, touched: true } }
    })
  }

  const setCustom = (question: ClarificationQuestion, custom: string) => {
    setAnswers((prev) => ({
      ...prev,
      [question.id]: { ...prev[question.id], custom, touched: true },
    }))
  }

  const compileInstruction = (source: Record<string, Answer>): string => {
    const lines = questions.map((question, index) => {
      const answer = source[question.id] ?? { selected: question.default, custom: "", touched: false }
      const labels = answer.selected.map(
        (value) => question.options.find((option) => option.value === value)?.label ?? value
      )
      const parts = [...labels]
      const custom = answer.custom.trim()
      if (custom) parts.push(custom)
      const answerText = parts.length ? parts.join("、") : NO_PREFERENCE
      const mark = answer.touched ? MARK_CONFIRMED : MARK_SUGGESTED
      const head = question.category ? `[${question.category}] ${question.question}` : question.question
      return `${index + 1}. ${head}\n   答复：${answerText}${mark}`
    })
    return [INSTRUCTION_INTRO, "", lines.join("\n"), "", INSTRUCTION_RULES].join("\n")
  }

  const handleApply = (useSuggestionsOnly: boolean) => {
    const source = useSuggestionsOnly ? seedAnswers(questions) : answers
    onApply(compileInstruction(source))
  }

  const renderQuestion = (question: ClarificationQuestion) => {
    const answer = answers[question.id] ?? { selected: [], custom: "", touched: false }
    const defaults = new Set(question.default ?? [])
    return (
      <div key={question.id} className="space-y-2.5 rounded-lg border bg-card/60 p-3">
        <div className="space-y-1">
          <div className="flex items-start gap-2">
            {question.category && (
              <Badge variant="outline" className="mt-0.5 shrink-0 text-[10px]">
                {question.category}
              </Badge>
            )}
            <p className="text-sm font-medium text-foreground">{question.question}</p>
          </div>
          {question.rationale && (
            <p className="text-xs text-muted-foreground">{question.rationale}</p>
          )}
        </div>

        <div className="space-y-1.5">
          {question.options.map((option) => {
            const checked = answer.selected.includes(option.value)
            const isDefault = defaults.has(option.value)
            return (
              <button
                key={option.value}
                type="button"
                role={question.type === "single" ? "radio" : "checkbox"}
                aria-checked={checked}
                onClick={() =>
                  question.type === "single"
                    ? setSingle(question, option.value)
                    : toggleMulti(question, option.value)
                }
                className={cn(
                  "flex w-full items-start gap-2.5 rounded-md border px-3 py-2 text-left text-sm transition-colors",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  checked ? "border-primary bg-primary/10" : "hover:bg-muted"
                )}
              >
                <span className="mt-0.5 shrink-0">
                  {question.type === "single" ? (
                    <span
                      className={cn(
                        "flex h-4 w-4 items-center justify-center rounded-full border",
                        checked ? "border-primary" : "border-muted-foreground/40"
                      )}
                    >
                      {checked && <span className="h-2 w-2 rounded-full bg-primary" />}
                    </span>
                  ) : (
                    <Checkbox checked={checked} className="pointer-events-none" tabIndex={-1} />
                  )}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-1.5">
                    <span className="font-medium text-foreground">{option.label}</span>
                    {isDefault && (
                      <Badge variant="secondary" className="text-[10px]">
                        {t("clarify.suggestedBadge")}
                      </Badge>
                    )}
                  </span>
                  {option.description && (
                    <span className="mt-0.5 block text-xs text-muted-foreground">
                      {option.description}
                    </span>
                  )}
                </span>
              </button>
            )
          })}
        </div>

        {question.allow_custom && (
          <Input
            value={answer.custom}
            onChange={(event) => setCustom(question, event.target.value)}
            placeholder={t("clarify.customPlaceholder")}
            className="text-sm"
          />
        )}
      </div>
    )
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85vh] max-w-2xl flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-primary" />
            {t("clarify.title")}
          </DialogTitle>
          <DialogDescription>{t("clarify.subtitle")}</DialogDescription>
        </DialogHeader>

        {questions.length === 0 ? (
          <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
            {t("clarify.empty")}
          </div>
        ) : (
          // min-h-0 lets this flex child shrink below its content so overflow-y
          // actually scrolls (DialogContent is a max-h-bounded flex column).
          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
            {questions.map(renderQuestion)}
          </div>
        )}

        <DialogFooter className="flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          {/* Skip = dismiss the questionnaire without iterating. The user lands back
              at the review gate with approve / free-text revise still available, and
              the once-per-round auto-open guard keeps it from re-popping this round. */}
          <Button
            variant="ghost"
            className="text-muted-foreground sm:order-1"
            disabled={submitting}
            onClick={() => onOpenChange(false)}
          >
            {t("clarify.skip")}
          </Button>
          <div className="flex flex-col gap-2 sm:order-2 sm:flex-row">
            <Button
              variant="ghost"
              disabled={submitting || questions.length === 0}
              onClick={() => handleApply(true)}
            >
              <ListChecks className="mr-2 h-4 w-4" />
              {t("clarify.applySuggestions")}
            </Button>
            <Button
              disabled={submitting || questions.length === 0}
              onClick={() => handleApply(false)}
            >
              {submitting ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <Check className="mr-2 h-4 w-4" />
              )}
              {t("clarify.apply")}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default RequirementsClarifyDialog
