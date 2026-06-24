import { useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { Check, Palette, Search, X } from "lucide-react"
import * as DialogPrimitive from "@radix-ui/react-dialog"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { cn } from "@/lib/utils"
import { useCodeStore } from "@/stores/codeStore"

interface StyleSelectModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Modal for picking UI styles. The catalog can grow large, so the full grid lives
 * here instead of inline in the style card — the main card only shows a compact
 * summary and the generate action stays visible above the fold.
 */
export function StyleSelectModal({ open, onOpenChange }: StyleSelectModalProps) {
  const { t } = useTranslation("code")
  const { t: tc } = useTranslation("common")

  const styles = useCodeStore((s) => s.styles)
  const selectedStyleIds = useCodeStore((s) => s.selectedStyleIds)
  const setSelectedStyleIds = useCodeStore((s) => s.setSelectedStyleIds)

  const [draftIds, setDraftIds] = useState<string[]>(selectedStyleIds)
  const [query, setQuery] = useState("")

  const filteredStyles = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return styles
    return styles.filter(
      (style) =>
        style.name.toLowerCase().includes(q) ||
        style.description.toLowerCase().includes(q)
    )
  }, [styles, query])

  const handleToggle = (styleId: string) => {
    setDraftIds((prev) =>
      prev.includes(styleId) ? prev.filter((id) => id !== styleId) : [...prev, styleId]
    )
  }

  const handleConfirm = () => {
    setSelectedStyleIds(draftIds)
    onOpenChange(false)
  }

  const handleOpenChange = (next: boolean) => {
    if (next) {
      setDraftIds(selectedStyleIds)
      setQuery("")
    }
    onOpenChange(next)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className={cn(
            "fixed inset-0 z-50 bg-black/80",
            "animated-dialog-overlay"
          )}
        />
        <DialogPrimitive.Content
          className={cn(
            "fixed left-[50%] top-[50%] z-50 grid w-full max-w-2xl translate-x-[-50%] translate-y-[-50%] gap-0 border bg-background p-0 shadow-lg sm:rounded-lg",
            "animated-dialog-content",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          )}
        >
          <DialogHeader className="px-6 pt-6 pb-4">
            <DialogTitle className="flex items-center gap-2">
              <Palette className="h-5 w-5 text-primary" />
              {t("style.modalTitle")}
            </DialogTitle>
            <DialogDescription>{t("style.modalDescription")}</DialogDescription>
          </DialogHeader>

          <div className="px-6 pb-4">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                placeholder={t("style.searchPlaceholder")}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                className="pl-9"
              />
            </div>
            <p className="mt-2 text-xs text-muted-foreground">
              {t("style.selectedCount", { count: draftIds.length })}
            </p>
          </div>

          <div className="border-t px-6 py-4">
            {filteredStyles.length === 0 ? (
              <div className="py-8 text-center text-sm text-muted-foreground">
                {t("style.emptySearch")}
              </div>
            ) : (
              <div className="grid max-h-[40vh] gap-3 overflow-y-auto overscroll-contain pr-1 sm:grid-cols-2">
                {filteredStyles.map((style) => {
                  const checked = draftIds.includes(style.id)
                  return (
                    <Label
                      key={style.id}
                      className="flex cursor-pointer gap-3 rounded-md border bg-card p-3 transition-colors hover:bg-muted/50"
                    >
                      <Checkbox
                        checked={checked}
                        onCheckedChange={() => handleToggle(style.id)}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2">
                          <span className="block text-sm font-medium">{style.name}</span>
                          {checked && (
                            <Check className="h-3.5 w-3.5 text-primary" />
                          )}
                        </span>
                        <span className="block text-xs text-muted-foreground">
                          {style.description}
                        </span>
                      </span>
                    </Label>
                  )
                })}
              </div>
            )}
          </div>

          <DialogFooter className="border-t px-6 py-4">
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              {tc("buttons.cancel")}
            </Button>
            <Button onClick={handleConfirm} disabled={draftIds.length === 0}>
              {tc("buttons.confirm")}
            </Button>
          </DialogFooter>

          <DialogPrimitive.Close className="absolute right-4 top-4 rounded-sm opacity-70 ring-offset-background transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:pointer-events-none data-[state=open]:bg-accent data-[state=open]:text-muted-foreground">
            <X className="h-4 w-4" />
            <span className="sr-only">{tc("buttons.close")}</span>
          </DialogPrimitive.Close>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </Dialog>
  )
}

export default StyleSelectModal
