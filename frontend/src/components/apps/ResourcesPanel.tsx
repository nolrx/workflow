/**
 * Resources panel — shows the frontend, backend and database resources backing a
 * deployed app at a glance, with quick links into the code / database viewers.
 */
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import { Boxes, Database, Download, FileCode2, Loader2, Server } from "lucide-react"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { downloadArtifact } from "@/lib/download"
import { appsApi, type AppResources, type SourceSummary } from "@/api/apps"

export function ResourcesPanel({
  projectId,
  onOpenCode,
  onOpenDatabase,
}: {
  projectId: string
  onOpenCode?: (lane: "frontend" | "backend") => void
  onOpenDatabase?: () => void
}) {
  const { t } = useTranslation("apps")
  const [res, setRes] = useState<AppResources | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    const run = async () => {
      setLoading(true)
      try {
        const r = await appsApi.resources(projectId)
        if (alive) setRes(r)
      } finally {
        if (alive) setLoading(false)
      }
    }
    void run()
    return () => {
      alive = false
    }
  }, [projectId])

  const download = async (summary: SourceSummary | null) => {
    if (!summary?.artifact_id) return
    try {
      await downloadArtifact(summary.artifact_id, summary.filename || "source.zip")
    } catch {
      toast.error(t("toast.error"))
    }
  }

  if (loading) {
    return (
      <div className="flex justify-center py-12 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    )
  }
  if (!res) return null

  return (
    <div className="grid gap-4 md:grid-cols-3">
      <SourceCard
        icon={<FileCode2 className="h-4 w-4" />}
        title={t("lanes.frontend")}
        summary={res.frontend}
        onView={() => onOpenCode?.("frontend")}
        onDownload={() => download(res.frontend)}
        viewLabel={t("resources.viewCode")}
        countLabel={t("resources.files")}
        emptyLabel={t("resources.none")}
        downloadLabel={t("code.download")}
      />
      <SourceCard
        icon={<Server className="h-4 w-4" />}
        title={t("lanes.backend")}
        summary={res.backend}
        onView={() => onOpenCode?.("backend")}
        onDownload={() => download(res.backend)}
        viewLabel={t("resources.viewCode")}
        countLabel={t("resources.files")}
        emptyLabel={t("resources.none")}
        downloadLabel={t("code.download")}
      />
      <Card className="flex flex-col gap-2 p-4">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <Database className="h-4 w-4" />
          {t("resources.database")}
        </div>
        <div className="space-y-0.5 text-xs text-muted-foreground">
          <div>
            {t("resources.engine")}: <code>{res.database.engine}</code>
          </div>
          {res.database.db_name && (
            <div className="truncate" title={res.database.db_name}>
              {t("resources.dbName")}: <code>{res.database.db_name}</code>
            </div>
          )}
          {res.database.table_count != null && (
            <div>
              {t("resources.tables")}: {res.database.table_count}
            </div>
          )}
        </div>
        <div className="mt-auto pt-1">
          <Button
            variant="outline"
            size="sm"
            onClick={onOpenDatabase}
            disabled={!res.database.introspectable}
          >
            <Boxes className="mr-1 h-3.5 w-3.5" />
            {t("resources.manageDb")}
          </Button>
        </div>
      </Card>
    </div>
  )
}

function SourceCard({
  icon,
  title,
  summary,
  onView,
  onDownload,
  viewLabel,
  countLabel,
  emptyLabel,
  downloadLabel,
}: {
  icon: React.ReactNode
  title: string
  summary: SourceSummary | null
  onView: () => void
  onDownload: () => void
  viewLabel: string
  countLabel: string
  emptyLabel: string
  downloadLabel: string
}) {
  return (
    <Card className="flex flex-col gap-2 p-4">
      <div className="flex items-center gap-2 text-sm font-semibold">
        {icon}
        {title}
      </div>
      {summary ? (
        <>
          <div className="text-xs text-muted-foreground">
            {summary.file_count} {countLabel}
          </div>
          <div className="mt-auto flex gap-2 pt-1">
            <Button variant="outline" size="sm" onClick={onView}>
              <FileCode2 className="mr-1 h-3.5 w-3.5" />
              {viewLabel}
            </Button>
            <Button variant="ghost" size="sm" onClick={onDownload}>
              <Download className="mr-1 h-3.5 w-3.5" />
              {downloadLabel}
            </Button>
          </div>
        </>
      ) : (
        <div className="text-xs text-muted-foreground">{emptyLabel}</div>
      )}
    </Card>
  )
}
