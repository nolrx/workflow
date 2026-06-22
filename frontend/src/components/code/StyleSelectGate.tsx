import { useTranslation } from "react-i18next"
import { Loader2, Palette } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { useAgentStore } from "@/stores/agentStore"
import { useCodeStore } from "@/stores/codeStore"

/**
 * Composer-area gate shown when the run pauses at `style_select` — the new step
 * between the documents review and style-document generation. The user picks one
 * or more UI styles from the catalog; confirming resumes the run via
 * `selectStyle` (action `select_style`), which generates the style document from
 * the chosen styles. At least one style must be selected.
 */
export function StyleSelectGate() {
  const { t } = useTranslation("code")
  const styles = useCodeStore((s) => s.styles)
  const selectedStyleIds = useCodeStore((s) => s.selectedStyleIds)
  const toggleStyle = useCodeStore((s) => s.toggleStyle)
  const selectStyle = useAgentStore((s) => s.selectStyle)
  const isStreaming = useAgentStore((s) => s.isStreaming)

  const canConfirm = selectedStyleIds.length > 0 && !isStreaming

  return (
    <div className="space-y-3">
      <p className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
        <Palette className="h-4 w-4 text-primary" />
        {t("styleSelect.title")}
      </p>
      <div className="grid gap-2 sm:grid-cols-2">
        {styles.map((style) => (
          <Label key={style.id} className="flex cursor-pointer gap-3 rounded-md border p-3">
            <Checkbox
              checked={selectedStyleIds.includes(style.id)}
              onCheckedChange={() => toggleStyle(style.id)}
            />
            <span className="min-w-0">
              <span className="block text-sm font-medium">{style.name}</span>
              <span className="block text-xs text-muted-foreground">{style.description}</span>
            </span>
          </Label>
        ))}
      </div>
      {selectedStyleIds.length === 0 && (
        <p className="text-xs text-amber-600">{t("styleSelect.required")}</p>
      )}
      <Button
        className="w-full"
        onClick={() => void selectStyle(selectedStyleIds)}
        disabled={!canConfirm}
      >
        {isStreaming && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
        {t("styleSelect.confirm")}
      </Button>
    </div>
  )
}

export default StyleSelectGate
