import { useEffect, useRef } from "react"
import { useTranslation } from "react-i18next"
import { Trash2 } from "lucide-react"
import { cn } from "@/lib/utils"

interface SessionContextMenuProps {
  open: boolean
  x: number
  y: number
  isDeployed?: boolean
  onClose: () => void
  onDelete: () => void
}

export function SessionContextMenu({
  open,
  x,
  y,
  isDeployed,
  onClose,
  onDelete,
}: SessionContextMenuProps) {
  const { t } = useTranslation("common")
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return

    const handlePointerDown = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) {
        onClose()
      }
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose()
      }
    }

    document.addEventListener("pointerdown", handlePointerDown)
    document.addEventListener("keydown", handleKeyDown)
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown)
      document.removeEventListener("keydown", handleKeyDown)
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      ref={ref}
      style={{ left: x, top: y }}
      className="fixed z-50 min-w-[10rem] rounded-md border bg-popover p-1 text-popover-foreground shadow-md"
    >
      <button
        type="button"
        disabled={isDeployed}
        title={isDeployed ? t("sidebar.cannotDeleteDeployed") : undefined}
        onClick={() => {
          if (isDeployed) return
          onDelete()
          onClose()
        }}
        className={cn(
          "flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm",
          isDeployed
            ? "cursor-not-allowed text-muted-foreground"
            : "hover:bg-accent hover:text-accent-foreground"
        )}
      >
        <Trash2 className="h-4 w-4" />
        {isDeployed ? t("sidebar.cannotDeleteDeployed") : t("sidebar.deleteSession")}
      </button>
    </div>
  )
}
