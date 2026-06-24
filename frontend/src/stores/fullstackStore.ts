/**
 * Full-stack pipeline store.
 *
 * Unlike `agentStore` (single-run, used by the conversation flow), this manages
 * the THREE concurrent generation runs (frontend / backend / middleware) plus the
 * atomic deploy run, each with its own live SSE stream, so the execution-detail
 * panel can show all of their progress at once. Each lane is independent: its own
 * run snapshot, event log and stream controller.
 */
import { create } from "zustand"

import { tokenManager } from "@/api/client"
import { AGENT_API_BASE, agentApi, type AgentEvent, type AgentRun } from "@/api/agent"
import {
  fullstackApi,
  type Deployment,
  type SharedContract,
} from "@/api/fullstack"
import { useCreditStore } from "@/stores/creditStore"

export type Lane = "frontend" | "backend" | "middleware" | "deploy"
export const GEN_LANES: Lane[] = ["frontend", "backend", "middleware"]

const TERMINAL = new Set(["completed", "partial", "failed", "cancelled"])
// Auto-reconnect budget per lane for a live stream that drops while its run is
// still going (e.g. the backend restarting mid-build — the worker resumes the run
// from its persisted phase, and the lane should pick the stream back up).
const MAX_STREAM_RETRIES = 40

interface LaneState {
  runId: string | null
  run: AgentRun | null
  events: AgentEvent[]
  isStreaming: boolean
}

const emptyLane = (): LaneState => ({ runId: null, run: null, events: [], isStreaming: false })

// Per-lane stream/refresh control kept outside the reactive store.
const aborts: Record<Lane, AbortController | null> = {
  frontend: null, backend: null, middleware: null, deploy: null,
}
const timers: Record<Lane, ReturnType<typeof setTimeout> | null> = {
  frontend: null, backend: null, middleware: null, deploy: null,
}
// Pending reconnect timer + remaining retry budget, per lane.
const reconnects: Record<Lane, ReturnType<typeof setTimeout> | null> = {
  frontend: null, backend: null, middleware: null, deploy: null,
}
const retries: Record<Lane, number> = { frontend: 0, backend: 0, middleware: 0, deploy: 0 }
let boundProject: string | null = null

interface FullstackState {
  projectId: string | null
  lanes: Record<Lane, LaneState>
  contract: SharedContract | null
  deployment: Deployment | null
  starting: boolean
  startingBackend: boolean
  deploying: boolean

  startFullstack: (projectId: string) => Promise<void>
  startBackendOnly: (projectId: string) => Promise<void>
  startDeploy: (projectId: string) => Promise<void>
  hydrate: (projectId: string) => Promise<void>
  reset: () => void
}

