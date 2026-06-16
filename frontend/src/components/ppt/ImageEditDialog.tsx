/**
 * Image Edit Dialog Component
 * Allows users to edit page images with natural language instructions
 */
import { useState, useCallback, useRef } from "react"
import { useTranslation } from "react-i18next"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Label } from "@/components/ui/label"
import { Loader2, Wand2, Upload, X, ImageIcon } from "lucide-react"
import { cn } from "@/lib/utils"
import type { PPTPage } from "@/stores/pptStore"
import type { ImageVersion } from "@/api/ppt"

interface ImageEditDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  page: PPTPage | null
  projectId: string
  isEditing: boolean
  onEdit: (
    instruction: string,
    options?: {
      selectionMask?: string
      materialImages?: string[]
    }
  ) => Promise<{ page: PPTPage; new_version: ImageVersion }>
}

export function ImageEditDialog({
  open,
  onOpenChange,
  page,
  isEditing,
  onEdit,
}: ImageEditDialogProps) {
  const { t } = useTranslation("ppt")
  const [instruction, setInstruction] = useState("")
  const [materialImages, setMaterialImages] = useState<string[]>([])
  const [materialPreviews, setMaterialPreviews] = useState<string[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)

  const handleClose = useCallback(() => {
    if (!isEditing) {
      setInstruction("")
      setMaterialImages([])
      setMaterialPreviews([])
      onOpenChange(false)
    }
  }, [isEditing, onOpenChange])

  const handleFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = e.target.files
      if (!files || files.length === 0) return

      Array.from(files).forEach((file) => {
        const reader = new FileReader()
        reader.onload = (event) => {
          const result = event.target?.result as string
          // Store full data URL for preview
          setMaterialPreviews((prev) => [...prev, result])
          // Store base64 without prefix for API
          const base64 = result.split(",")[1]
          setMaterialImages((prev) => [...prev, base64])
        }
        reader.readAsDataURL(file)
      })

      // Reset input
      if (fileInputRef.current) {
        fileInputRef.current.value = ""
      }
    },
    []
  )

  const handleRemoveMaterial = useCallback((index: number) => {
    setMaterialImages((prev) => prev.filter((_, i) => i !== index))
    setMaterialPreviews((prev) => prev.filter((_, i) => i !== index))
  }, [])

  const handleSubmit = useCallback(async () => {
    if (!instruction.trim() || !page) return

    try {
      await onEdit(instruction, {
        materialImages: materialImages.length > 0 ? materialImages : undefined,
      })
      handleClose()
    } catch (error) {
      console.error("Image edit failed:", error)
    }
  }, [instruction, materialImages, page, onEdit, handleClose])

  const title = page?.outline_content?.title || t("imageEdit.untitled", "Untitled Slide")

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Wand2 className="w-5 h-5" />
            {t("imageEdit.title", "Edit Image")}
          </DialogTitle>
          <DialogDescription>
            {t(
              "imageEdit.description",
              "Describe the changes you want to make to this slide image."
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* Current image preview */}
          <div>
            <Label className="text-sm font-medium mb-2 block">
              {t("imageEdit.currentImage", "Current Image")} - {title}
            </Label>
            <div className="relative aspect-video bg-gray-100 dark:bg-gray-800 rounded-lg overflow-hidden">
              {page?.generated_image_url ? (
                <img
                  src={page.generated_image_url}
                  alt={title}
                  className="w-full h-full object-contain"
                />
              ) : (
                <div className="w-full h-full flex items-center justify-center text-gray-400">
                  <ImageIcon className="w-16 h-16" />
                </div>
              )}
            </div>
          </div>

          {/* Edit instruction */}
          <div>
            <Label htmlFor="instruction" className="text-sm font-medium mb-2 block">
              {t("imageEdit.instruction", "Edit Instruction")}
            </Label>
            <Textarea
              id="instruction"
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              placeholder={t(
                "imageEdit.instructionPlaceholder",
                "e.g., Change the background color to blue, Add a company logo in the corner, Make the text bigger..."
              )}
              rows={3}
              className="resize-none"
            />
          </div>

          {/* Material images */}
          <div>
            <Label className="text-sm font-medium mb-2 block">
              {t("imageEdit.materials", "Reference Images (Optional)")}
            </Label>
            <p className="text-xs text-muted-foreground mb-2">
              {t(
                "imageEdit.materialsHint",
                "Add images that should be incorporated into the edit (logos, icons, etc.)"
              )}
            </p>

            <div className="flex flex-wrap gap-2">
              {/* Material previews */}
              {materialPreviews.map((preview, index) => (
                <div
                  key={index}
                  className="relative w-20 h-20 rounded-lg overflow-hidden border group"
                >
                  <img
                    src={preview}
                    alt={`Material ${index + 1}`}
                    className="w-full h-full object-cover"
                  />
                  <button
                    type="button"
                    onClick={() => handleRemoveMaterial(index)}
                    className="absolute top-1 right-1 p-1 bg-black/50 rounded-full opacity-0 group-hover:opacity-100 transition-opacity"
                  >
                    <X className="w-3 h-3 text-white" />
                  </button>
                </div>
              ))}

              {/* Add button */}
              {materialPreviews.length < 3 && (
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  className={cn(
                    "w-20 h-20 rounded-lg border-2 border-dashed",
                    "flex flex-col items-center justify-center gap-1",
                    "text-muted-foreground hover:text-foreground hover:border-primary",
                    "transition-colors"
                  )}
                >
                  <Upload className="w-5 h-5" />
                  <span className="text-xs">{t("imageEdit.add", "Add")}</span>
                </button>
              )}
            </div>

            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              multiple
              className="hidden"
              onChange={handleFileSelect}
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={handleClose} disabled={isEditing}>
            {t("common.cancel", "Cancel")}
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={!instruction.trim() || isEditing || !page?.generated_image_url}
          >
            {isEditing ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                {t("imageEdit.editing", "Editing...")}
              </>
            ) : (
              <>
                <Wand2 className="mr-2 h-4 w-4" />
                {t("imageEdit.apply", "Apply Edit")}
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default ImageEditDialog
