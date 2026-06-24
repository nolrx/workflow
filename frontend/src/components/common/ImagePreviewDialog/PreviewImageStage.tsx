import { useTranslation } from "react-i18next"
import { ChevronLeft, ChevronRight } from "lucide-react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { PreviewImage } from "./types"
import { MIN_SCALE } from "./useImagePreview"

interface PreviewImageStageProps {
  image: PreviewImage
  scale: number
  position: { x: number; y: number }
  isDragging: boolean
  hasPrev: boolean
  hasNext: boolean
  onToggleZoom: () => void
  onMouseDown: (e: React.MouseEvent<HTMLImageElement>) => void
  onWheel: (e: React.WheelEvent<HTMLImageElement>) => void
  onPrev: () => void
  onNext: () => void
}

export function PreviewImageStage({
  image,
  scale,
  position,
  isDragging,
  hasPrev,
  hasNext,
  onToggleZoom,
  onMouseDown,
  onWheel,
  onPrev,
  onNext,
}: PreviewImageStageProps) {
  const { t } = useTranslation("common")
  const showNav = scale <= MIN_SCALE

  return (
    <div className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden px-4 pb-12 sm:px-10">
      {showNav && hasPrev && (
        <NavArrow direction="left" onClick={onPrev} title={t("preview.previous")} />
      )}

      <img
        src={image.src}
        alt={image.alt || t("preview.imageAlt")}
        className={cn(
          "max-h-full max-w-full rounded object-contain shadow-2xl",
          scale > MIN_SCALE ? "cursor-grab active:cursor-grabbing" : "cursor-zoom-in"
        )}
        style={{
          transform: `translate(${position.x}px, ${position.y}px) scale(${scale})`,
          transition: isDragging ? "none" : "transform 200ms ease-out",
        }}
        onClick={(e) => {
          e.stopPropagation()
          onToggleZoom()
        }}
        onMouseDown={onMouseDown}
        onWheel={onWheel}
        draggable={false}
      />

      {showNav && hasNext && (
        <NavArrow direction="right" onClick={onNext} title={t("preview.next")} />
      )}
    </div>
  )
}

interface NavArrowProps {
  direction: "left" | "right"
  onClick: () => void
  title: string
}

function NavArrow({ direction, onClick, title }: NavArrowProps) {
  const Icon = direction === "left" ? ChevronLeft : ChevronRight
  const sideClass = direction === "left" ? "left-2 sm:left-4" : "right-2 sm:right-4"

  return (
    <Button
      variant="ghost"
      size="icon"
      className={cn(
        "absolute top-1/2 z-10 h-10 w-10 -translate-y-1/2 rounded-full",
        "bg-black/40 text-white/90 hover:bg-black/60 hover:text-white",
        "sm:h-12 sm:w-12",
        sideClass
      )}
      onClick={(e) => {
        e.stopPropagation()
        onClick()
      }}
      title={title}
    >
      <Icon className="h-6 w-6" />
    </Button>
  )
}
