import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import { figmaApi, type FigmaAttachedDesign, type FigmaResolved } from "@/api/figma"
import { FigmaCredentialCard } from "@/components/code/FigmaCredentialCard"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

/**
 * Attach a whole Figma file (all frames) to the current Code project.
 *
 * Flow: connect Figma (PAT) -> paste a file URL -> resolve (preview name +
 * thumbnail) -> attach (pull the node tree + render every top-level frame, store
 * them). A subsequent "generate multi-file project" run feeds the design (render
 * images + IR) into the build so the generated React project matches the design.
 *
 * Requires an existing project. No agent run is started here — attach is a plain
 * fetch; the design persists on the project until replaced/detached.
 */
export function FigmaImportDialog({ projectId }: { projectId?: string | null }) {
  const { t } = useTranslation("code")

  const [open, setOpen] = useState(false)
  const [connected, setConnected] = useState(false)
  const [url, setUrl] = useState("")
  const [resolving, setResolving] = useState(false)
  const [resolved, setResolved] = useState<FigmaResolved | null>(null)
  const [attaching, setAttaching] = useState(false)
  const [design, setDesign] = useState<FigmaAttachedDesign | null>(null)

  // Load any already-attached design when the dialog opens.
  useEffect(() => {
    if (!open || !projectId) return
    let cancelled = false
    void figmaApi
      .getDesign(projectId)
      .then((d) => !cancelled && setDesign(d))
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [open, projectId])

  const handleResolve = async () => {
    const value = url.trim()
    if (!value) return
    setResolving(true)
    setResolved(null)
    try {
      setResolved(await figmaApi.resolveUrl(value))
    } catch (error) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("figma.resolveFailed")
      toast.error(message)
    } finally {
      setResolving(false)
    }
  }

  const handleAttach = async () => {
    const value = url.trim()
    if (!value) return
    if (!projectId) {
      toast.error(t("figma.needProject"))
      return
    }
    setAttaching(true)
    try {
      const attached = await figmaApi.attachDesign(projectId, value)
      setDesign(attached)
      toast.success(t("figma.attached", { count: attached.count }))
    } catch (error) {
      const message =
        (error as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        t("figma.attachFailed")
      toast.error(message)
    } finally {
      setAttaching(false)
    }
  }

  const handleDetach = async () => {
    if (!projectId) return
    try {
      await figmaApi.detachDesign(projectId)
      setDesign(null)
    } catch {
      toast.error(t("figma.attachFailed"))
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          {t("figma.attach")}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("figma.attachTitle")}</DialogTitle>
          <DialogDescription>{t("figma.attachDesc")}</DialogDescription>
        </DialogHeader>

        <FigmaCredentialCard onConnectedChange={setConnected} />

        {design && (
          <div className="rounded-md border bg-muted/40 p-3 text-sm">
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium">
                {t("figma.attached", { count: design.count })}
                {design.file_name ? ` · ${design.file_name}` : ""}
              </span>
              <Button variant="ghost" size="sm" onClick={handleDetach}>
                {t("figma.detach")}
              </Button>
            </div>
            <p className="mt-1 truncate text-xs text-muted-foreground">
              {design.frames.map((f) => f.name).join("、")}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">{t("figma.attachHint")}</p>
          </div>
        )}

        <div className="space-y-2">
          <Label htmlFor="figma-url">{t("figma.importUrlLabel")}</Label>
          <div className="flex gap-2">
            <Input
              id="figma-url"
              value={url}
              placeholder="https://www.figma.com/design/…"
              onChange={(event) => setUrl(event.target.value)}
              disabled={!connected}
            />
            <Button
              variant="outline"
              size="sm"
              onClick={handleResolve}
              disabled={!connected || resolving || !url.trim()}
            >
              {resolving ? t("figma.resolving") : t("figma.resolve")}
            </Button>
          </div>
          {!connected && <p className="text-xs text-muted-foreground">{t("figma.connectFirst")}</p>}
        </div>

        {resolved && (
          <div className="flex items-center gap-3 rounded-md border p-3">
            {resolved.thumbnail_url && (
              <img
                src={resolved.thumbnail_url}
                alt={resolved.name || "Figma"}
                className="h-16 w-24 shrink-0 rounded object-cover"
              />
            )}
            <div className="min-w-0 text-sm">
              <div className="truncate font-medium">{resolved.name || resolved.file_key}</div>
              <div className="text-xs text-muted-foreground">{t("figma.targetWholeFile")}</div>
            </div>
          </div>
        )}

        <div className="flex items-center justify-between gap-2">
          {!projectId && <p className="text-xs text-muted-foreground">{t("figma.needProject")}</p>}
          <Button
            className="ml-auto"
            onClick={handleAttach}
            disabled={!connected || !url.trim() || !projectId || attaching}
          >
            {attaching ? t("figma.attaching") : t("figma.attachStart")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default FigmaImportDialog
