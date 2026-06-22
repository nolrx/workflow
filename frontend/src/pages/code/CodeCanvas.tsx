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
  const runCanvas = useCanvasStore((s) => s.runCanvas)
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

  // Ensure the project is loaded (source nodes are seeded from its products).
  useEffect(() => {
    if (projectId && project?.id !== projectId) void loadProject(projectId)
  }, [projectId, project?.id, loadProject])

  // Once the project for this route is in hand, load/create its canvas.
  useEffect(() => {
    if (project?.id && project.id === projectId) void loadForProject(project)
    return () => reset()
  }, [project, projectId, loadForProject, reset])

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-2 border-b px-4 py-2">
        <Button asChild variant="ghost" size="sm">
          <Link to={`/code/${projectId}`}>
            <ArrowLeft className="mr-1 h-4 w-4" /> {t("back")}
          </Link>
        </Button>
        <span className="text-sm font-medium">{t("title")}</span>
        <div className="mx-2 h-4 w-px bg-border" />
        <Button variant="outline" size="sm" onClick={() => addNode("agent")}>
          <Bot className="mr-1 h-4 w-4" /> {t("toolbar.addAgent")}
        </Button>
        <Button variant="outline" size="sm" onClick={() => addNode("merge")}>
          <Combine className="mr-1 h-4 w-4" /> {t("toolbar.addMerge")}
        </Button>
        <Button variant="outline" size="sm" onClick={() => addNode("branch")}>
          <GitBranch className="mr-1 h-4 w-4" /> {t("toolbar.addBranch")}
        </Button>
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
          <Button size="sm" onClick={handleRun} disabled={running || status !== "ready"}>
            {running ? (
              <Loader2 className="mr-1 h-4 w-4 animate-spin" />
            ) : (
              <Play className="mr-1 h-4 w-4" />
            )}
            {running ? t("toolbar.running") : t("toolbar.run")}
          </Button>
        </div>
      </header>
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
