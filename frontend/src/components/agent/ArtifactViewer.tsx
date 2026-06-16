import { useTranslation } from "react-i18next"
import { Download, FileJson, FileText, ImageIcon } from "lucide-react"

import type { AgentArtifact } from "@/api/agent"
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
        {artifact.artifact_type === "image" && artifact.preview_url ? (
          <a href={artifact.preview_url} target="_blank" rel="noreferrer">
            <img
              src={artifact.preview_url}
              alt={artifact.title}
              className="max-h-72 w-full rounded border object-contain"
            />
          </a>
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
