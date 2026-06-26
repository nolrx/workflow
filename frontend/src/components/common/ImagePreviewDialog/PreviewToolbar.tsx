import { useTranslation } from "react-i18next"
import * as DialogPrimitive from "@radix-ui/react-dialog"
import { Download, ExternalLink, Info, Minus, Plus, X, ZoomIn } from "lucide-react"

import { Button } from "@/components/ui/button"

interface PreviewToolbarProps {
  currentIndex: number
  total: number
  scale: number
  canZoomIn: boolean
  canZoomOut: boolean
  onZoomIn: () => void
  onZoomOut: () => void
  onOpenInNewTab: () => void
  onDownload: () => void
  onShowCaption?: () => void
}

export function PreviewToolbar({
  currentIndex,
  total,
  canZoomIn,
  canZoomOut,
  onZoomIn,
  onZoomOut,
  onOpenInNewTab,
  onDownload,
  onShowCaption,
}: PreviewToolbarProps) {
  const { t } = useTranslation("common")

  return (
    <div className="flex shrink-0 items-center justify-between gap-2 bg-gradient-to-b from-black/60 to-transparent px-4 py-3 sm:px-6 sm:py-4">
      <div className="flex items-center gap-2 text-sm text-white/90">
        <ZoomIn className="h-4 w-4" />
        <span>
          {total > 1
            ? t("preview.counter", { current: currentIndex + 1, total })
            : t("preview.title")}
        </span>
      </div>

      <div className="flex items-center gap-1">
        <ToolbarButton
          icon={<Minus className="h-5 w-5" />}
          onClick={onZoomOut}
          disabled={!canZoomOut}
          title={t("preview.zoomOut")}
        />
        <ToolbarButton
          icon={<Plus className="h-5 w-5" />}
          onClick={onZoomIn}
          disabled={!canZoomIn}
          title={t("preview.zoomIn")}
        />
        <ToolbarButton
          icon={<ExternalLink className="h-5 w-5" />}
          onClick={onOpenInNewTab}
          title={t("preview.openInNewTab")}
        />
        <ToolbarButton
          icon={<Download className="h-5 w-5" />}
          onClick={onDownload}
          title={t("preview.download")}
        />
        {onShowCaption && (
          <ToolbarButton
            icon={<Info className="h-5 w-5" />}
            onClick={onShowCaption}
            title={t("preview.showCaption")}
          />
        )}
        <DialogPrimitive.Close asChild>
          <ToolbarButton
            icon={<X className="h-5 w-5" />}
            onClick={() => {}}
            title={t("preview.close")}
          />
        </DialogPrimitive.Close>
      </div>
    </div>
  )
}

interface ToolbarButtonProps {
  icon: React.ReactNode
  onClick: () => void
  title: string
  disabled?: boolean
}

function ToolbarButton({ icon, onClick, title, disabled }: ToolbarButtonProps) {
  return (
    <Button
      variant="ghost"
      size="icon"
      disabled={disabled}
      className="h-9 w-9 text-white/90 hover:bg-white/10 hover:text-white disabled:opacity-40"
      onClick={onClick}
      title={title}
    >
      {icon}
    </Button>
  )
}
