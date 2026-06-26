/**
 * Application detail (应用详情) — one deployed app's full context organized into
 * tabs: 概览 / 资源 / 数据库 / 代码 / 二次开发. Shows deployment/health/API base/
 * preview/tech-stack/recent runs/GitHub, the resource & database & code entries,
 * and the IterationPanel for secondary development. Reached at `/apps/:projectId`
 * and `/apps/:projectId/iterate` (the latter opens the 二次开发 tab).
 */
import { useEffect, useState } from "react"
import { useNavigate, useParams, useLocation } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import {
  ArrowLeft,
  ExternalLink,
  FileCode2,
  Github,
  History,
  Loader2,
  Power,
  RefreshCw,
} from "lucide-react"
import { AppLayout } from "@/components/layout/AppLayout"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { AgentRunPanel } from "@/components/agent/AgentRunPanel"
import {
  DeployStatusBadge,
  HealthBadge,
  RunStatusBadge,
} from "@/components/apps/badges"
import { IterationPanel } from "@/components/apps/IterationPanel"
import { ResourcesPanel } from "@/components/apps/ResourcesPanel"
import { DatabaseViewer } from "@/components/apps/DatabaseViewer"
import { CodeViewer } from "@/components/apps/CodeViewer"
import { LogsViewer } from "@/components/apps/LogsViewer"
import { useAppStore } from "@/stores/appStore"
import { useAgentStore } from "@/stores/agentStore"
import { fullstackApi } from "@/api/fullstack"
import { appsApi } from "@/api/apps"
import { openDeployedApp } from "@/lib/appPreview"
import type { AgentRun } from "@/api/agent"

