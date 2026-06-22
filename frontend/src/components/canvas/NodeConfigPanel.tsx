/**
 * Right-hand inspector for the selected canvas node. Edits the node's label and
 * type-specific config (agent prompt/role/model/output-target, merge join,
 * branch options) and, after a run, shows the node's produced output.
 */
import { Trash2, X } from "lucide-react"
import { type ReactNode, useState } from "react"

import type {
  AgentConfig,
  BranchConfig,
  BranchOption,
  MergeConfig,
  NodeModel,
  SourceDocConfig,
} from "@/api/canvas"
import { Button } from "@/components/ui/button"
import type { FlowNode } from "@/stores/canvasStore"
import { useCanvasStore } from "@/stores/canvasStore"
import { ModelSelector } from "./ModelSelector"

const SOURCE_KIND_LABEL: Record<string, string> = {
  requirements_doc: "需求文档",
  development_flow: "开发流程",
  style_prompt: "风格文档",
  preview: "UI 预览",
  code_document: "拆分文档",
}

const STAGE_OPTIONS = [
  { value: "requirements", label: "需求" },
  { value: "flow", label: "开发流程" },
  { value: "style", label: "风格" },
]

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <label className="text-xs font-medium text-muted-foreground">{label}</label>
      {children}
    </div>
  )
}

const inputCls = "w-full rounded-md border bg-background px-2 py-1.5 text-sm"

interface NodeConfigPanelProps {
  node: FlowNode
  onClose: () => void
}

export function NodeConfigPanel({ node, onClose }: NodeConfigPanelProps) {
  const updateNodeConfig = useCanvasStore((s) => s.updateNodeConfig)
  const updateNodeLabel = useCanvasStore((s) => s.updateNodeLabel)
  const removeNode = useCanvasStore((s) => s.removeNode)
  const fetchNodeOutput = useCanvasStore((s) => s.fetchNodeOutput)
  const runId = useCanvasStore((s) => s.runId)

  const [output, setOutput] = useState<string | null>(null)
  const [loadingOutput, setLoadingOutput] = useState(false)

  // The panel is remounted (keyed by node id) on selection change, so per-node
  // output state resets naturally — no reset effect needed.
  const config = node.data.config as Record<string, unknown>
  const patch = (p: Record<string, unknown>) => updateNodeConfig(node.id, p)

  const loadOutput = async () => {
    setLoadingOutput(true)
    setOutput((await fetchNodeOutput(node.id)) ?? "（暂无产出）")
    setLoadingOutput(false)
  }

  return (
    <aside className="flex h-full w-80 flex-col border-l bg-card">
      <div className="flex items-center justify-between border-b px-3 py-2">
        <span className="text-sm font-medium">节点设置</span>
        <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onClose}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto p-3">
        <Field label="名称">
          <input
            className={inputCls}
            value={node.data.label}
            onChange={(e) => updateNodeLabel(node.id, e.target.value)}
            disabled={node.type === "source_doc"}
          />
        </Field>

        {node.type === "source_doc" && (
          <p className="rounded-md bg-muted/50 px-2 py-2 text-xs text-muted-foreground">
            只读来源节点 ·{" "}
            {SOURCE_KIND_LABEL[(config as unknown as SourceDocConfig).source_kind] ||
              (config as unknown as SourceDocConfig).source_kind}
            。内容在执行时实时读取，不可在画布编辑。
          </p>
        )}

        {node.type === "agent" && (
          <AgentEditor config={config as unknown as AgentConfig} patch={patch} />
        )}
        {node.type === "merge" && (
          <MergeEditor config={config as unknown as MergeConfig} patch={patch} />
        )}
        {node.type === "branch" && (
          <BranchEditor config={config as unknown as BranchConfig} patch={patch} />
        )}

        {runId && (
          <div className="space-y-1.5 border-t pt-3">
            <Button variant="outline" size="sm" onClick={loadOutput} disabled={loadingOutput}>
              {loadingOutput ? "加载中…" : "查看产出"}
            </Button>
            {output !== null && (
              <pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded-md bg-muted/50 p-2 text-xs">
                {output}
              </pre>
            )}
          </div>
        )}
      </div>

      {node.type !== "source_doc" && (
        <div className="border-t p-3">
          <Button
            variant="ghost"
            size="sm"
            className="text-red-500 hover:text-red-600"
            onClick={() => {
              removeNode(node.id)
              onClose()
            }}
          >
            <Trash2 className="mr-1 h-4 w-4" /> 删除节点
          </Button>
        </div>
      )}
    </aside>
  )
}

