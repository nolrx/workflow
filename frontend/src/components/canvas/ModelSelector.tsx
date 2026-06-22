/**
 * Per-node text model picker. Only text-capable providers (claude / gemini) are
 * offered — openai/panlaxy are image-only. The API key is never chosen here; the
 * server resolves it from the environment for the selected provider.
 */
import type { NodeModel } from "@/api/canvas"

const PROVIDERS: { value: NodeModel["provider"]; label: string; placeholder: string }[] = [
  { value: "claude", label: "Claude", placeholder: "claude-opus-4-8" },
  { value: "gemini", label: "Gemini", placeholder: "gemini-3-flash-preview" },
]

interface ModelSelectorProps {
  value?: NodeModel | null
  onChange: (model: NodeModel) => void
}

export function ModelSelector({ value, onChange }: ModelSelectorProps) {
  const provider = value?.provider || "claude"
  const active = PROVIDERS.find((p) => p.value === provider) || PROVIDERS[0]
  return (
    <div className="space-y-1.5">
      <label className="text-xs font-medium text-muted-foreground">模型</label>
      <div className="flex gap-2">
        <select
          className="h-8 rounded-md border bg-background px-2 text-sm"
          value={provider}
          onChange={(e) =>
            onChange({ ...value, provider: e.target.value as NodeModel["provider"] })
          }
        >
          {PROVIDERS.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
        <input
          className="h-8 flex-1 rounded-md border bg-background px-2 text-sm"
          placeholder={active.placeholder}
          value={value?.model_name || ""}
          onChange={(e) => onChange({ ...value, provider, model_name: e.target.value })}
        />
      </div>
      <p className="text-[11px] text-muted-foreground/70">留空则使用该 provider 的默认模型。</p>
    </div>
  )
}
