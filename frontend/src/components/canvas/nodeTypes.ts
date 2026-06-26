/** React Flow custom node registry, keyed by the persisted node `type`. */
import type { NodeTypes } from "@xyflow/react"

import { AgentNode } from "./AgentNode"
import { BranchNode } from "./BranchNode"
import { MergeNode } from "./MergeNode"
import { SourceDocNode } from "./SourceDocNode"
import { StageNode } from "./StageNode"

export const canvasNodeTypes: NodeTypes = {
  source_doc: SourceDocNode,
  agent: AgentNode,
  merge: MergeNode,
  branch: BranchNode,
  stage: StageNode,
}
