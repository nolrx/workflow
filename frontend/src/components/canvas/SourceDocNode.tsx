/** Read-only source node: references an existing stage product as an input. */
import { Handle, Position, type NodeProps } from "@xyflow/react"
import { FileText } from "lucide-react"

import type { SourceDocConfig } from "@/api/canvas"
import type { FlowNode } from "@/stores/canvasStore"
import { NodeShell } from "./NodeShell"

const KIND_LABEL: Record<string, string> = {
  requirements_doc: "需求文档",
  development_flow: "开发流程",
  style_prompt: "风格文档",
  preview: "UI 预览",
  code_document: "拆分文档",
}

export function SourceDocNode({ id, data, selected }: NodeProps<FlowNode>) {
  const config = data.config as SourceDocConfig
  return (
    <>
      <NodeShell
        nodeId={id}
        title={data.label}
        subtitle={`来源 · ${KIND_LABEL[config.source_kind] || config.source_kind}`}
        icon={<FileText className="h-4 w-4" />}
        accent="bg-amber-100 text-amber-600"
        selected={selected}
      />
      <Handle type="source" position={Position.Right} />
    </>
  )
}
