/** Agent node: free prompt + role + per-node model; consumes wired inputs. */
import { Handle, Position, type NodeProps } from "@xyflow/react"
import { Bot } from "lucide-react"

import type { AgentConfig } from "@/api/canvas"
import type { FlowNode } from "@/stores/canvasStore"
import { NodeShell } from "./NodeShell"

export function AgentNode({ id, data, selected }: NodeProps<FlowNode>) {
  const config = data.config as AgentConfig
  const provider = config.model?.provider || "claude"
  const promptPreview = config.prompt?.trim() || "未填写指令"
  return (
    <>
      <Handle type="target" position={Position.Left} />
      <NodeShell
        nodeId={id}
        title={data.label}
        subtitle={`Agent · ${provider}`}
        icon={<Bot className="h-4 w-4" />}
        accent="bg-violet-100 text-violet-600"
        selected={selected}
      >
        <p className="line-clamp-3 whitespace-pre-wrap">{promptPreview}</p>
        {config.role_ids?.length ? (
          <p className="mt-1 text-[11px] text-muted-foreground/80">角色: {config.role_ids.join(", ")}</p>
        ) : null}
      </NodeShell>
      <Handle type="source" position={Position.Right} />
    </>
  )
}
