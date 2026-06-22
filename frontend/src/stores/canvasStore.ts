/**
 * Remix canvas store.
 *
 * Holds the React Flow node/edge graph for one canvas and debounce-saves the
 * whole graph back to the backend (PUT) on every edit, mirroring agentStore's
 * module-level timer pattern. Source nodes are seeded read-only from the loaded
 * CodeProject; the run + SSE node-status wiring is added in a later milestone.
 */
import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
} from "@xyflow/react"
import { create } from "zustand"

import { AGENT_API_BASE, agentApi, type AgentStepStatus } from "@/api/agent"
import {
  canvasApi,
  type AgentConfig,
  type BranchConfig,
  type Canvas,
  type CanvasEdge,
  type CanvasNode,
  type CanvasNodeConfig,
  type CanvasNodeData,
  type CanvasNodeType,
  type MergeConfig,
  type SourceKind,
} from "@/api/canvas"
import type { CodeProject } from "@/api/code"
import { tokenManager } from "@/api/client"

export type NodeRunStatus = "running" | "completed" | "failed" | "skipped"

export type FlowNode = Node<CanvasNodeData>
export type FlowEdge = Edge

// Debounced-save control kept outside the store (not reactive).
let saveTimer: ReturnType<typeof setTimeout> | null = null
let activeCanvasId: string | null = null
// Run-stream control kept outside the store (not reactive).
let runStreamAbort: AbortController | null = null

/** Map an AgentStep status onto the canvas node-status palette. */
function toNodeStatus(status: AgentStepStatus): NodeRunStatus | null {
  if (status === "running" || status === "completed" || status === "failed") return status
  if (status === "skipped") return "skipped"
  return null
}

function clearSaveTimer() {
  if (saveTimer) {
    clearTimeout(saveTimer)
    saveTimer = null
  }
}

function genId(prefix: string): string {
  const rand =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(36).slice(2, 10)
  return `${prefix}_${rand}`
}

const SOURCE_LABELS: Record<Exclude<SourceKind, "code_document">, string> = {
  requirements_doc: "需求文档",
  development_flow: "开发流程",
  style_prompt: "风格文档",
  preview: "UI 预览",
}

/** Default config for a freshly-dropped executable node. */
function defaultConfig(type: CanvasNodeType): CanvasNodeConfig {
  if (type === "agent") {
    return {
      prompt: "",
      role_ids: [],
      recipe_id: null,
      model: { provider: "claude" },
      output_target: { as_artifact: true, as_code_document: null, as_stage_version: null },
      input_join: "labeled",
    } satisfies AgentConfig
  }
  if (type === "merge") {
    return {
      separator: "\n\n---\n\n",
      labeled: true,
      title_template: "## {label}",
    } satisfies MergeConfig
  }
  if (type === "branch") {
    return {
      mode: "llm_classify",
      prompt: "判断上游结论属于以下哪一类,只回类别名。",
      branches: [
        { key: "a", label: "分支 A" },
        { key: "b", label: "分支 B" },
      ],
      default_branch: "a",
      model: { provider: "claude" },
    } satisfies BranchConfig
  }
  return {}
}

const DEFAULT_LABEL: Record<CanvasNodeType, string> = {
  source_doc: "来源文档",
  agent: "Agent",
  merge: "合并",
  branch: "条件分支",
}

/** Build read-only source nodes from the project's existing stage products. */
function seedSourceNodes(project: CodeProject): CanvasNode[] {
  const nodes: CanvasNode[] = []
  let y = 40
  const push = (kind: SourceKind, label: string, documentId?: string) => {
    nodes.push({
      id: genId("src"),
      type: "source_doc",
      position: { x: 40, y },
      data: { label, config: { source_kind: kind, document_id: documentId ?? null } },
    })
    y += 110
  }
  if (project.requirements_doc) push("requirements_doc", SOURCE_LABELS.requirements_doc)
  if (project.development_flow) push("development_flow", SOURCE_LABELS.development_flow)
  if (project.style_prompt) push("style_prompt", SOURCE_LABELS.style_prompt)
  for (const doc of project.documents || []) {
    push("code_document", doc.title || "文档", doc.id)
  }
  if (project.preview_images?.length || project.confirmed_preview_url) {
    push("preview", SOURCE_LABELS.preview)
  }
  return nodes
}

/** Strip React Flow runtime fields down to the persisted node contract. */
function toCanvasNodes(nodes: FlowNode[]): CanvasNode[] {
  return nodes.map((n) => ({
    id: n.id,
    type: (n.type || "agent") as CanvasNodeType,
    position: n.position,
    data: n.data,
  }))
}

