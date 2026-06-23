/**
 * Agent Swarm store.
 *
 * Drives the live run workspace. SSE (fetch + ReadableStream, so the bearer
 * token can be sent) updates the event
 * timeline in real time; structural events trigger a debounced snapshot refresh
 * so steps/artifacts stay authoritative. fetchRun also serves as the reconnect
 * path after a page reload.
 */
import { create } from "zustand"

import { tokenManager } from "@/api/client"
import { useCreditStore } from "@/stores/creditStore"
import {
  AGENT_API_BASE,
  agentApi,
  type AgentEvent,
  type AgentRun,
  type AgentStep,
  type CreateRunBody,
} from "@/api/agent"

// Stream/refresh control kept outside the store (not reactive state).
let streamAbort: AbortController | null = null
let refreshTimer: ReturnType<typeof setTimeout> | null = null
// The run the panel is currently bound to. Async refreshes resolving for a
// different (or closed) run must not write back into the store.
let activeRunId: string | null = null
// Run statuses that mean the workflow has finished (and credits settled / refunded).
const TERMINAL_RUN_STATUSES = new Set(["completed", "partial", "failed", "cancelled"])
// Guards the post-run balance refresh so it fires once per run reaching a terminal state.
let creditRefreshedRunId: string | null = null

function clearRefreshTimer() {
  if (refreshTimer) {
    clearTimeout(refreshTimer)
    refreshTimer = null
  }
}

interface AgentState {
  run: AgentRun | null
  events: AgentEvent[]
  /** Live-accumulated token text per step id (from transient agent_delta events). */
  streamingByStep: Record<string, string>
  selectedStepId: string | null
  isStreaming: boolean
  debugMode: boolean
  /** Whether the full-detail run modal is open (the inline timeline is always shown). */
  panelOpen: boolean

  startRun: (body: CreateRunBody) => Promise<string>
  openRun: (runId: string) => Promise<void>
  listRuns: (params?: {
    domain?: string
    resourceId?: string
    workflow?: string
    limit?: number
  }) => Promise<AgentRun[]>
  openLatestRunForResource: (resourceId: string, workflow?: string) => Promise<boolean>
  cancelRun: () => Promise<void>
  /** Relaunch the bound run (failed / partial) to retry its failed stage. */
  retryRun: (stage?: string | null) => Promise<void>
  resumeRun: (action: "approve" | "revise", instruction?: string) => Promise<void>
  /** Submit a UI-style selection at the style_select gate (resumes the run). */
  selectStyle: (styleIds: string[]) => Promise<void>
  selectStep: (stepId: string | null) => void
  setDebugMode: (value: boolean) => void
  openPanel: () => void
  closePanel: () => void
  /** Tear down the current run workspace (stream + state) for a fresh session. */
  reset: () => void
}