export function AppDetail() {
  const { t } = useTranslation("apps")
  const navigate = useNavigate()
  const location = useLocation()
  const { projectId = "" } = useParams()

  const detail = useAppStore((s) => s.detail)
  const isLoading = useAppStore((s) => s.isLoadingDetail)
  const error = useAppStore((s) => s.error)
  const fetchApp = useAppStore((s) => s.fetchApp)
  const reset = useAppStore((s) => s.reset)
  const openRun = useAgentStore((s) => s.openRun)
  const openPanel = useAgentStore((s) => s.openPanel)

  const [tab, setTab] = useState(location.pathname.endsWith("/iterate") ? "iterate" : "overview")
  const [codeLane, setCodeLane] = useState<"frontend" | "backend">("frontend")
  const [prevPath, setPrevPath] = useState(location.pathname)
  const [probedHealth, setProbedHealth] = useState<string | null>(null)
  const [healthLoading, setHealthLoading] = useState(false)
  const [stopping, setStopping] = useState(false)

  // Manual re-probe (event handler — free to setState; not an effect).
  const refreshHealth = async () => {
    setHealthLoading(true)
    try {
      const res = await appsApi.refreshHealth(projectId)
      if (res.available) setProbedHealth(res.health)
    } catch {
      /* health probe is best-effort */
    } finally {
      setHealthLoading(false)
    }
  }

  useEffect(() => {
    if (projectId) void fetchApp(projectId)
    return () => reset()
  }, [projectId, fetchApp, reset])

  // Re-probe health on open so a now-unhealthy app is reflected (best-effort).
  useEffect(() => {
    if (!projectId) return
    let alive = true
    ;(async () => {
      try {
        const res = await appsApi.refreshHealth(projectId)
        if (alive && res.available) setProbedHealth(res.health)
      } catch {
        /* best-effort */
      }
    })()
    return () => {
      alive = false
    }
  }, [projectId])

  const stop = async () => {
    if (stopping) return // prevent rapid double-submit
    setStopping(true)
    try {
      await appsApi.stop(projectId)
      toast.success(t("toast.stopped"))
      await fetchApp(projectId)
    } catch {
      toast.error(t("toast.error"))
    } finally {
      setStopping(false)
    }
  }

  // Deep-link /apps/:id/iterate → open the 二次开发 tab (set-state-during-render,
  // guarded so it only fires when the path actually changes).
  if (location.pathname !== prevPath) {
    setPrevPath(location.pathname)
    if (location.pathname.endsWith("/iterate")) setTab("iterate")
  }

  const replay = async (run: AgentRun | null | undefined) => {
    if (!run) return
    await openRun(run.id)
    openPanel()
  }

  const redeploy = async () => {
    try {
      await fullstackApi.deploy(projectId)
      toast.success(t("toast.deployStarted"))
    } catch {
      toast.error(t("toast.error"))
    }
  }

  if (isLoading && !detail) {
    return (
      <AppLayout title={t("title")}>
        <div className="flex items-center justify-center py-20 text-muted-foreground">
          <Loader2 className="h-5 w-5 animate-spin" />
        </div>
      </AppLayout>
    )
  }

  if (!detail) {
    return (
      <AppLayout title={t("title")}>
        <Card className="flex flex-col items-center gap-3 p-8 text-center text-sm text-muted-foreground">
          <span className={error ? "text-destructive" : undefined}>{error || t("notFound")}</span>
          <Button variant="outline" size="sm" onClick={() => navigate("/apps")}>
            <ArrowLeft className="mr-1 h-4 w-4" />
            {t("actions.back")}
          </Button>
        </Card>
      </AppLayout>
    )
  }

  const { project, deployment, tech_stack, runs, github } = detail
  const isRunning = deployment?.status === "running"

  return (
    <AppLayout title={project.title}>
      <div className="space-y-6">
        <div className="flex items-center justify-between gap-2">
          <Button variant="ghost" size="sm" onClick={() => navigate("/apps")}>
            <ArrowLeft className="mr-1 h-4 w-4" />
            {t("actions.back")}
          </Button>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => openDeployedApp(projectId)} disabled={!isRunning}>
              <ExternalLink className="mr-1 h-4 w-4" />
              {t("actions.open")}
            </Button>
            <Button size="sm" variant="outline" onClick={() => navigate(`/code/${projectId}`)}>
              <FileCode2 className="mr-1 h-4 w-4" />
              {t("actions.openProject")}
            </Button>
            <Button size="sm" variant="outline" onClick={() => replay(runs.deploy)}>
              <History className="mr-1 h-4 w-4" />
              {t("actions.replay")}
            </Button>
            <Button size="sm" variant="outline" onClick={redeploy}>
              <RefreshCw className="mr-1 h-4 w-4" />
              {t("actions.redeploy")}
            </Button>
            {isRunning && (
              <Button size="sm" variant="outline" className="text-destructive" onClick={stop} disabled={stopping}>
                {stopping ? (
                  <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                ) : (
                  <Power className="mr-1 h-4 w-4" />
                )}
                {t("actions.stop")}
              </Button>
            )}
          </div>
        </div>

        {/* Title + status (always visible above the tabs) */}
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-xl font-bold">{project.title}</h2>
          <DeployStatusBadge status={deployment?.status ?? null} />
          <HealthBadge health={probedHealth ?? deployment?.health ?? null} />
          <button
            onClick={refreshHealth}
            title={t("health.refresh")}
            className="text-muted-foreground hover:text-foreground"
            disabled={healthLoading}
          >
            <RefreshCw className={`h-3.5 w-3.5 ${healthLoading ? "animate-spin" : ""}`} />
          </button>
          <span className="rounded border px-2 py-0.5 text-xs text-muted-foreground">
            {project.visibility}
          </span>
        </div>

        <Tabs value={tab} onValueChange={setTab}>
          <TabsList>
            <TabsTrigger value="overview">{t("tabs.overview")}</TabsTrigger>
            <TabsTrigger value="resources">{t("tabs.resources")}</TabsTrigger>
            <TabsTrigger value="database">{t("tabs.database")}</TabsTrigger>
            <TabsTrigger value="code">{t("tabs.code")}</TabsTrigger>
            <TabsTrigger value="logs">{t("tabs.logs")}</TabsTrigger>
            <TabsTrigger value="iterate">{t("tabs.iterate")}</TabsTrigger>
          </TabsList>

          {/* Overview */}
          <TabsContent value="overview" className="space-y-6">
            <Card className="space-y-4 p-5">
              {project.requirement_summary && (
                <div>
                  <div className="text-xs font-medium uppercase text-muted-foreground">
                    {t("detail.requirement")}
                  </div>
                  <p className="mt-1 whitespace-pre-wrap text-sm text-muted-foreground">
                    {project.requirement_summary}
                  </p>
                </div>
              )}
              <div className="grid gap-3 sm:grid-cols-2">
                <InfoRow label={t("detail.apiBase")} value={detail.api_base_path} mono />
                <InfoRow label={t("detail.previewUrl")} value={detail.preview_url} mono />
                {(tech_stack?.language || tech_stack?.framework) && (
                  <InfoRow
                    label={t("detail.techStack")}
                    value={[tech_stack.language, tech_stack.framework].filter(Boolean).join(" / ")}
                  />
                )}
                {deployment?.deployed_at && (
                  <InfoRow
                    label={t("columns.deployedAt")}
                    value={new Date(deployment.deployed_at).toLocaleString()}
                  />
                )}
              </div>
            </Card>

            <Card className="space-y-3 p-5">
              <h3 className="text-lg font-semibold">{t("detail.recentRuns")}</h3>
              <Separator />
              <div className="grid gap-2 sm:grid-cols-2">
                {(["frontend", "backend", "middleware", "deploy"] as const).map((lane) => {
                  const run = runs[lane]
                  return (
                    <div key={lane} className="flex items-center justify-between gap-2 text-sm">
                      <span className="text-muted-foreground">{t(`lanes.${lane}`)}</span>
                      {run ? (
                        <button
                          onClick={() => replay(run)}
                          className="flex items-center gap-2 hover:underline"
                        >
                          <RunStatusBadge status={run.status} />
                        </button>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </div>
                  )
                })}
              </div>
            </Card>

            {github && (
              <Card className="space-y-2 p-5">
                <h3 className="flex items-center gap-2 text-lg font-semibold">
                  <Github className="h-4 w-4" />
                  {t("detail.github")}
                </h3>
                <Separator />
                <div className="grid gap-2 text-sm sm:grid-cols-2">
                  {github.html_url ? (
                    <a
                      href={String(github.html_url)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="truncate text-primary hover:underline"
                    >
                      {github.repo_owner}/{github.repo_name}
                    </a>
                  ) : (
                    <span className="text-muted-foreground">
                      {github.repo_owner}/{github.repo_name}
                    </span>
                  )}
                  {github.dev_branch && (
                    <InfoRow label={t("detail.devBranch")} value={String(github.dev_branch)} mono />
                  )}
                </div>
              </Card>
            )}
          </TabsContent>

          {/* Resources */}
          <TabsContent value="resources">
            <ResourcesPanel
              projectId={projectId}
              onOpenCode={(lane) => {
                setCodeLane(lane)
                setTab("code")
              }}
              onOpenDatabase={() => setTab("database")}
            />
          </TabsContent>

          {/* Database */}
          <TabsContent value="database">
            <DatabaseViewer projectId={projectId} />
          </TabsContent>

          {/* Code */}
          <TabsContent value="code">
            <CodeViewer projectId={projectId} initialLane={codeLane} />
          </TabsContent>

          {/* Runtime logs */}
          <TabsContent value="logs">
            <LogsViewer projectId={projectId} />
          </TabsContent>

          {/* Secondary development */}
          <TabsContent value="iterate">
            <Card className="p-5">
              <IterationPanel projectId={projectId} onSettled={() => fetchApp(projectId)} />
            </Card>
          </TabsContent>
        </Tabs>
      </div>

      <AgentRunPanel />
    </AppLayout>
  )
}

function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="text-xs font-medium uppercase text-muted-foreground">{label}</div>
      <div className={`mt-0.5 truncate text-sm ${mono ? "font-mono text-[13px]" : ""}`} title={value}>
        {value}
      </div>
    </div>
  )
}