function toCanvasEdges(edges: FlowEdge[]): CanvasEdge[] {
  return edges.map((e, index) => {
    const d = (e.data || {}) as { label?: unknown; order?: unknown }
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      sourceHandle: e.sourceHandle ?? null,
      targetHandle: e.targetHandle ?? null,
      data: {
        label: typeof d.label === "string" ? d.label : undefined,
        order: typeof d.order === "number" ? d.order : index,
      },
    }
  })
}

function fromCanvas(canvas: Canvas): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const nodes: FlowNode[] = (canvas.nodes || []).map((n) => ({
    id: n.id,
    type: n.type,
    position: n.position,
    data: n.data,
    deletable: n.type !== "source_doc",
  }))
  const edges: FlowEdge[] = (canvas.edges || []).map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.sourceHandle ?? undefined,
    targetHandle: e.targetHandle ?? undefined,
    data: (e.data ?? undefined) as Record<string, unknown> | undefined,
  }))
  return { nodes, edges }
}

interface CanvasState {
  projectId: string | null
  canvasId: string | null
  name: string
  nodes: FlowNode[]
  edges: FlowEdge[]
  status: "idle" | "loading" | "ready" | "error"
  saving: boolean
  running: boolean
  runId: string | null
  /** Per-node live execution status, keyed by node id (set during a run). */
  nodeRunStatus: Record<string, NodeRunStatus>

  loadForProject: (project: CodeProject) => Promise<void>
  onNodesChange: (changes: NodeChange<FlowNode>[]) => void
  onEdgesChange: (changes: EdgeChange<FlowEdge>[]) => void
  onConnect: (connection: Connection) => void
  addNode: (type: CanvasNodeType, position?: { x: number; y: number }) => void
  updateNodeConfig: (nodeId: string, patch: Record<string, unknown>) => void
  updateNodeLabel: (nodeId: string, label: string) => void
  removeNode: (nodeId: string) => void
  runCanvas: () => Promise<void>
  /** Fetch a node's produced artifact text from the last run, if any. */
  fetchNodeOutput: (nodeId: string) => Promise<string | null>
  save: () => Promise<void>
  reset: () => void
}

