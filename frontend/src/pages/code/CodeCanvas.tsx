/**
 * Remix canvas page (n8n-style). Existing stage products are seeded as read-only
 * source nodes; the user drops agent / merge / branch nodes and wires them. The
 * whole graph debounce-saves to the backend. Executing the graph (a
 * code_canvas_generation run) is wired in a later milestone.
 */
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import { ArrowLeft, Bot, Combine, GitBranch, Loader2, Play } from "lucide-react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Link, useParams } from "react-router-dom"
import { toast } from "sonner"

import { EXISTING_SOURCE_KINDS, type SourceKind } from "@/api/canvas"
import { NodeConfigPanel } from "@/components/canvas/NodeConfigPanel"
import { canvasNodeTypes } from "@/components/canvas/nodeTypes"
import { Button } from "@/components/ui/button"
import { useCanvasStore, type FlowEdge, type FlowNode } from "@/stores/canvasStore"
import { useCodeStore } from "@/stores/codeStore"

function CanvasInner() {
  const { t } = useTranslation("canvas")
  const { projectId } = useParams<{ projectId: string }>()
  const project = useCodeStore((s) => s.project)
  const loadProject = useCodeStore((s) => s.loadProject)

  const nodes = useCanvasStore((s) => s.nodes)
  const edges = useCanvasStore((s) => s.edges)
  const status = useCanvasStore((s) => s.status)
  const saving = useCanvasStore((s) => s.saving)
  const running = useCanvasStore((s) => s.running)
  const onNodesChange = useCanvasStore((s) => s.onNodesChange)
  const onEdgesChange = useCanvasStore((s) => s.onEdgesChange)
  const onConnect = useCanvasStore((s) => s.onConnect)
  const addNode = useCanvasStore((s) => s.addNode)
  const addStageNode = useCanvasStore((s) => s.addStageNode)
  const addSourceNode = useCanvasStore((s) => s.addSourceNode)
  const nodeContracts = useCanvasStore((s) => s.nodeContracts)
  const loadNodeContracts = useCanvasStore((s) => s.loadNodeContracts)
  const runCanvas = useCanvasStore((s) => s.runCanvas)
  const freezeCanvas = useCanvasStore((s) => s.freezeCanvas)
  const paused = useCanvasStore((s) => s.paused)
  const reviewStage = useCanvasStore((s) => s.reviewStage)
  const resumeCanvas = useCanvasStore((s) => s.resumeCanvas)
  const loadForProject = useCanvasStore((s) => s.loadForProject)
  const reset = useCanvasStore((s) => s.reset)

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selectedNode = nodes.find((n) => n.id === selectedId) || null

  const handleRun = async () => {
    try {
      await runCanvas()
    } catch (err) {
      const message =
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("runFailed")
      toast.error(message)
    }
  }

  const handleFreeze = async () => {
    try {
      const pinned = await freezeCanvas()
      toast.success(t("toolbar.frozen", { count: pinned }))
    } catch {
      toast.error(t("toolbar.freezeFailed"))
    }
  }

  const [reviseText, setReviseText] = useState("")
  const reviewLabel = nodes.find((n) => n.id === reviewStage)?.data.label ?? reviewStage ?? ""
  const handleResume = async (action: "approve" | "revise") => {
    try {
      await resumeCanvas(action, action === "revise" ? reviseText : "")
      setReviseText("")
    } catch {
      toast.error(t("review.failed"))
    }
  }

  // Ensure the project is loaded (source nodes are seeded from its products).
  useEffect(() => {
    if (projectId && project?.id !== projectId) void loadProject(projectId)
  }, [projectId, project?.id, loadProject])

  // Once the project for this route is in hand, load/create its canvas.
  useEffect(() => {
    if (project?.id && project.id === projectId) void loadForProject(project)
    return () => reset()
  }, [project, projectId, loadForProject, reset])

  // Load the typed node-contract catalog once (drives the stage palette).
  useEffect(() => {
    void loadNodeContracts()
  }, [loadNodeContracts])

  return (
    <div className="flex h-screen flex-col">
      <header className="flex flex-wrap items-center gap-2 border-b px-4 py-2">
        <Button asChild variant="ghost" size="sm">
          <Link to={`/code/${projectId}`}>
            <ArrowLeft className="mr-1 h-4 w-4" />
            <span className="hidden sm:inline">{t("back")}</span>
          </Link>
        </Button>
        <span className="text-sm font-medium">{t("title")}</span>
        <div className="mx-2 h-4 w-px bg-border" />
        <Button variant="outline" size="sm" onClick={() => addNode("agent")}>
          <Bot className="mr-1 h-4 w-4" />
          <span className="hidden sm:inline">{t("toolbar.addAgent")}</span>
        </Button>
        <Button variant="outline" size="sm" onClick={() => addNode("merge")}>
          <Combine className="mr-1 h-4 w-4" />
          <span className="hidden sm:inline">{t("toolbar.addMerge")}</span>
        </Button>
        <Button variant="outline" size="sm" onClick={() => addNode("branch")}>
          <GitBranch className="mr-1 h-4 w-4" />
          <span className="hidden sm:inline">{t("toolbar.addBranch")}</span>
        </Button>
        {Object.keys(nodeContracts).length > 0 && (
          <select
            className="h-8 rounded-md border bg-background px-2 text-sm"
            value=""
            onChange={(e) => {
              const key = e.target.value
              if (!key) return
              addStageNode(key, t(`stage.name.${key}`, { defaultValue: key }))
            }}
          >
            <option value="">{t("toolbar.addStage")}</option>
            {Object.values(nodeContracts).map((c) => (
              <option key={c.node_type} value={c.node_type} disabled={!c.executable}>
                {t(`stage.name.${c.node_type}`, { defaultValue: c.node_type })}
                {c.executable ? "" : " ·"}
              </option>
            ))}
          </select>
        )}
        <select
          className="h-8 rounded-md border bg-background px-2 text-sm"
          value=""
          onChange={(e) => {
            const kind = e.target.value as SourceKind
            if (!kind) return
            const short = kind.replace("existing_", "")
            addSourceNode(kind, t(`source.existing.${short}`, { defaultValue: kind }))
          }}
        >
          <option value="">{t("toolbar.addSource")}</option>
          {EXISTING_SOURCE_KINDS.map((k) => (
            <option key={k} value={k}>
              {t(`source.existing.${k.replace("existing_", "")}`, { defaultValue: k })}
            </option>
          ))}
        </select>
        <div className="ml-auto flex items-center gap-3">
          <span className="text-xs text-muted-foreground">
            {saving ? (
              <span className="flex items-center gap-1">
                <Loader2 className="h-3 w-3 animate-spin" /> {t("status.saving")}
              </span>
            ) : status === "ready" ? (
              t("status.saved")
            ) : status === "loading" ? (
              t("status.loading")
            ) : status === "error" ? (
              <span className="text-red-500">{t("status.loadFailed")}</span>
            ) : null}
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={handleFreeze}
            disabled={running || status !== "ready"}
          >
            <span className="hidden sm:inline">{t("toolbar.freeze")}</span>
            <span className="sm:hidden">📌</span>
          </Button>
          <Button size="sm" onClick={handleRun} disabled={running || status !== "ready"}>
            {running ? (
              <Loader2 className="mr-1 h-4 w-4 animate-spin" />
            ) : (
              <Play className="mr-1 h-4 w-4" />
            )}
            <span className="hidden sm:inline">
              {running ? t("toolbar.running") : t("toolbar.run")}
            </span>
          </Button>
        </div>
      </header>
      {paused && (
        <div className="flex flex-wrap items-center gap-2 border-b bg-amber-50 px-4 py-2 text-sm dark:bg-amber-950/30">
          <span className="font-medium">{t("review.awaiting", { stage: reviewLabel })}</span>
          <input
            className="h-8 min-w-48 flex-1 rounded-md border bg-background px-2 text-sm"
            placeholder={t("review.placeholder")}
            value={reviseText}
            onChange={(e) => setReviseText(e.target.value)}
          />
          <Button
            size="sm"
            variant="outline"
            disabled={!reviseText.trim()}
            onClick={() => handleResume("revise")}
          >
            {t("review.revise")}
          </Button>
          <Button size="sm" onClick={() => handleResume("approve")}>
            {t("review.approve")}
          </Button>
        </div>
      )}
      <div className="flex min-h-0 flex-1">
        <div className="flex-1">
          <ReactFlow<FlowNode, FlowEdge>
            nodes={nodes}
            edges={edges}
            nodeTypes={canvasNodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={(_, node) => setSelectedId(node.id)}
            onPaneClick={() => setSelectedId(null)}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background />
            <Controls />
            <MiniMap pannable zoomable />
          </ReactFlow>
        </div>
        {selectedNode && (
          <NodeConfigPanel
            key={selectedNode.id}
            node={selectedNode}
            onClose={() => setSelectedId(null)}
          />
        )}
      </div>
    </div>
  )
}

export function CodeCanvas() {
  return (
    <ReactFlowProvider>
      <CanvasInner />
    </ReactFlowProvider>
  )
}