export const useFullstackStore = create<FullstackState>()((set, get) => {
  const setLane = (lane: Lane, patch: Partial<LaneState>) =>
    set((state) => ({ lanes: { ...state.lanes, [lane]: { ...state.lanes[lane], ...patch } } }))

  const mergeEvents = (lane: Lane, incoming: AgentEvent[]) => {
    if (!incoming.length) return
    set((state) => {
      const cur = state.lanes[lane].events
      const seen = new Set(cur.map((e) => e.sequence))
      const merged = [...cur]
      for (const e of incoming) {
        if (!seen.has(e.sequence)) {
          merged.push(e)
          seen.add(e.sequence)
        }
      }
      merged.sort((a, b) => a.sequence - b.sequence)
      return { lanes: { ...state.lanes, [lane]: { ...state.lanes[lane], events: merged } } }
    })
  }

  const refresh = async (lane: Lane, runId: string) => {
    try {
      const run = await agentApi.fetchRun(runId)
      if (get().projectId !== boundProject) return
      if (get().lanes[lane].runId !== runId) return
      setLane(lane, { run })
      if (run.events) mergeEvents(lane, run.events)
      if (TERMINAL.has(run.status)) {
        void useCreditStore.getState().refreshBalance()
        // The deploy run mutates the deployment row — refresh the snapshot.
        if (lane === "deploy" && get().projectId) void refreshStatus(get().projectId as string)
      }
    } catch {
      /* transient; the next event / refresh recovers */
    }
  }

  const scheduleRefresh = (lane: Lane, runId: string) => {
    if (timers[lane]) clearTimeout(timers[lane] as ReturnType<typeof setTimeout>)
    timers[lane] = setTimeout(() => void refresh(lane, runId), 350)
  }

  const refreshStatus = async (projectId: string) => {
    try {
      const status = await fullstackApi.status(projectId)
      if (get().projectId !== projectId) return
      set({ deployment: status.deployment })
    } catch {
      /* best-effort */
    }
  }

  // A lane's live stream dropped without a clean terminal end (most likely the
  // backend restarting). Reconnect — only for the still-bound, still-running run,
  // with a bounded backing-off budget. The worker resumes the run server-side; this
  // re-subscribe (from the last sequence held) picks the continued events back up.
  const maybeReconnect = (lane: Lane, runId: string) => {
    if (get().projectId !== boundProject || get().lanes[lane].runId !== runId) return
    const run = get().lanes[lane].run
    if (run && TERMINAL.has(run.status)) {
      retries[lane] = 0
      return
    }
    if (retries[lane] >= MAX_STREAM_RETRIES) return
    retries[lane] += 1
    const delay = Math.min(1000 * retries[lane], 5000)
    if (reconnects[lane]) clearTimeout(reconnects[lane] as ReturnType<typeof setTimeout>)
    reconnects[lane] = setTimeout(() => {
      if (get().lanes[lane].runId === runId) void stream(lane, runId)
    }, delay)
  }

  const stream = async (lane: Lane, runId: string) => {
    aborts[lane]?.abort()
    aborts[lane] = new AbortController()
    if (reconnects[lane]) clearTimeout(reconnects[lane] as ReturnType<typeof setTimeout>)
    reconnects[lane] = null
    setLane(lane, { isStreaming: true })
    const token = tokenManager.getAccessToken()
    const lastSeq = get().lanes[lane].events.reduce((m, e) => Math.max(m, e.sequence), 0)
    const url = `${AGENT_API_BASE}/agent/runs/${runId}/stream${lastSeq ? `?last_sequence=${lastSeq}` : ""}`
    try {
      const response = await fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal: aborts[lane]?.signal,
      })
      if (!response.ok || !response.body) {
        setLane(lane, { isStreaming: false })
        await refresh(lane, runId)
        maybeReconnect(lane, runId)
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
            if (line.startsWith(":")) continue
            if (line.startsWith("event:")) eventType = line.slice(6).trim()
            else if (line.startsWith("data:")) dataStr += line.slice(5).trim()
          }
          if (eventType === "done") {
            retries[lane] = 0
            setLane(lane, { isStreaming: false })
            await refresh(lane, runId)
            return
          }
          if (!dataStr || eventType === "agent_delta") continue
          try {
            const event = JSON.parse(dataStr) as AgentEvent
            mergeEvents(lane, [event])
            retries[lane] = 0 // healthy stream — refresh the reconnect budget
            scheduleRefresh(lane, runId)
            if (event.event_type === "run_completed") {
              retries[lane] = 0
              setLane(lane, { isStreaming: false })
              await refresh(lane, runId)
              return
            }
          } catch {
            /* malformed chunk; snapshot refresh keeps state correct */
          }
        }
      }
      setLane(lane, { isStreaming: false })
      await refresh(lane, runId)
      maybeReconnect(lane, runId)
    } catch (error) {
      if ((error as Error).name !== "AbortError") {
        setLane(lane, { isStreaming: false })
        await refresh(lane, runId)
        maybeReconnect(lane, runId)
      }
    }
  }

  const openLane = (lane: Lane, runId: string, live: boolean) => {
    setLane(lane, { runId, run: null, events: [] })
    void refresh(lane, runId)
    if (live) void stream(lane, runId)
  }

  const reset = () => {
    boundProject = null
    for (const lane of ["frontend", "backend", "middleware", "deploy"] as Lane[]) {
      aborts[lane]?.abort()
      aborts[lane] = null
      if (timers[lane]) clearTimeout(timers[lane] as ReturnType<typeof setTimeout>)
      timers[lane] = null
      if (reconnects[lane]) clearTimeout(reconnects[lane] as ReturnType<typeof setTimeout>)
      reconnects[lane] = null
      retries[lane] = 0
    }
    set({
      projectId: null,
      lanes: { frontend: emptyLane(), backend: emptyLane(), middleware: emptyLane(), deploy: emptyLane() },
      contract: null,
      deployment: null,
      starting: false,
      startingBackend: false,
      deploying: false,
    })
  }

  return {
    projectId: null,
    lanes: { frontend: emptyLane(), backend: emptyLane(), middleware: emptyLane(), deploy: emptyLane() },
    contract: null,
    deployment: null,
    starting: false,
    startingBackend: false,
    deploying: false,

    startFullstack: async (projectId) => {
      set({ starting: true })
      try {
        boundProject = projectId
        set({ projectId })
        const result = await fullstackApi.start(projectId)
        set({ contract: result.contract })
        // Guard each lane: a full start returns all three, but the response shape
        // is now subset-aware (every lane optional).
        if (result.runs.frontend) openLane("frontend", result.runs.frontend, true)
        if (result.runs.backend) openLane("backend", result.runs.backend, true)
        if (result.runs.middleware) openLane("middleware", result.runs.middleware, true)
      } finally {
        set({ starting: false })
      }
    },

    // Regenerate ONLY the backend project (reuses the frozen contract, leaves the
    // frontend/middleware runs untouched). After it completes, the user re-deploys
    // to rebuild + run the Codex repair cycle on the fresh backend.
    startBackendOnly: async (projectId) => {
      set({ startingBackend: true })
      try {
        boundProject = projectId
        set({ projectId })
        const result = await fullstackApi.start(projectId, ["backend"])
        if (result.contract) set({ contract: result.contract })
        if (result.runs.backend) openLane("backend", result.runs.backend, true)
      } finally {
        set({ startingBackend: false })
      }
    },

    startDeploy: async (projectId) => {
      set({ deploying: true })
      try {
        boundProject = projectId
        set({ projectId })
        const { run_id } = await fullstackApi.deploy(projectId)
        openLane("deploy", run_id, true)
      } finally {
        set({ deploying: false })
      }
    },

    hydrate: async (projectId) => {
      boundProject = projectId
      set({ projectId })
      try {
        const status = await fullstackApi.status(projectId)
        if (boundProject !== projectId) return
        set({ deployment: status.deployment })
        const laneRun: Record<Lane, AgentRun | null> = {
          frontend: status.runs.frontend,
          backend: status.runs.backend,
          middleware: status.runs.middleware,
          deploy: status.runs.deploy,
        }
        for (const lane of ["frontend", "backend", "middleware", "deploy"] as Lane[]) {
          const run = laneRun[lane]
          if (run) openLane(lane, run.id, run.status === "running" || run.status === "queued")
        }
        if (status.contract_status === "ready") {
          try {
            set({ contract: await fullstackApi.contract(projectId) })
          } catch {
            /* contract is optional for display */
          }
        }
      } catch {
        /* no pipeline yet — leave lanes empty */
      }
    },

    reset,
  }
})

/**
 * True when the app can be deployed. The deploy's only HARD dependency is the
 * backend lane finishing successfully — the server builds the backend image from
 * its source. The frontend (served separately at /preview/<pid>/) and the
 * middleware init.sql (an optional fallback for non-self-migrating backends) are
 * best-effort, so a failed frontend/middleware lane must NOT permanently block
 * deploy. We still wait for the whole pipeline to SETTLE so we never deploy
 * mid-generation. Mirrors the backend's `deploy_service.deploy()` requirements.
 */
export function deployReady(lanes: Record<Lane, LaneState>): boolean {
  const backend = lanes.backend.run?.status
  const backendOk = backend === "completed" || backend === "partial"
  const settled = GEN_LANES.every((lane) => TERMINAL.has(lanes[lane].run?.status ?? ""))
  return backendOk && settled
}
