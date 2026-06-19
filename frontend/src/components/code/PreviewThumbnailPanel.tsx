import { useTranslation } from "react-i18next"
import { CheckCircle2, ImageIcon, Loader2 } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { StageHistoryDialog } from "@/components/code/StageHistoryDialog"
import { useCodeStore } from "@/stores/codeStore"

/**
 * Right-hand rail that shows the generated UI preview thumbnails (moved out of
 * the conversation transcript). Reads the Code project straight from the store;
 * each thumbnail can be confirmed as the UI baseline. Rendered inside an
 * animated, slide-in container by CodeStudio — this component owns only the
 * content + confirm interactions, not the show/hide animation.
 */
export function PreviewThumbnailPanel() {
  const { t } = useTranslation("code")
  const project = useCodeStore((s) => s.project)
  const isLoading = useCodeStore((s) => s.isLoading)
  const activeAction = useCodeStore((s) => s.activeAction)
  const confirmPreview = useCodeStore((s) => s.confirmPreview)
  const setCurrentProject = useCodeStore((s) => s.setCurrentProject)

  const images = project?.preview_images ?? []
  const generating = activeAction === "preview"

  return (
    <Card className="flex h-full min-h-0 w-full flex-col overflow-hidden p-0">
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
          <ImageIcon className="h-4 w-4" />
        </span>
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
          {t("preview.title")}
        </span>
        {project && (
          <StageHistoryDialog
            projectId={project.id}
            stage="preview"
            onRestored={setCurrentProject}
            triggerLabel={t("versions.previewHistory")}
          />
        )}
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        {images.length === 0 && generating ? (
          <div className="flex flex-col items-center justify-center gap-2 py-12 text-center text-sm text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin text-primary" />
            {t("preview.generating")}
          </div>
        ) : images.length === 0 ? (
          <div className="flex min-h-32 items-center justify-center rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
            {t("preview.empty")}
          </div>
        ) : (
          images.map((image) => {
            const confirmed = project?.confirmed_preview_url === image.url
            return (
              <div
                key={image.id}
                className="overflow-hidden rounded-lg border bg-card shadow-sm duration-300 animate-in fade-in zoom-in-95"
              >
                <img
                  src={image.url}
                  alt={t("preview.imageAlt")}
                  loading="lazy"
                  className="aspect-square w-full object-cover"
                />
                <div className="p-2">
                  <Button
                    className="w-full"
                    size="sm"
                    variant={confirmed ? "secondary" : "default"}
                    onClick={() => void confirmPreview(image.url)}
                    disabled={isLoading}
                  >
                    {confirmed ? (
                      <>
                        <CheckCircle2 className="mr-1.5 h-4 w-4" />
                        {t("preview.confirmed")}
                      </>
                    ) : (
                      t("preview.confirm")
                    )}
                  </Button>
                </div>
              </div>
            )
          })
        )}
      </div>
    </Card>
  )
}

export default PreviewThumbnailPanel