export const useAgentStore = create<AgentState>()((set, get) => {
  const mergeEvents = (incoming: AgentEvent[]) => {
    if (!incoming.length) return
    set((state) => {
      const seen = new Set(state.events.map((event) => event.sequence))
      const merged = [...state.events]
      for (const event of incoming) {
        if (!seen.has(event.sequence)) {
          merged.push(event)
          seen.add(event.sequence)
        }
      }
      merged.sort((a, b) => a.sequence - b.sequence)
      return { events: merged }
    })
  }

  const refresh = async (runId: string) => {
    try {
      const run = await agentApi.fetchRun(runId)
      // A newer run started, or the panel was closed, while this was in flight.
      if (runId !== activeRunId) return
      set((state) => ({
        run,
        selectedStepId: state.selectedStepId || run.steps?.[0]?.id || null,
      }))
      if (run.events) mergeEvents(run.events)
      // When the run settles, credits have been spent (or refunded on early
      // failure) — refresh the displayed balance exactly once per run.
      if (TERMINAL_RUN_STATUSES.has(run.status) && creditRefreshedRunId !== runId) {
        creditRefreshedRunId = runId
        void useCreditStore.getState().refreshBalance()
      }
    } catch {
      // Transient (e.g. mid-write); the next refresh / stream event recovers.
    }
  }

  const scheduleRefresh = (runId: string) => {
    if (refreshTimer) clearTimeout(refreshTimer)
    refreshTimer = setTimeout(() => void refresh(runId), 350)
  }

  const stream = async (runId: string) => {
    streamAbort?.abort()
    streamAbort = new AbortController()
    set({ isStreaming: true })

    const token = tokenManager.getAccessToken()
    // Resume the stream from the last event we already hold so reconnects (and
    // the re-subscribe after a resume) pull only the delta instead of replaying
    // the whole log. The server continues the sequence past existing events.
    const lastSeq = get().events.reduce((max, event) => Math.max(max, event.sequence), 0)
    const streamUrl = `${AGENT_API_BASE}/agent/runs/${runId}/stream${
      lastSeq ? `?last_sequence=${lastSeq}` : ""
    }`
    try {
      const response = await fetch(streamUrl, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal: streamAbort.signal,
      })
      if (!response.ok || !response.body) {
        set({ isStreaming: false })
        await refresh(runId)
        return
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        let boundary = buffer.indexOf("\n\n")
        while (boundary !== -1) {
          const block = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          boundary = buffer.indexOf("\n\n")

          let eventType = "message"
          let dataStr = ""
          for (const line of block.split("\n")) {
            if (line.startsWith(":")) continue // SSE comment / keepalive
            if (line.startsWith("event:")) eventType = line.slice(6).trim()
            else if (line.startsWith("data:")) dataStr += line.slice(5).trim()
          }

          if (eventType === "done") {
            clearRefreshTimer()
            set({ isStreaming: false })
            await refresh(runId)
            return
          }
          if (!dataStr) continue
          // Transient token delta: append to the step's live text. Never
          // persisted, so it carries no sequence and skips the snapshot refresh.
          if (eventType === "agent_delta") {
            try {
              const delta = JSON.parse(dataStr) as { step_id?: string; text?: string }
              if (delta.step_id && delta.text) {
                const stepId = delta.step_id
                const text = delta.text
                set((state) => ({
                  streamingByStep: {
                    ...state.streamingByStep,
                    [stepId]: (state.streamingByStep[stepId] || "") + text,
                  },
                }))
              }
            } catch {
              // Ignore malformed delta; the snapshot still holds the full text.
            }
            continue
          }
          try {
            const event = JSON.parse(dataStr) as AgentEvent
            mergeEvents([event])
            scheduleRefresh(runId)
            if (event.event_type === "run_completed") {
              clearRefreshTimer()
              set({ isStreaming: false })
              await refresh(runId)
              return
            }
          } catch {
            // Ignore malformed chunk; the snapshot refresh keeps state correct.
          }
        }
      }
      set({ isStreaming: false })
      await refresh(runId)
    } catch (error) {
      if ((error as Error).name !== "AbortError") {
        set({ isStreaming: false })
        await refresh(runId)
      }
    }
  }

  return {
    run: null,
    events: [],
    streamingByStep: {},
    selectedStepId: null,
    isStreaming: false,
    debugMode: false,
    panelOpen: false,

    startRun: async (body) => {
      activeRunId = null
      clearRefreshTimer()
      streamAbort?.abort()
      set({ run: null, events: [], streamingByStep: {}, selectedStepId: null, panelOpen: false })
      const result = await agentApi.createRun(body)
      activeRunId = result.run_id
      await refresh(result.run_id)
      void stream(result.run_id)
      return result.run_id
    },

    openRun: async (runId) => {
      activeRunId = runId
      clearRefreshTimer()
      streamAbort?.abort()
      set({ run: null, events: [], streamingByStep: {}, selectedStepId: null })
      await refresh(runId)
      const run = get().run
      if (run && (run.status === "queued" || run.status === "running")) {
        void stream(runId)
      }
    },

    listRuns: async (params) => {
      try {
        return await agentApi.listRuns(params)
      } catch {
        return []
      }
    },

    // Find and replay the most recent run tied to a resource (e.g. a Code
    // project). A resource accumulates runs across several workflows, so pass
    // `workflow` to replay a specific one (e.g. the conversation/document run)
    // instead of whatever auxiliary run (frontend build, figma slice, canvas)
    // happens to be newest — otherwise the conversation rebinds to a run with no
    // review events and the transcript renders empty.
    openLatestRunForResource: async (resourceId, workflow) => {
      const runs = await get().listRuns({ resourceId, workflow, limit: 1 })
      if (!runs.length) return false
      await get().openRun(runs[0].id)
      return true
    },

    cancelRun: async () => {
      const run = get().run
      if (!run) return
      await agentApi.cancelRun(run.id)
      await refresh(run.id)
    },

    // Retry a failed / partial run from its failed stage. The backend re-runs the
    // worker on the SAME run id (re-using completed stages), so — like resumeRun —
    // we re-subscribe to the live stream, which pulls only events past what we
    // already hold. Throws on a rejected retry (e.g. insufficient credits) so the
    // caller can surface it.
    retryRun: async (stage) => {
      const run = get().run
      if (!run || (run.status !== "failed" && run.status !== "partial")) return
      activeRunId = run.id
      // A fresh terminal state may be re-entered, so let the post-run credit
      // refresh fire again once this retry settles.
      creditRefreshedRunId = null
      await agentApi.retryRun(run.id, { stage })
      await refresh(run.id)
      void stream(run.id)
    },

    // Human-in-the-loop review: approve the produced document (advance to the
    // next stage) or revise it (regenerate from the instruction). Either way the
    // backend relaunches the worker, so we re-subscribe to the live stream — the
    // re-subscribe pulls only events past what we already have.
    resumeRun: async (action, instruction) => {
      const run = get().run
      if (!run || run.status !== "paused") return
      const stage = run.progress?.review_stage ?? undefined
      activeRunId = run.id
      await agentApi.resumeRun(run.id, { action, stage, instruction })
      await refresh(run.id)
      void stream(run.id)
    },

    // Style-selection gate: persist the user's UI style picks and resume the run,
    // which generates the style document from that choice (same relaunch + re-
    // subscribe path as resumeRun).
    selectStyle: async (styleIds) => {
      const run = get().run
      if (!run || run.status !== "paused") return
      const stage = run.progress?.review_stage ?? "style_select"
      activeRunId = run.id
      await agentApi.resumeRun(run.id, { action: "select_style", stage, style_ids: styleIds })
      await refresh(run.id)
      void stream(run.id)
    },

    selectStep: (stepId) => set({ selectedStepId: stepId }),
    setDebugMode: (value) => set({ debugMode: value }),

    // The inline two-pane timeline is always mounted; the modal is just a
    // richer detail view, so open/close only toggles its visibility and never
    // tears down the live run/stream.
    openPanel: () => set({ panelOpen: true }),
    closePanel: () => set({ panelOpen: false }),

    // Tear down the live run + workspace so a new session starts clean. Unlike a
    // bare setState this also aborts the stream and clears the module-level
    // run-tracking state — otherwise a still-open stream could write back into
    // the freshly-reset store via a late refresh.
    reset: () => {
      activeRunId = null
      creditRefreshedRunId = null
      streamAbort?.abort()
      streamAbort = null
      clearRefreshTimer()
      set({
        run: null,
        events: [],
        streamingByStep: {},
        selectedStepId: null,
        isStreaming: false,
        panelOpen: false,
      })
    },
  }
})

