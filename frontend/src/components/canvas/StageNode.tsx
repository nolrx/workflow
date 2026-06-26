/**
 * Typed stage node: runs a real generation stage via its node contract.
 *
 * Unlike the freeform agent node, a stage node exposes one TYPED handle per
 * declared input/output port (handle id = port name), so edges carry the
 * port-level typing the backend validates. Ports are read from the contract
 * catalog held in the canvas store. A stage whose executor isn't wired yet
 * (container / preview / deploy) is rendered greyed-out.
 */
import { Handle, Position, type NodeProps } from "@xyflow/react"
import { Boxes } from "lucide-react"
import { useTranslation } from "react-i18next"

import type { StageConfig } from "@/api/canvas"
import { useCanvasStore, type FlowNode } from "@/stores/canvasStore"
import { NodeShell } from "./NodeShell"

/** Spread N handles evenly down a node's side. */
function handleTop(index: number, count: number): string {
  return `${((index + 1) / (count + 1)) * 100}%`
}

/** Drop the "code:" / "core:" namespace for a compact port-type label. */
function shortType(type: string): string {
  return type.replace(/^[a-z]+:/, "")
}

export function StageNode({ id, data, selected }: NodeProps<FlowNode>) {
  const { t } = useTranslation("canvas")
  const config = data.config as StageConfig
  const contract = useCanvasStore((s) => s.nodeContracts[config.contract_key])
  const inputs = contract?.inputs ?? []
  const outputs = contract?.outputs ?? []
  const runnable = contract ? contract.executable : true
  const pinned = Boolean(config.prompt_pin?.hash)

  const subtitle = contract
    ? `${t("stage.node")}${runnable ? "" : " · " + t("stage.unavailable")}${
        pinned ? " · " + t("stage.pinned") : ""
      }`
    : config.contract_key

  return (
    <>
      {inputs.map((port, i) => (
        <Handle
          key={`in-${port.name}`}
          id={port.name}
          type="target"
          position={Position.Left}
          style={{ top: handleTop(i, inputs.length) }}
          title={`${port.name}: ${port.type}${port.required ? " *" : ""}`}
        />
      ))}

      <NodeShell
        nodeId={id}
        title={data.label}
        subtitle={subtitle}
        icon={<Boxes className="h-4 w-4" />}
        accent={runnable ? "bg-sky-100 text-sky-600" : "bg-slate-100 text-slate-400"}
        selected={selected}
      >
        <div className="space-y-0.5">
          {inputs.map((port) => (
            <p key={port.name} className="text-[11px]">
              ← {port.name}
              <span className="text-muted-foreground/70"> :{shortType(port.type)}</span>
              {port.required ? <span className="text-red-500"> *</span> : null}
            </p>
          ))}
          {outputs.map((port) => (
            <p key={port.name} className="text-right text-[11px]">
              {port.name}
              <span className="text-muted-foreground/70"> :{shortType(port.type)}</span> →
            </p>
          ))}
        </div>
      </NodeShell>

      {outputs.map((port, i) => (
        <Handle
          key={`out-${port.name}`}
          id={port.name}
          type="source"
          position={Position.Right}
          style={{ top: handleTop(i, outputs.length) }}
          title={`${port.name}: ${port.type}`}
        />
      ))}
    </>
  )
}
