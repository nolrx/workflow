/** Merge node: concatenates all wired upstream outputs (no LLM call). */
import { Handle, Position, type NodeProps } from "@xyflow/react"
import { Combine } from "lucide-react"

import type { MergeConfig } from "@/api/canvas"
import type { FlowNode } from "@/stores/canvasStore"
import { NodeShell } from "./NodeShell"

export function MergeNode({ id, data, selected }: NodeProps<FlowNode>) {
  const config = data.config as MergeConfig
  return (
    <>
      <Handle type="target" position={Position.Left} />
      <NodeShell
        nodeId={id}
        title={data.label}
        subtitle={config.labeled ? "合并 · 带标题" : "合并 · 纯拼接"}
        icon={<Combine className="h-4 w-4" />}
        accent="bg-sky-100 text-sky-600"
        selected={selected}
      />
      <Handle type="source" position={Position.Right} />
    </>
  )
}
