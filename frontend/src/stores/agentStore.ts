/**
 * Agent Swarm store.
 *
 * Drives the live run workspace. SSE (fetch + ReadableStream, mirroring the
 * RedBook streaming approach so the bearer token can be sent) updates the event
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
  listRuns: (params?: { domain?: string; resourceId?: string; limit?: number }) => Promise<AgentRun[]>
  openLatestRunForResource: (resourceId: string) => Promise<boolean>
  cancelRun: () => Promise<void>
  selectStep: (stepId: string | null) => void
  setDebugMode: (value: boolean) => void
  openPanel: () => void
  closePanel: () => void
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
    try {
      const response = await fetch(`${AGENT_API_BASE}/agent/runs/${runId}/stream`, {
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

    // Find and replay the most recent run tied to a resource (e.g. a Code project).
    openLatestRunForResource: async (resourceId) => {
      const runs = await get().listRuns({ resourceId, limit: 1 })
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

    selectStep: (stepId) => set({ selectedStepId: stepId }),
    setDebugMode: (value) => set({ debugMode: value }),

    // The inline two-pane timeline is always mounted; the modal is just a
    // richer detail view, so open/close only toggles its visibility and never
    // tears down the live run/stream.
    openPanel: () => set({ panelOpen: true }),
    closePanel: () => set({ panelOpen: false }),
  }
})

export const selectCurrentStep = (state: {
  run: AgentRun | null
  selectedStepId: string | null
}): AgentStep | null => {
  if (!state.run?.steps) return null
  return state.run.steps.find((step) => step.id === state.selectedStepId) || null
}
