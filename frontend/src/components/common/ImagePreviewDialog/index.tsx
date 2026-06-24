import * as DialogPrimitive from "@radix-ui/react-dialog"

import { cn } from "@/lib/utils"
import type { PreviewImage } from "./types"
import { useImagePreview } from "./useImagePreview"
import { PreviewCaption } from "./PreviewCaption"
import { PreviewImageStage } from "./PreviewImageStage"
import { PreviewToolbar } from "./PreviewToolbar"

export type { PreviewImage }

interface ImagePreviewDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  images: PreviewImage[]
  /** Controlled active image index. */
  index: number
  /** Called when the user navigates to another image. */
  onIndexChange?: (index: number) => void
}

/**
 * Full-screen friendly image preview dialog.
 *
 * - Click thumbnail to open.
 * - Click backdrop to close.
 * - Click image to zoom; drag to pan when zoomed in; wheel to zoom.
 * - Arrow keys / on-screen chevrons to browse a gallery.
 * - Download and open-in-new-tab actions in the top toolbar.
 */
export function ImagePreviewDialog({
  open,
  onOpenChange,
  images,
  index,
  onIndexChange,
}: ImagePreviewDialogProps) {
  const safeIndex = Math.max(0, Math.min(index, images.length - 1))
  const current = images[safeIndex]
  const hasPrev = safeIndex > 0
  const hasNext = safeIndex < images.length - 1

  const {
    scale,
    position,
    isDragging,
    canZoomIn,
    canZoomOut,
    zoomIn,
    zoomOut,
    toggleZoom,
    resetZoom,
    goPrev,
    goNext,
    handleMouseDown,
    handleWheel,
  } = useImagePreview({
    imageCount: images.length,
    currentIndex: safeIndex,
    onIndexChange,
    onClose: () => onOpenChange(false),
  })

  const handleOpenChange = (next: boolean) => {
    if (!next) resetZoom()
    onOpenChange(next)
  }

  const handleOpenInNewTab = () => {
    if (!current) return
    window.open(current.src, "_blank", "noopener,noreferrer")
  }

  const handleDownload = () => {
    if (!current) return
    const url = current.downloadUrl ?? current.src
    const link = document.createElement("a")
    link.href = url
    link.download = current.alt || "image"
    link.target = "_blank"
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
  }

  if (!current) return null

  return (
    <DialogPrimitive.Root open={open} onOpenChange={handleOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className={cn(
            "fixed inset-0 z-50 bg-black/90",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0"
          )}
        />

        <DialogPrimitive.Content
          className={cn(
            "fixed inset-0 z-50 flex flex-col outline-none",
            "data-[state=open]:animate-in data-[state=closed]:animate-out",
            "data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0"
          )}
          onEscapeKeyDown={() => handleOpenChange(false)}
          onClick={(e) => {
            // Close only when clicking the empty backdrop, not the image or controls.
            if (e.target === e.currentTarget) handleOpenChange(false)
          }}
        >
          <DialogPrimitive.Title className="sr-only">
            Image preview {safeIndex + 1} of {images.length}
          </DialogPrimitive.Title>

          <PreviewToolbar
            currentIndex={safeIndex}
            total={images.length}
            scale={scale}
            canZoomIn={canZoomIn}
            canZoomOut={canZoomOut}
            onZoomIn={zoomIn}
            onZoomOut={zoomOut}
            onOpenInNewTab={handleOpenInNewTab}
            onDownload={handleDownload}
          />

          <PreviewImageStage
            image={current}
            scale={scale}
            position={position}
            isDragging={isDragging}
            hasPrev={hasPrev}
            hasNext={hasNext}
            onToggleZoom={toggleZoom}
            onMouseDown={handleMouseDown}
            onWheel={handleWheel}
            onPrev={goPrev}
            onNext={goNext}
          />

          <PreviewCaption alt={current.alt} />
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

export default ImagePreviewDialog
