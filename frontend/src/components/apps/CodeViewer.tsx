/**
 * In-app code viewer (应用代码入口) — browse the app's latest frontend/backend
 * source (read-only) and download the full source zip. Files are read from the
 * published source artifact server-side.
 */
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import { Download, FileCode2, Loader2 } from "lucide-react"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { downloadArtifact } from "@/lib/download"
import { appsApi, type CodeFile, type CodeListing } from "@/api/apps"

type Lane = "frontend" | "backend"

export function CodeViewer({
  projectId,
  initialLane = "frontend",
}: {
  projectId: string
  initialLane?: Lane
}) {
  const { t } = useTranslation("apps")
  const [lane, setLane] = useState<Lane>(initialLane)
  const [prevInitialLane, setPrevInitialLane] = useState<Lane>(initialLane)
  const [listing, setListing] = useState<CodeListing | null>(null)
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<string | null>(null)
  const [file, setFile] = useState<CodeFile | null>(null)
  const [fileLoading, setFileLoading] = useState(false)

  // Sync the lane when the parent requests a different one (set-state-during-render,
  // the React-recommended pattern for adjusting state from a changed prop).
  if (initialLane !== prevInitialLane) {
    setPrevInitialLane(initialLane)
    setLane(initialLane)
  }

  useEffect(() => {
    let alive = true
    const run = async () => {
      setLoading(true)
      setSelected(null)
      setFile(null)
      try {
        const d = await appsApi.code(projectId, lane)
        if (alive) setListing(d)
      } finally {
        if (alive) setLoading(false)
      }
    }
    void run()
    return () => {
      alive = false
    }
  }, [projectId, lane])

  const openFile = async (path: string) => {
    setSelected(path)
    setFileLoading(true)
    try {
      setFile(await appsApi.codeFile(projectId, lane, path))
    } finally {
      setFileLoading(false)
    }
  }

  const download = async () => {
    if (!listing?.artifact_id) return
    try {
      await downloadArtifact(listing.artifact_id, `${lane}-source.zip`)
    } catch {
      toast.error(t("toast.error"))
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="inline-flex overflow-hidden rounded-md border text-sm">
          {(["frontend", "backend"] as Lane[]).map((l) => (
            <button
              key={l}
              onClick={() => setLane(l)}
              className={`px-3 py-1.5 ${lane === l ? "bg-primary text-primary-foreground" : "hover:bg-accent"}`}
            >
              {t(`lanes.${l}`)}
            </button>
          ))}
        </div>
        {listing?.download_url && (
          <Button variant="outline" size="sm" onClick={download}>
            <Download className="mr-1 h-3.5 w-3.5" />
            {t("code.download")}
          </Button>
        )}
      </div>

      {loading ? (
        <div className="flex justify-center py-12 text-muted-foreground">
          <Loader2 className="h-5 w-5 animate-spin" />
        </div>
      ) : !listing || listing.files.length === 0 ? (
        <Card className="p-6 text-sm text-muted-foreground">{t("code.empty")}</Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
          <Card className="max-h-[60vh] overflow-y-auto p-2">
            {listing.files.map((f) => (
              <button
                key={f}
                onClick={() => openFile(f)}
                className={`flex w-full items-center gap-1.5 truncate rounded px-2 py-1 text-left text-xs transition-colors ${
                  selected === f ? "bg-primary/10 text-primary" : "hover:bg-accent"
                }`}
                title={f}
              >
                <FileCode2 className="h-3 w-3 shrink-0" />
                {f}
              </button>
            ))}
          </Card>

          <Card className="min-w-0 p-0">
            {!selected ? (
              <p className="py-8 text-center text-sm text-muted-foreground">{t("code.pickFile")}</p>
            ) : fileLoading ? (
              <div className="flex justify-center py-8 text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
              </div>
            ) : file?.is_binary ? (
              <p className="py-8 text-center text-sm text-muted-foreground">{t("code.binary")}</p>
            ) : (
              <div>
                <div className="flex items-center justify-between border-b px-3 py-1.5 text-xs text-muted-foreground">
                  <span className="truncate">{selected}</span>
                  {file?.truncated && <span>{t("code.truncated")}</span>}
                </div>
                <pre className="max-h-[60vh] overflow-auto p-3 text-xs leading-relaxed">
                  <code>{file?.content}</code>
                </pre>
              </div>
            )}
          </Card>
        </div>
      )}
    </div>
  )
}
