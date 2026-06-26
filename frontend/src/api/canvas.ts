/**
 * Remix canvas API client (n8n-style node graph attached to a Code project).
 *
 * The node/edge shape is the shared contract with the backend DAG executor: the
 * backend only reads id / type / data.config / source / target / sourceHandle and
 * ignores pure-UI fields (position, etc.). Executing a canvas reuses the Agent
 * Swarm run API (workflow=code_canvas_generation, config={canvas_id}).
 */
import { api } from "@/api/client"

export type CanvasNodeType = "source_doc" | "agent" | "merge" | "branch" | "stage"

/** What a source node references on the project (read-only input). */
export type SourceKind =
  | "requirements_doc"
  | "development_flow"
  | "style_prompt"
  | "preview"
  | "code_document"
  // "Existing built product" sources: reuse a current product (e.g. an existing
  // frontend) instead of regenerating it — wire into a deploy / build input.
  | "existing_frontend"
  | "existing_backend"
  | "existing_contract"
  | "existing_middleware"

/** The "existing built product" source kinds — added manually, not auto-seeded. */
export const EXISTING_SOURCE_KINDS: SourceKind[] = [
  "existing_frontend",
  "existing_backend",
  "existing_contract",
  "existing_middleware",
]

export interface SourceDocConfig {
  source_kind: SourceKind
  document_id?: string | null
}

/** Per-node text model override. Only text-capable providers are offered. */
export interface NodeModel {
  provider: "claude" | "gemini"
  model_name?: string | null
  base_url?: string | null
}

export interface AgentOutputTarget {
  as_artifact: boolean
  as_code_document?: { document_type: string } | null
  as_stage_version?: { stage: string } | null
}

export interface AgentConfig {
  prompt: string
  role_ids: string[]
  recipe_id?: string | null
  model?: NodeModel | null
  output_target: AgentOutputTarget
  input_join: "concat" | "labeled"
}

export interface MergeConfig {
  separator: string
  labeled: boolean
  title_template: string
}

export interface BranchOption {
  key: string
  label: string
  keywords?: string[]
}

export interface BranchConfig {
  mode: "llm_classify" | "keyword"
  prompt?: string
  branches: BranchOption[]
  default_branch: string
  model?: NodeModel | null
}

/** A typed stage node: runs a real generation stage via its node contract. */
export interface PromptPin {
  key: string
  version: number | null
  hash: string | null
}

export interface StageConfig {
  contract_key: string
  prompt?: string
  model?: NodeModel | null
  /** Frozen prompt version stamped at publish time (absent ⇒ follow live HEAD). */
  prompt_pin?: PromptPin | null
}

/** One typed port on a node contract (catalog view). */
export interface NodeContractPort {
  name: string
  type: string
  required?: boolean
}

/** A node contract as exposed to the canvas palette (the composable "component"). */
export interface NodeContractCatalogItem {
  node_type: string
  role: string
  review_gate: boolean
  executable: boolean
  inputs: NodeContractPort[]
  outputs: NodeContractPort[]
  prompt_key: string | null
}

export type CanvasNodeConfig =
  | SourceDocConfig
  | AgentConfig
  | MergeConfig
  | BranchConfig
  | StageConfig
  | Record<string, unknown>

export interface CanvasNodeData {
  label: string
  config: CanvasNodeConfig
  // Index signature so this satisfies @xyflow/react v12's Node<T extends
  // Record<string, unknown>> constraint (used via FlowNode = Node<CanvasNodeData>).
  [key: string]: unknown
}

/** Persisted node shape (a strict subset of a React Flow node). */
export interface CanvasNode {
  id: string
  type: CanvasNodeType
  position: { x: number; y: number }
  data: CanvasNodeData
}

export interface CanvasEdge {
  id: string
  source: string
  target: string
  sourceHandle?: string | null
  targetHandle?: string | null
  data?: { label?: string; order?: number }
}

export interface CanvasViewport {
  x?: number
  y?: number
  zoom?: number
}

export interface CanvasSummary {
  id: string
  project_id: string
  user_id: string
  team_id: string | null
  name: string
  last_run_id: string | null
  created_at: string | null
  updated_at: string | null
}

export interface Canvas extends CanvasSummary {
  nodes: CanvasNode[]
  edges: CanvasEdge[]
  viewport: CanvasViewport
}

interface Envelope<T> {
  data: T
  message?: string
}

export interface CanvasGraphInput {
  name?: string
  nodes?: CanvasNode[]
  edges?: CanvasEdge[]
  viewport?: CanvasViewport
}

export const canvasApi = {
  list: async (projectId: string): Promise<CanvasSummary[]> => {
    const res = await api.get<Envelope<{ canvases: CanvasSummary[] }>>(
      `/code/projects/${projectId}/canvases`
    )
    return res.data.canvases
  },
  create: async (projectId: string, body: CanvasGraphInput = {}): Promise<Canvas> => {
    const res = await api.post<Envelope<{ canvas: Canvas }>>(
      `/code/projects/${projectId}/canvases`,
      body
    )
    return res.data.canvas
  },
  get: async (projectId: string, canvasId: string): Promise<Canvas> => {
    const res = await api.get<Envelope<{ canvas: Canvas }>>(
      `/code/projects/${projectId}/canvases/${canvasId}`
    )
    return res.data.canvas
  },
  update: async (
    projectId: string,
    canvasId: string,
    body: CanvasGraphInput
  ): Promise<Canvas> => {
    const res = await api.put<Envelope<{ canvas: Canvas }>>(
      `/code/projects/${projectId}/canvases/${canvasId}`,
      body
    )
    return res.data.canvas
  },
  remove: async (projectId: string, canvasId: string): Promise<void> => {
    await api.delete<Envelope<unknown>>(`/code/projects/${projectId}/canvases/${canvasId}`)
  },
  /** The typed node-contract catalog that drives the typed-node palette. */
  nodeContracts: async (): Promise<NodeContractCatalogItem[]> => {
    const res = await api.get<Envelope<{ node_contracts: NodeContractCatalogItem[] }>>(
      "/code/node-contracts"
    )
    return res.data.node_contracts
  },
  /** Freeze typed stage prompts to exact versions (reproducible runs). */
  freeze: async (
    projectId: string,
    canvasId: string
  ): Promise<{ canvas: Canvas; pinned: number }> => {
    const res = await api.post<Envelope<{ canvas: Canvas; pinned: number }>>(
      `/code/projects/${projectId}/canvases/${canvasId}/freeze`,
      {}
    )
    return res.data
  },
}
