/**
 * Shared stage model for the Code workspace.
 *
 * The workspace presents the run as a sequence of per-stage *windows*: the
 * stepper is the navigation bar and each stage shows only its own slice of the
 * conversation. This module is the single source of truth for the display
 * stages, the internal-key -> display-stage mapping, and the run-derived
 * navigation state (which stage is live, which failed, how far you can jump),
 * so the stepper, the conversation rail, and the page all agree.
 */
import type { AgentRun } from "@/api/agent"

/** The five user-facing stages of the Code workflow, in order. */
export const DISPLAY_STAGES = ["requirements", "flow", "documents", "style", "app"] as const
export type DisplayStage = (typeof DISPLAY_STAGES)[number]

/** Map any internal step / progress / review-stage key onto its display-stage index. */
export const STAGE_INDEX: Record<string, number> = {
  planner: 0,
  requirements: 0,
  flow: 1,
  documents: 2,
  style_select: 3,
  style: 3,
  preview: 3,
  publish: 4,
  publisher: 4,
  app: 4,
  build: 4,
  critic: 4,
  repair: 4,
  done: 4,
  // The frontend-project build is a separate run that also binds to the store;
  // its whole pipeline maps onto the "app" stage so a build failure surfaces here.
  fe_planner: 4,
  fe_project_build: 4,
  fe_publish: 4,
}

/** Display-stage index for an internal key, or -1 when unknown. */
export function stageIndexOf(key: string | null | undefined): number {
  if (!key) return -1
  return key in STAGE_INDEX ? STAGE_INDEX[key] : -1
}

/** Resolve an internal step / review-stage key to its display stage (null if unknown). */
export function displayStageOf(key: string | null | undefined): DisplayStage | null {
  const idx = stageIndexOf(key)
  return idx >= 0 ? DISPLAY_STAGES[idx] : null
}

export interface StageNav {
  /** Live/active stage index (review gate takes precedence over current step); -1 when no run. */
  activeIdx: number
  /** Index of the stage that failed (terminal failed/partial run), else -1. */
  failedIdx: number
  /** The failed step's agent_key, for a precise per-stage retry. */
  failedStepKey: string | null
  /** Furthest stage the user may navigate to (review past windows, not the future). */
  maxReachedIdx: number
  allDone: boolean
  paused: boolean
  /** Where the workspace should focus by default / when the live position advances. */
  focusStage: DisplayStage
  /** The live stage as a display stage (null when no run / not started). */
  activeStage: DisplayStage | null
}

/**
 * Derive the per-stage navigation state from a run. Replicates the progress /
 * failure logic the stepper has always used (review_stage precedence, the
 * "unknown key -> stage 0" fallback, the failed-stage anchor) and adds the two
 * things the windowed UI needs: how far the user may jump (`maxReachedIdx`) and
 * which window to focus (`focusStage`).
 */
export function deriveStageNav(run: AgentRun | null): StageNav {
  const last = DISPLAY_STAGES.length - 1
  const progress = run?.progress
  const status = run?.status
  const allDone = status === "completed" || status === "partial"
  const paused = status === "paused"

  const current = progress?.review_stage || progress?.current_step || (run ? "requirements" : null)
  const currentIdx = current ? STAGE_INDEX[current] ?? 0 : -1

  const failedStep =
    status === "failed" || status === "partial"
      ? run?.steps?.find((step) => step.status === "failed")
      : undefined
  let failedIdx = -1
  if (failedStep) failedIdx = STAGE_INDEX[failedStep.agent_key] ?? Math.max(currentIdx, 0)
  else if (status === "failed") failedIdx = Math.max(currentIdx, 0)

  const activeIdx = failedIdx >= 0 ? failedIdx : currentIdx
  // Completed runs open every window for review; an in-flight run lets you look
  // back over the done stages up to (and including) the live one.
  const maxReachedIdx = allDone ? last : Math.max(activeIdx, 0)

  let focusIdx: number
  if (failedIdx >= 0) focusIdx = failedIdx
  else if (allDone) focusIdx = last
  else if (activeIdx >= 0) focusIdx = activeIdx
  else focusIdx = 0

  return {
    activeIdx,
    failedIdx,
    failedStepKey: failedStep?.agent_key ?? null,
    maxReachedIdx,
    allDone,
    paused,
    focusStage: DISPLAY_STAGES[Math.min(Math.max(focusIdx, 0), last)],
    activeStage: activeIdx >= 0 ? DISPLAY_STAGES[Math.min(activeIdx, last)] : null,
  }
}
