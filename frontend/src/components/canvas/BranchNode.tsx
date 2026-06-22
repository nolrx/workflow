/**
 * Branch node: routes downstream by a classified key. Each branch option exposes
 * its own source handle (id = branch key); an edge's sourceHandle records which
 * branch it belongs to, so the executor can prune the unselected subgraphs.
 */
import { Handle, Position, type NodeProps } from "@xyflow/react"
import { GitBranch } from "lucide-react"

import type { BranchConfig } from "@/api/canvas"
import type { FlowNode } from "@/stores/canvasStore"
import { NodeShell } from "./NodeShell"

export function BranchNode({ id, data, selected }: NodeProps<FlowNode>) {
  const config = data.config as BranchConfig
  const branches = config.branches || []
  return (
    <>
      <Handle type="target" position={Position.Left} />
      <NodeShell
        nodeId={id}
        title={data.label}
        subtitle={`条件分支 · ${config.mode === "keyword" ? "关键词" : "AI 判定"}`}
        icon={<GitBranch className="h-4 w-4" />}
        accent="bg-orange-100 text-orange-600"
        selected={selected}
      >
        <ul className="space-y-1">
          {branches.map((b) => (
            <li key={b.key} className="relative flex items-center justify-between pr-3">
              <span className="truncate">{b.label}</span>
              {b.key === config.default_branch ? (
                <span className="text-[10px] text-muted-foreground">默认</span>
              ) : null}
              <Handle
                type="source"
                position={Position.Right}
                id={b.key}
                style={{ right: -18, top: "50%" }}
              />
            </li>
          ))}
        </ul>
      </NodeShell>
    </>
  )
}
