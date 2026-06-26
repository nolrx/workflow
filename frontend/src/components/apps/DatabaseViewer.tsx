/**
 * Read-only database viewer (数据库管理入口) for a deployed app — lists the app's
 * tables, their columns and row counts, and previews sample rows. Purely
 * introspective: no writes, no destructive ops.
 */
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Database, Loader2, Table2 } from "lucide-react"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { appsApi, type DatabaseInfo, type TableRows } from "@/api/apps"

export function DatabaseViewer({ projectId }: { projectId: string }) {
  const { t } = useTranslation("apps")
  const [info, setInfo] = useState<DatabaseInfo | null>(null)
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<string | null>(null)
  const [rows, setRows] = useState<TableRows | null>(null)
  const [rowsLoading, setRowsLoading] = useState(false)

  useEffect(() => {
    let alive = true
    const run = async () => {
      setLoading(true)
      try {
        const d = await appsApi.database(projectId)
        if (alive) setInfo(d)
      } finally {
        if (alive) setLoading(false)
      }
    }
    void run()
    return () => {
      alive = false
    }
  }, [projectId])

  const openTable = async (name: string) => {
    setSelected(name)
    setRowsLoading(true)
    try {
      setRows(await appsApi.tableRows(projectId, name, 20))
    } finally {
      setRowsLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    )
  }

  if (!info || !info.available) {
    return (
      <Card className="p-6 text-sm text-muted-foreground">
        {info?.engine === "sqlite" ? t("database.sqlite") : t("database.unavailable")}
      </Card>
    )
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[260px_1fr]">
      <Card className="p-3">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium">
          <Database className="h-4 w-4" />
          {info.db_name || t("database.title")}
        </div>
        <div className="space-y-0.5">
          {info.tables.length === 0 ? (
            <p className="px-1 py-2 text-sm text-muted-foreground">{t("database.noTables")}</p>
          ) : (
            info.tables.map((tbl) => (
              <button
                key={tbl.name}
                onClick={() => openTable(tbl.name)}
                className={`flex w-full items-center justify-between gap-2 rounded px-2 py-1.5 text-left text-sm transition-colors ${
                  selected === tbl.name ? "bg-primary/10 text-primary" : "hover:bg-accent"
                }`}
              >
                <span className="flex items-center gap-1.5 truncate">
                  <Table2 className="h-3.5 w-3.5 shrink-0" />
                  {tbl.name}
                </span>
                {tbl.row_count != null && (
                  <span className="shrink-0 text-xs text-muted-foreground">{tbl.row_count}</span>
                )}
              </button>
            ))
          )}
        </div>
      </Card>

      <Card className="min-w-0 p-4">
        {!selected ? (
          <p className="py-8 text-center text-sm text-muted-foreground">{t("database.pickTable")}</p>
        ) : (
          <div className="space-y-4">
            <div>
              <h4 className="mb-2 text-sm font-semibold">{selected}</h4>
              <div className="flex flex-wrap gap-1.5">
                {(info.tables.find((x) => x.name === selected)?.columns || []).map((c) => (
                  <span
                    key={c.name}
                    className="rounded border bg-muted px-2 py-0.5 text-xs"
                    title={c.type}
                  >
                    {c.name}
                    <span className="ml-1 text-muted-foreground">{c.type}</span>
                  </span>
                ))}
              </div>
            </div>

            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-xs font-medium uppercase text-muted-foreground">
                  {t("database.sampleRows")}
                </span>
                <Button variant="ghost" size="sm" onClick={() => openTable(selected)} disabled={rowsLoading}>
                  {rowsLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : t("actions.retry")}
                </Button>
              </div>
              {rowsLoading ? (
                <div className="flex justify-center py-6 text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" />
                </div>
              ) : rows && rows.available && rows.rows.length > 0 ? (
                <div className="overflow-x-auto rounded border">
                  <table className="w-full text-left text-xs">
                    <thead className="bg-muted">
                      <tr>
                        {rows.columns.map((c) => (
                          <th key={c} className="whitespace-nowrap px-2 py-1.5 font-medium">
                            {c}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.rows.map((row, ri) => (
                        <tr key={ri} className="border-t">
                          {row.map((cell, ci) => (
                            <td key={ci} className="max-w-[280px] truncate px-2 py-1.5" title={String(cell ?? "")}>
                              {cell === null ? <span className="text-muted-foreground">NULL</span> : String(cell)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="py-4 text-sm text-muted-foreground">{t("database.noRows")}</p>
              )}
            </div>
          </div>
        )}
      </Card>
    </div>
  )
}