export const useCanvasStore = create<CanvasState>()((set, get) => {
  const scheduleSave = () => {
    clearSaveTimer()
    saveTimer = setTimeout(() => void get().save(), 600)
  }

  // Consume the run's SSE stream, mapping step lifecycle events onto node colors.
  // The step's agent_key IS the node id (set by the canvas workflow). A final
  // snapshot fetch settles authoritative statuses (skipped / failed included).
  const streamRun = async (runId: string) => {
    runStreamAbort?.abort()
    runStreamAbort = new AbortController()
    const token = tokenManager.getAccessToken()
    const settle = async () => {
      try {
        const run = await agentApi.fetchRun(runId)
        const map: Record<string, NodeRunStatus> = {}
        for (const step of run.steps || []) {
          const mapped = toNodeStatus(step.status)
          if (mapped) map[step.agent_key] = mapped
        }
        if (get().runId === runId) set({ nodeRunStatus: map })
      } catch {
        // Snapshot will be retried implicitly on the next run.
      }
    }
    try {
      const response = await fetch(`${AGENT_API_BASE}/agent/runs/${runId}/stream`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal: runStreamAbort.signal,
      })
      if (!response.ok || !response.body) {
        await settle()
        set({ running: false })
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
            await settle()
            set({ running: false })
            return
          }
          if (!dataStr || eventType === "agent_delta") continue
          try {
            const event = JSON.parse(dataStr) as {
              event_type: string
              payload?: { agent_key?: string }
            }
            const nodeId = event.payload?.agent_key
            if (nodeId && event.event_type === "step_started") {
              set((s) => ({ nodeRunStatus: { ...s.nodeRunStatus, [nodeId]: "running" } }))
            } else if (nodeId && event.event_type === "step_completed") {
              set((s) => ({ nodeRunStatus: { ...s.nodeRunStatus, [nodeId]: "completed" } }))
            }
            if (event.event_type === "run_completed") {
              await settle()
              set({ running: false })
              return
            }
          } catch {
            // Ignore malformed chunk; the settle() snapshot keeps state correct.
          }
        }
      }
      await settle()
      set({ running: false })
    } catch (error) {
      if ((error as Error).name !== "AbortError") {
        await settle()
        set({ running: false })
      }
    }
  }

  return {
    projectId: null,
    canvasId: null,
    name: "未命名画布",
    nodes: [],
    edges: [],
    status: "idle",
    saving: false,
    running: false,
    runId: null,
    nodeRunStatus: {},

    // Load (or lazily create) the project's first canvas. A new canvas is seeded
    // with read-only source nodes built from the project's existing products.
    loadForProject: async (project) => {
      clearSaveTimer()
      set({ status: "loading", projectId: project.id })
      try {
        const summaries = await canvasApi.list(project.id)
        let canvas: Canvas
        if (summaries.length) {
          canvas = await canvasApi.get(project.id, summaries[0].id)
        } else {
          canvas = await canvasApi.create(project.id, {
            name: "默认画布",
            nodes: seedSourceNodes(project),
          })
        }
        activeCanvasId = canvas.id
        const { nodes, edges } = fromCanvas(canvas)
        set({
          canvasId: canvas.id,
          name: canvas.name,
          nodes,
          edges,
          status: "ready",
          nodeRunStatus: {},
        })
      } catch {
        set({ status: "error" })
      }
    },

    onNodesChange: (changes) => {
      set((state) => ({ nodes: applyNodeChanges(changes, state.nodes) }))
      scheduleSave()
    },

    onEdgesChange: (changes) => {
      set((state) => ({ edges: applyEdgeChanges(changes, state.edges) }))
      scheduleSave()
    },

    onConnect: (connection) => {
      set((state) => ({ edges: addEdge(connection, state.edges) }))
      scheduleSave()
    },

    addNode: (type, position) => {
      const node: FlowNode = {
        id: genId(type === "source_doc" ? "src" : type),
        type,
        position: position || { x: 320 + Math.random() * 120, y: 120 + Math.random() * 120 },
        data: { label: DEFAULT_LABEL[type], config: defaultConfig(type) },
        deletable: type !== "source_doc",
      }
      set((state) => ({ nodes: [...state.nodes, node] }))
      scheduleSave()
    },

    updateNodeConfig: (nodeId, patch) => {
      set((state) => ({
        nodes: state.nodes.map((n) =>
          n.id === nodeId
            ? { ...n, data: { ...n.data, config: { ...(n.data.config as object), ...patch } } }
            : n
        ),
      }))
      scheduleSave()
    },

    updateNodeLabel: (nodeId, label) => {
      set((state) => ({
        nodes: state.nodes.map((n) =>
          n.id === nodeId ? { ...n, data: { ...n.data, label } } : n
        ),
      }))
      scheduleSave()
    },

    removeNode: (nodeId) => {
      set((state) => ({
        nodes: state.nodes.filter((n) => n.id !== nodeId),
        edges: state.edges.filter((e) => e.source !== nodeId && e.target !== nodeId),
      }))
      scheduleSave()
    },

    fetchNodeOutput: async (nodeId) => {
      const runId = get().runId
      if (!runId) return null
      try {
        const run = await agentApi.fetchRun(runId)
        const artifact = (run.artifacts || []).find((a) => a.domain_ref_id === nodeId)
        return artifact?.content_text ?? null
      } catch {
        return null
      }
    },

    // Persist first (the executor reads the latest graph from the DB), then start
    // a code_canvas_generation run and stream node statuses onto the canvas.
    runCanvas: async () => {
      const { projectId, canvasId, running } = get()
      if (!projectId || !canvasId || running) return
      await get().save()
      set({ running: true, nodeRunStatus: {} })
      try {
        const result = await agentApi.createRun({
          domain: "code",
          workflow: "code_canvas_generation",
          resource_type: "code_project",
          resource_id: projectId,
          config: { canvas_id: canvasId },
        })
        set({ runId: result.run_id })
        void streamRun(result.run_id)
      } catch (error) {
        set({ running: false })
        throw error
      }
    },

    save: async () => {
      const { projectId, canvasId, name, nodes, edges } = get()
      if (!projectId || !canvasId) return
      set({ saving: true })
      try {
        await canvasApi.update(projectId, canvasId, {
          name,
          nodes: toCanvasNodes(nodes),
          edges: toCanvasEdges(edges),
        })
      } catch {
        // Transient; the next edit reschedules a save.
      } finally {
        if (canvasId === activeCanvasId) set({ saving: false })
      }
    },

    reset: () => {
      clearSaveTimer()
      runStreamAbort?.abort()
      runStreamAbort = null
      activeCanvasId = null
      set({
        projectId: null,
        canvasId: null,
        name: "未命名画布",
        nodes: [],
        edges: [],
        status: "idle",
        saving: false,
        running: false,
        runId: null,
        nodeRunStatus: {},
      })
    },
  }
})
