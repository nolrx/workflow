import { useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import { figmaApi, type FigmaExportResult, type FigmaExportSource } from "@/api/figma"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"

/**
 * Export a Code artifact to Figma via the companion plugin.
 *
 * Generates a one-time pairing code the user enters in the Figma plugin; the
 * plugin pulls the design package and rebuilds it as layers. Shows a live
 * countdown and lets the user regenerate once the code expires.
 */
export function FigmaExportDialog({
  projectId,
  source,
  previewId,
  runId,
  triggerLabel,
}: {
  projectId: string
  source: FigmaExportSource
  previewId?: string
  runId?: string
  triggerLabel?: string
}) {
  const { t } = useTranslation("code")
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<FigmaExportResult | null>(null)
  const [remaining, setRemaining] = useState(0)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const generate = async () => {
    setLoading(true)
    try {
      const res = await figmaApi.exportToFigma(projectId, { source, previewId, runId })
      setResult(res)
      setRemaining(res.ttl_seconds)
    } catch (error) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("figma.exportFailed")
      toast.error(message)
    } finally {
      setLoading(false)
    }
  }

  // Generate a fresh code each time the dialog opens; clear it on close. Driven
  // from the open-change event (not an effect) to avoid cascading renders.
  const handleOpenChange = (next: boolean) => {
    setOpen(next)
    if (next) {
      void generate()
    } else {
      setResult(null)
      setRemaining(0)
    }
  }

  // Countdown tick.
  useEffect(() => {
    if (timerRef.current) clearInterval(timerRef.current)
    if (remaining <= 0) return
    timerRef.current = setInterval(() => {
      setRemaining((value) => (value <= 1 ? 0 : value - 1))
    }, 1000)
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [remaining])

  const expired = !!result && remaining <= 0
  const mmss = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`

  const copyCode = async () => {
    if (!result) return
    try {
      await navigator.clipboard.writeText(result.pairing_code)
      toast.success(t("figma.copied"))
    } catch {
      /* clipboard may be unavailable; the code is shown for manual copy */
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          {triggerLabel || t("figma.export")}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("figma.exportTitle")}</DialogTitle>
          <DialogDescription>{t("figma.exportDesc")}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col items-center gap-3 py-2">
          {loading ? (
            <p className="text-sm text-muted-foreground">{t("figma.exporting")}</p>
          ) : result ? (
            <>
              <div className="font-mono text-3xl font-bold tracking-[0.3em]">
                {result.pairing_code}
              </div>
              <p className={`text-xs ${expired ? "text-destructive" : "text-muted-foreground"}`}>
                {expired ? t("figma.expired") : t("figma.expiresIn", { time: mmss })}
              </p>
              <div className="flex gap-2">
                <Button size="sm" variant="outline" onClick={copyCode} disabled={expired}>
                  {t("figma.copyCode")}
                </Button>
                {expired && (
                  <Button size="sm" onClick={generate}>
                    {t("figma.regenerate")}
                  </Button>
                )}
              </div>
            </>
          ) : null}
        </div>

        <p className="rounded-md bg-muted/40 p-3 text-xs leading-relaxed text-muted-foreground">
          {t("figma.pluginHint")}
        </p>
      </DialogContent>
    </Dialog>
  )
}

export default FigmaExportDialog