function AgentEditor({
  config,
  patch,
}: {
  config: AgentConfig
  patch: (p: Record<string, unknown>) => void
}) {
  const target = config.output_target || { as_artifact: true }
  const setTarget = (p: Record<string, unknown>) =>
    patch({ output_target: { ...target, ...p } })
  return (
    <>
      <Field label="指令">
        <textarea
          className={`${inputCls} min-h-24`}
          placeholder="描述这个 Agent 要基于上游输入做什么…"
          value={config.prompt || ""}
          onChange={(e) => patch({ prompt: e.target.value })}
        />
      </Field>
      <Field label="角色（prompt_library id，逗号分隔）">
        <input
          className={inputCls}
          placeholder="例如：product_pm, architecture"
          value={(config.role_ids || []).join(", ")}
          onChange={(e) =>
            patch({
              role_ids: e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            })
          }
        />
      </Field>
      <ModelSelector value={config.model} onChange={(m: NodeModel) => patch({ model: m })} />
      <Field label="多输入拼接方式">
        <select
          className={inputCls}
          value={config.input_join || "labeled"}
          onChange={(e) => patch({ input_join: e.target.value })}
        >
          <option value="labeled">带来源标题</option>
          <option value="concat">纯拼接</option>
        </select>
      </Field>
      <div className="space-y-2 border-t pt-3">
        <span className="text-xs font-medium text-muted-foreground">产出落地</span>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={!!target.as_code_document}
            onChange={(e) =>
              setTarget({ as_code_document: e.target.checked ? { document_type: "derived" } : null })
            }
          />
          落为开发文档
        </label>
        {target.as_code_document && (
          <input
            className={inputCls}
            placeholder="document_type（如 derived）"
            value={target.as_code_document.document_type}
            onChange={(e) => setTarget({ as_code_document: { document_type: e.target.value } })}
          />
        )}
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={!!target.as_stage_version}
            onChange={(e) =>
              setTarget({ as_stage_version: e.target.checked ? { stage: "requirements" } : null })
            }
          />
          落为阶段版本
        </label>
        {target.as_stage_version && (
          <select
            className={inputCls}
            value={target.as_stage_version.stage}
            onChange={(e) => setTarget({ as_stage_version: { stage: e.target.value } })}
          >
            {STAGE_OPTIONS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        )}
      </div>
    </>
  )
}

function MergeEditor({
  config,
  patch,
}: {
  config: MergeConfig
  patch: (p: Record<string, unknown>) => void
}) {
  return (
    <>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={config.labeled ?? true}
          onChange={(e) => patch({ labeled: e.target.checked })}
        />
        每段加来源标题
      </label>
      <Field label="分隔符">
        <input
          className={inputCls}
          value={config.separator ?? "\n\n---\n\n"}
          onChange={(e) => patch({ separator: e.target.value })}
        />
      </Field>
      {config.labeled !== false && (
        <Field label="标题模板（{label} 为来源名）">
          <input
            className={inputCls}
            value={config.title_template ?? "## {label}"}
            onChange={(e) => patch({ title_template: e.target.value })}
          />
        </Field>
      )}
    </>
  )
}

function BranchEditor({
  config,
  patch,
}: {
  config: BranchConfig
  patch: (p: Record<string, unknown>) => void
}) {
  const branches = config.branches || []
  const setBranch = (index: number, p: Partial<BranchOption>) =>
    patch({ branches: branches.map((b, i) => (i === index ? { ...b, ...p } : b)) })
  const addBranch = () =>
    patch({ branches: [...branches, { key: `b${branches.length + 1}`, label: "新分支" }] })
  const removeBranch = (index: number) =>
    patch({ branches: branches.filter((_, i) => i !== index) })

  return (
    <>
      <Field label="判定方式">
        <select
          className={inputCls}
          value={config.mode || "llm_classify"}
          onChange={(e) => patch({ mode: e.target.value })}
        >
          <option value="llm_classify">AI 判定</option>
          <option value="keyword">关键词匹配</option>
        </select>
      </Field>
      {config.mode !== "keyword" && (
        <>
          <Field label="判定指令">
            <textarea
              className={`${inputCls} min-h-20`}
              value={config.prompt || ""}
              onChange={(e) => patch({ prompt: e.target.value })}
            />
          </Field>
          <ModelSelector value={config.model} onChange={(m: NodeModel) => patch({ model: m })} />
        </>
      )}
      <div className="space-y-2 border-t pt-3">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-muted-foreground">分支</span>
          <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={addBranch}>
            + 添加
          </Button>
        </div>
        {branches.map((branch, index) => (
          <div key={index} className="space-y-1 rounded-md border p-2">
            <div className="flex gap-1.5">
              <input
                className={`${inputCls} flex-1`}
                placeholder="key"
                value={branch.key}
                onChange={(e) => setBranch(index, { key: e.target.value })}
              />
              <input
                className={`${inputCls} flex-1`}
                placeholder="名称"
                value={branch.label}
                onChange={(e) => setBranch(index, { label: e.target.value })}
              />
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8 shrink-0"
                onClick={() => removeBranch(index)}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </div>
            {config.mode === "keyword" && (
              <input
                className={inputCls}
                placeholder="关键词，逗号分隔"
                value={(branch.keywords || []).join(", ")}
                onChange={(e) =>
                  setBranch(index, {
                    keywords: e.target.value
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean),
                  })
                }
              />
            )}
          </div>
        ))}
      </div>
      <Field label="默认分支">
        <select
          className={inputCls}
          value={config.default_branch || ""}
          onChange={(e) => patch({ default_branch: e.target.value })}
        >
          {branches.map((b) => (
            <option key={b.key} value={b.key}>
              {b.label} ({b.key})
            </option>
          ))}
        </select>
      </Field>
    </>
  )
}
