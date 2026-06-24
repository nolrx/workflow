import { useState } from "react"
import { useTranslation } from "react-i18next"
import { History, Loader2, RotateCcw } from "lucide-react"
import { toast } from "sonner"

import { codeApi, type CodeProject, type StageVersion } from "@/api/code"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { ImagePreviewDialog } from "@/components/common/ImagePreviewDialog"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"

export type HistoryStage = "requirements" | "flow" | "documents" | "style" | "preview"

interface StageHistoryDialogProps {
  projectId: string
  stage: HistoryStage
  /** Called with the refreshed project after a successful rollback. */
  onRestored: (project: CodeProject) => void
  /** Override the trigger button label (defaults to a generic "history"). */
  triggerLabel?: string
}

function formatTime(iso: string | null): string {
  if (!iso) return ""
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString()
}

/**
 * Per-stage version history. The trigger sits in a stage tab's toolbar; opening
 * it lists every recorded version (newest first), previews the selected one
 * read-only, and can restore (roll back) any non-current version — the backend
 * rewrites the live project from that version and the parent reloads it.
 */
export function StageHistoryDialog({
  projectId,
  stage,
  onRestored,
  triggerLabel,
}: StageHistoryDialogProps) {
  const { t } = useTranslation("code")
  const [open, setOpen] = useState(false)
  const [versions, setVersions] = useState<StageVersion[]>([])
  const [loading, setLoading] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<StageVersion | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [restoringId, setRestoringId] = useState<string | null>(null)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewIndex, setPreviewIndex] = useState(0)
  const [previewImages, setPreviewImages] = useState<Array<{ src: string; alt?: string }>>([])

  const selectVersion = async (version: StageVersion) => {
    setSelectedId(version.id)
    setLoadingDetail(true)
    setDetail(null)
    try {
      setDetail(await codeApi.getStageVersion(projectId, stage, version.id))
    } catch {
      toast.error(t("versions.loadFailed"))
    } finally {
      setLoadingDetail(false)
    }
  }

  const handleOpenChange = (next: boolean) => {
    setOpen(next)
    if (!next) return
    setSelectedId(null)
    setDetail(null)
    setLoading(true)
    void (async () => {
      try {
        const list = await codeApi.listStageVersions(projectId, stage)
        setVersions(list)
        const current = list.find((item) => item.is_current) ?? list[0]
        if (current) void selectVersion(current)
      } catch {
        toast.error(t("versions.loadFailed"))
      } finally {
        setLoading(false)
      }
    })()
  }

  const handleRestore = async (version: StageVersion) => {
    setRestoringId(version.id)
    try {
      const project = await codeApi.activateStageVersion(projectId, stage, version.id)
      onRestored(project)
      toast.success(t("versions.restored"))
      setOpen(false)
    } catch {
      toast.error(t("versions.restoreFailed"))
    } finally {
      setRestoringId(null)
    }
  }

  const renderContent = (version: StageVersion) => {
    const empty = <div className="text-xs text-muted-foreground">{t("versions.noContent")}</div>
    if (stage === "documents") {
      const docs =
        (version.content_json as {
          documents?: Array<{ title: string; document_type: string; content: string }>
        })?.documents ?? []
      if (!docs.length) return empty
      return (
        <div className="space-y-3">
          {docs.map((doc, index) => (
            <div key={index}>
              <div className="text-xs font-semibold">
                {doc.title}
                <span className="ml-1.5 font-normal text-muted-foreground">
                  {doc.document_type}
                </span>
              </div>
              <pre className="mt-1 whitespace-pre-wrap break-words text-xs">{doc.content}</pre>
            </div>
          ))}
        </div>
      )
    }
    if (stage === "preview") {
      const images =
        (version.content_json as { preview_images?: Array<{ id?: string; url: string }> })
          ?.preview_images ?? []
      if (!images.length) return empty
      const openPreview = (index: number) => {
        setPreviewImages(images.map((img) => ({ src: img.url, alt: "" })))
        setPreviewIndex(index)
        setPreviewOpen(true)
      }
      return (
        <div className="grid grid-cols-2 gap-2">
          {images.map((image, index) => (
            <button
              key={image.id ?? index}
              type="button"
              onClick={() => openPreview(index)}
              className="group relative overflow-hidden rounded border focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <img
                src={image.url}
                alt=""
                className="w-full transition-transform duration-300 group-hover:scale-105"
              />
              <span className="absolute inset-0 flex items-center justify-center bg-black/0 opacity-0 transition-all duration-200 group-hover:bg-black/20 group-hover:opacity-100">
                <span className="text-xs font-medium text-white drop-shadow">
                  {t("versions.zoom")}
                </span>
              </span>
            </button>
          ))}
        </div>
      )
    }
    const text = version.content_text ?? ""
    if (!text.trim()) return empty
    return <pre className="whitespace-pre-wrap break-words text-xs">{text}</pre>
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 gap-1.5 text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <History className="h-4 w-4" />
          {triggerLabel ?? t("versions.button")}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>
            {t("versions.title")} · {t(`versions.stage.${stage}`)}
          </DialogTitle>
        </DialogHeader>
        {loading ? (
          <div className="flex h-64 items-center justify-center text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />
          </div>
        ) : versions.length === 0 ? (
          <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
            {t("versions.empty")}
          </div>
        ) : (
          <div className="grid gap-4 md:grid-cols-[240px_minmax(0,1fr)]">
            <ScrollArea className="h-[420px] pr-2">
              <div className="space-y-2">
                {versions.map((version) => (
                  <button
                    key={version.id}
                    type="button"
                    onClick={() => void selectVersion(version)}
                    className={cn(
                      "w-full rounded-md border p-2.5 text-left transition-colors",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      selectedId === version.id ? "border-primary bg-primary/10" : "hover:bg-muted"
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium">
                        {t("versions.versionLabel", { n: version.version_number })}
                      </span>
                      {version.is_current && (
                        <Badge variant="secondary" className="shrink-0">
                          {t("versions.current")}
                        </Badge>
                      )}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      <Badge variant="outline" className="text-[10px]">
                        {t(`versions.source.${version.source}`)}
                      </Badge>
                      <span className="text-xs text-muted-foreground">
                        {formatTime(version.created_at)}
                      </span>
                    </div>
                    {version.summary && (
                      <p className="mt-1 truncate text-xs text-muted-foreground">
                        {version.summary}
                      </p>
                    )}
                  </button>
                ))}
              </div>
            </ScrollArea>
            <div className="flex min-h-[420px] flex-col">
              {loadingDetail ? (
                <div className="flex flex-1 items-center justify-center text-muted-foreground">
                  <Loader2 className="h-5 w-5 animate-spin" />
                </div>
              ) : detail ? (
                <>
                  <ScrollArea className="h-[372px] rounded-md border bg-muted/30 p-3">
                    {renderContent(detail)}
                  </ScrollArea>
                  <div className="mt-3 flex items-center justify-between gap-2">
                    {detail.note ? (
                      <span
                        className="truncate text-xs text-muted-foreground"
                        title={detail.note}
                      >
                        {detail.note}
                      </span>
                    ) : (
                      <span />
                    )}
                    <Button
                      size="sm"
                      disabled={detail.is_current || restoringId === detail.id}
                      onClick={() => void handleRestore(detail)}
                    >
                      {restoringId === detail.id ? (
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      ) : (
                        <RotateCcw className="mr-2 h-4 w-4" />
                      )}
                      {detail.is_current ? t("versions.current") : t("versions.restore")}
                    </Button>
                  </div>
                </>
              ) : (
                <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
                  {t("versions.selectHint")}
                </div>
              )}
            </div>
          </div>
        )}
      </DialogContent>

      <ImagePreviewDialog
        open={previewOpen}
        onOpenChange={setPreviewOpen}
        images={previewImages}
        index={previewIndex}
        onIndexChange={setPreviewIndex}
      />
    </Dialog>
  )
}

export default StageHistoryDialog