export const selectCurrentStep = (state: {
  run: AgentRun | null
  selectedStepId: string | null
}): AgentStep | null => {
  if (!state.run?.steps) return null
  return state.run.steps.find((step) => step.id === state.selectedStepId) || null
}

/** A single bubble in the conversational workspace transcript. */
export interface ConversationMessage {
  id: string
  role: "user" | "assistant" | "system"
  kind: "requirement" | "revision" | "awaiting_review" | "resolved" | "completed" | "error"
  stage?: string | null
  text: string
  sequence: number
}

/**
 * Derive the chat transcript from the run's opening requirement + its ordered
 * event log. Everything is event-sourced, so a paused or finished run replays
 * into the same conversation — the live "generating" bubble is layered on top by
 * the component from streamingByStep.
 */
export function deriveConversation(
  run: AgentRun | null,
  events: AgentEvent[]
): ConversationMessage[] {
  if (!run) return []
  const messages: ConversationMessage[] = []
  const cfg = (run.config || {}) as { requirement?: string }
  const snap = ((run.input_snapshot?.config as { requirement?: string }) || {})
  const requirement = cfg.requirement || snap.requirement
  if (requirement) {
    messages.push({ id: "req-0", role: "user", kind: "requirement", text: requirement, sequence: 0 })
  }
  for (const event of [...events].sort((a, b) => a.sequence - b.sequence)) {
    const payload = event.payload || {}
    const stage = (payload.stage as string | undefined) ?? null
    if (event.event_type === "user_revision") {
      messages.push({
        id: event.id,
        role: "user",
        kind: "revision",
        stage,
        text: (payload.instruction as string) || event.message || "",
        sequence: event.sequence,
      })
    } else if (event.event_type === "step_awaiting_review") {
      messages.push({
        id: event.id,
        role: "assistant",
        kind: "awaiting_review",
        stage,
        text: event.message || "",
        sequence: event.sequence,
      })
    } else if (event.event_type === "review_resolved") {
      messages.push({
        id: event.id,
        role: "system",
        kind: "resolved",
        stage,
        text: event.message || "",
        sequence: event.sequence,
      })
    } else if (event.event_type === "run_completed") {
      messages.push({
        id: event.id,
        role: "system",
        kind: "completed",
        text: event.message || "",
        sequence: event.sequence,
      })
    } else if (event.event_type === "error") {
      messages.push({
        id: event.id,
        role: "system",
        kind: "error",
        text: event.message || "",
        sequence: event.sequence,
      })
    }
  }
  return messages
}
