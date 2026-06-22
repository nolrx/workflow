import { useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import { RotateCcw, Save, Search } from "lucide-react"
import { AppLayout } from "@/components/layout/AppLayout"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import { adminApi, type PromptDetail, type PromptSummary } from "@/api/admin"

const SCOPE_ORDER = ["code", "prefix", "special", "custom"]

export function PromptAdmin() {
  const { t } = useTranslation("admin")

  const [prompts, setPrompts] = useState<PromptSummary[]>([])
  const [mongoAvailable, setMongoAvailable] = useState(true)
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [detail, setDetail] = useState<PromptDetail | null>(null)
  const [content, setContent] = useState("")
  const [query, setQuery] = useState("")
  const [loadingList, setLoadingList] = useState(true)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [saving, setSaving] = useState(false)

  const refreshList = async () => {
    setLoadingList(true)
    try {
      const { prompts: list, mongo_available } = await adminApi.listPrompts()
      setPrompts(list)
      setMongoAvailable(mongo_available)
    } catch {
      toast.error(t("errors.loadList"))
    } finally {
      setLoadingList(false)
    }
  }

  useEffect(() => {
    void refreshList()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const selectPrompt = async (key: string) => {
    setSelectedKey(key)
    setLoadingDetail(true)
    try {
      const { prompt } = await adminApi.getPrompt(key)
      setDetail(prompt)
      setContent(prompt.content)
    } catch {
      toast.error(t("errors.loadDetail"))
    } finally {
      setLoadingDetail(false)
    }
  }

  const handleSave = async () => {
    if (!detail) return
    setSaving(true)
    try {
      const updated = await adminApi.updatePrompt(detail.key, content)
      setDetail(updated)
      setContent(updated.content)
      toast.success(t("saved"))
      void refreshList()
    } catch (error) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("errors.save")
      toast.error(message)
    } finally {
      setSaving(false)
    }
  }

  const handleReset = async () => {
    if (!detail) return
    setSaving(true)
    try {
      const updated = await adminApi.resetPrompt(detail.key)
      setDetail(updated)
      setContent(updated.content)
      toast.success(t("resetDone"))
      void refreshList()
    } catch (error) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("errors.reset")
      toast.error(message)
    } finally {
      setSaving(false)
    }
  }

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return prompts
    return prompts.filter(
      (p) =>
        p.key.toLowerCase().includes(q) ||
        p.name.toLowerCase().includes(q) ||
        p.description.toLowerCase().includes(q)
    )
  }, [prompts, query])

  const grouped = useMemo(() => {
    const groups: Record<string, PromptSummary[]> = {}
    for (const p of filtered) {
      ;(groups[p.scope] ||= []).push(p)
    }
    const scopes = Object.keys(groups).sort(
      (a, b) => SCOPE_ORDER.indexOf(a) - SCOPE_ORDER.indexOf(b)
    )
    return scopes.map((scope) => ({ scope, items: groups[scope] }))
  }, [filtered])

  const dirty = detail !== null && content !== detail.content
  const scopeLabel = (scope: string) => t(`groups.${scope}`, { defaultValue: scope })

  return (
    <AppLayout title={t("title")}>
      <div className="space-y-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight">{t("title")}</h2>
          <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
        </div>

        {!mongoAvailable && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
            {t("mongoUnavailable")}
          </div>
        )}

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[320px_1fr]">
          {/* Left: prompt list */}
          <div className="flex max-h-[calc(100vh-220px)] flex-col rounded-lg border bg-card">
            <div className="border-b p-3">
              <div className="relative">
                <Search className="pointer-events-none absolute left-2 top-2.5 h-4 w-4 text-muted-foreground" />
                <Input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder={t("searchPlaceholder")}
                  className="pl-8"
                />
              </div>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-2">
              {loadingList ? (
                <p className="px-2 py-3 text-sm text-muted-foreground">{t("loading")}</p>
              ) : grouped.length === 0 ? (
                <p className="px-2 py-3 text-sm text-muted-foreground">{t("empty")}</p>
              ) : (
                grouped.map((group) => (
                  <div key={group.scope} className="mb-3">
                    <div className="px-2 py-1 text-xs font-semibold uppercase text-muted-foreground">
                      {scopeLabel(group.scope)}
                    </div>
                    {group.items.map((p) => (
                      <button
                        key={p.key}
                        onClick={() => selectPrompt(p.key)}
                        title={p.key}
                        className={cn(
                          "flex w-full items-center justify-between gap-2 rounded-md px-2 py-2 text-left text-sm transition-colors",
                          selectedKey === p.key
                            ? "bg-accent font-medium text-accent-foreground"
                            : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                        )}
                      >
                        <span className="truncate">{p.name}</span>
                        {p.is_overridden && (
                          <Badge variant="secondary" className="shrink-0 text-[10px]">
                            {t("badge.overridden")}
                          </Badge>
                        )}
                      </button>
                    ))}
                  </div>
                ))
              )}
            </div>
          </div>

          {/* Right: editor */}
          <div className="flex max-h-[calc(100vh-220px)] flex-col rounded-lg border bg-card">
            {detail === null ? (
              <div className="flex flex-1 items-center justify-center p-8 text-sm text-muted-foreground">
                {t("selectPrompt")}
              </div>
            ) : (
              <>
                <div className="flex items-start justify-between gap-4 border-b p-4">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <h3 className="truncate font-semibold">{detail.name}</h3>
                      {detail.is_overridden ? (
                        <Badge variant="secondary">{t("badge.overridden")}</Badge>
                      ) : (
                        <Badge variant="outline">{t("badge.default")}</Badge>
                      )}
                    </div>
                    <p className="truncate font-mono text-xs text-muted-foreground">{detail.key}</p>
                    {detail.description && (
                      <p className="mt-1 text-xs text-muted-foreground">{detail.description}</p>
                    )}
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleReset}
                      disabled={saving || !mongoAvailable || !detail.has_default || !detail.is_overridden}
                    >
                      <RotateCcw className="mr-1 h-4 w-4" />
                      {t("reset")}
                    </Button>
                    <Button
                      size="sm"
                      onClick={handleSave}
                      disabled={saving || !mongoAvailable || !dirty}
                    >
                      <Save className="mr-1 h-4 w-4" />
                      {t("save")}
                    </Button>
                  </div>
                </div>
                <div className="min-h-0 flex-1 p-4">
                  <Textarea
                    value={content}
                    onChange={(e) => setContent(e.target.value)}
                    disabled={loadingDetail || !mongoAvailable}
                    spellCheck={false}
                    className="h-full min-h-[360px] resize-none font-mono text-xs leading-relaxed"
                  />
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </AppLayout>
  )
}
