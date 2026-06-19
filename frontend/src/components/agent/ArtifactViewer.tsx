import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Download, FileJson, FileText, ImageIcon, Loader2 } from "lucide-react"

import { agentApi, type AgentArtifact } from "@/api/agent"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

interface ArtifactViewerProps {
  artifact: AgentArtifact
  onDownload?: (artifact: AgentArtifact) => void
}

function TypeIcon({ type }: { type: AgentArtifact["artifact_type"] }) {
  if (type === "image") return <ImageIcon className="h-4 w-4" />
  if (type === "json") return <FileJson className="h-4 w-4" />
  return <FileText className="h-4 w-4" />
}

export function ArtifactViewer({ artifact, onDownload }: ArtifactViewerProps) {
  const { t } = useTranslation("agent")

  // Image artifacts are now disk-backed: preview_url points at the JWT-protected
  // /file route, which an <img> tag can't authenticate (no Authorization header).
  // Inline data: URLs (older runs) render directly; disk-backed images are
  // fetched as an authenticated blob and shown via an object URL.
  const inlineDataUrl =
    artifact.artifact_type === "image" && artifact.preview_url?.startsWith("data:")
      ? artifact.preview_url
      : null
  const needsBlob =
    artifact.artifact_type === "image" && !inlineDataUrl && Boolean(artifact.file_url)

  const [blobUrl, setBlobUrl] = useState<string | null>(null)
  const [blobFailed, setBlobFailed] = useState(false)

  useEffect(() => {
    if (!needsBlob) return
    let cancelled = false
    let objectUrl: string | null = null
    agentApi
      .downloadArtifact(artifact.id)
      .then((blob) => {
        if (cancelled) return
        objectUrl = URL.createObjectURL(blob)
        setBlobUrl(objectUrl)
      })
      .catch(() => {
        // Surface as "empty" rather than a broken-image icon.
        if (!cancelled) setBlobFailed(true)
      })
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [needsBlob, artifact.id])

  const imageSrc = inlineDataUrl ?? blobUrl
  const imageLoading = needsBlob && !blobUrl && !blobFailed

  const jsonText =
    artifact.artifact_type === "json" && artifact.content_json !== undefined
      ? JSON.stringify(artifact.content_json, null, 2)
      : null

  return (
    <div className="rounded-md border">
      <div className="flex items-center justify-between gap-2 border-b bg-muted/40 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <TypeIcon type={artifact.artifact_type} />
          <span className="truncate text-sm font-medium">{artifact.title}</span>
          <Badge variant="outline" className="shrink-0 text-[10px] uppercase">
            {artifact.artifact_type}
          </Badge>
        </div>
        {artifact.file_url && onDownload && (
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2"
            onClick={() => onDownload(artifact)}
          >
            <Download className="mr-1 h-3.5 w-3.5" />
            {t("artifact.download")}
          </Button>
        )}
      </div>

      <div className="p-3">
        {artifact.artifact_type === "image" ? (
          imageSrc ? (
            <a href={imageSrc} target="_blank" rel="noreferrer">
              <img
                src={imageSrc}
                alt={artifact.title}
                className="max-h-72 w-full rounded border object-contain"
              />
            </a>
          ) : imageLoading ? (
            <div className="flex h-32 items-center justify-center text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">{t("artifact.empty")}</p>
          )
        ) : jsonText !== null ? (
          <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words text-xs">
            {jsonText}
          </pre>
        ) : artifact.content_text ? (
          <pre
            className={cn(
              "max-h-72 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed"
            )}
          >
            {artifact.content_text}
          </pre>
        ) : (
          <p className="text-xs text-muted-foreground">{t("artifact.empty")}</p>
        )}
      </div>
    </div>
  )
}

export default ArtifactViewer
