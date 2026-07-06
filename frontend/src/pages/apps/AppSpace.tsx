/**
 * Application Space (应用空间) — the management/iteration entry for the user's
 * ALREADY-DEPLOYED apps. First screen is the app list + management actions (no
 * marketing). Each app can be opened, inspected, iterated, redeployed, or replayed.
 */
import { useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import {
  Boxes,
  ExternalLink,
  Eye,
  History,
  LayoutGrid,
  List,
  Loader2,
  Power,
  RefreshCw,
  Search,
  Terminal,
  User as UserIcon,
  Users,
} from "lucide-react"
import { AppLayout } from "@/components/layout/AppLayout"
import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { AgentRunPanel } from "@/components/agent/AgentRunPanel"
import { DeployStatusBadge, HealthBadge } from "@/components/apps/badges"
import { useAppStore } from "@/stores/appStore"
import { useAgentStore } from "@/stores/agentStore"
import { useAuthStore } from "@/stores/authStore"
import { useTeamStore } from "@/stores/teamStore"
import { fullstackApi } from "@/api/fullstack"
import { openDeployedApp } from "@/lib/appPreview"
import { appsApi, type AppListItem } from "@/api/apps"

const STATUS_OPTIONS = ["running", "failed", "stopped", "rolled_back"]
const HEALTH_OPTIONS = ["healthy", "unhealthy", "unknown"]
const VIEW_KEY = "apps-view"
type AppView = "card" | "table"

export function AppSpace() {
  const { t } = useTranslation("apps")
  const navigate = useNavigate()
  const apps = useAppStore((s) => s.apps)
  const total = useAppStore((s) => s.total)
  const isLoading = useAppStore((s) => s.isLoadingApps)
  const isLoadingMore = useAppStore((s) => s.isLoadingMore)
  const error = useAppStore((s) => s.error)
  const fetchApps = useAppStore((s) => s.fetchApps)
  const loadMore = useAppStore((s) => s.loadMore)
  const openLatestRunForResource = useAgentStore((s) => s.openLatestRunForResource)
  const openPanel = useAgentStore((s) => s.openPanel)

  const teams = useTeamStore((s) => s.teams)
  const scopeTeamId = useTeamStore((s) => s.scopeTeamId)
  const setScopeTeamId = useTeamStore((s) => s.setScopeTeamId)
  const fetchTeams = useTeamStore((s) => s.fetchTeams)
  const isAdmin = useAuthStore((s) => s.user?.role === "admin")

  // Admin-only platform-wide view. Kept as local (non-persisted) state so it
  // resets on reload and never leaks into the sticky personal/team scope.
  const ALL_SCOPE = "__all__"
  const [adminAll, setAdminAll] = useState(false)

  const [search, setSearch] = useState("")
  const [status, setStatus] = useState("all")
  const [health, setHealth] = useState("all")
  const [redeployingId, setRedeployingId] = useState<string | null>(null)
  const [stoppingId, setStoppingId] = useState<string | null>(null)
  const [view, setView] = useState<AppView>(
    () => (localStorage.getItem(VIEW_KEY) as AppView) || "card"
  )

  const changeView = (next: AppView) => {
    setView(next)
    localStorage.setItem(VIEW_KEY, next)
  }

  // Load the teams the user belongs to so the scope switcher has options.
  useEffect(() => {
    void fetchTeams()
  }, [fetchTeams])

  const buildFilters = () => ({
    status: status === "all" ? undefined : status,
    health: health === "all" ? undefined : health,
    q: search.trim() || undefined,
    // Admin "全部" overrides the team scope; the backend ignores scope for non-admins.
    scope: adminAll ? "all" : undefined,
    team_id: adminAll ? null : scopeTeamId,
  })

  useEffect(() => {
    void fetchApps(buildFilters())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, health, scopeTeamId, adminAll])

  const applySearch = () => {
    void fetchApps(buildFilters())
  }

  const handleReplay = async (projectId: string) => {
    const ok = await openLatestRunForResource(projectId, "code_fullstack_deploy")
    if (ok) openPanel()
    else toast.error(t("toast.noRun"))
  }

  const handleRedeploy = async (projectId: string) => {
    setRedeployingId(projectId)
    try {
      await fullstackApi.deploy(projectId)
      toast.success(t("toast.deployStarted"))
    } catch {
      toast.error(t("toast.error"))
    } finally {
      setRedeployingId(null)
    }
  }

  const handleStop = async (projectId: string) => {
    if (stoppingId) return // one stop at a time; prevents rapid double-submit
    setStoppingId(projectId)
    try {
      await appsApi.stop(projectId)
      toast.success(t("toast.stopped"))
      // Await the refresh so the button stays disabled until the list reflects the
      // stopped status (the Stop button hides once is_running flips false).
      await fetchApps(buildFilters())
    } catch {
      toast.error(t("toast.error"))
    } finally {
      setStoppingId(null)
    }
  }

  return (
    <AppLayout title={t("title")}>
      <div className="space-y-6">
        <div>
          <h2 className="flex items-center gap-2 text-2xl font-bold tracking-tight">
            <Boxes className="h-6 w-6" />
            {t("title")}
          </h2>
          <p className="text-muted-foreground">{t("subtitle")}</p>
        </div>

        {/* Scope: personal vs each team the user belongs to. */}
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm text-muted-foreground">{t("scope.label")}</span>
          <Select
            value={adminAll ? ALL_SCOPE : (scopeTeamId ?? "personal")}
            onValueChange={(v) => {
              if (v === ALL_SCOPE) {
                setAdminAll(true)
                return
              }
              setAdminAll(false)
              setScopeTeamId(v === "personal" ? null : v)
            }}
          >
            <SelectTrigger className="w-[200px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="personal">
                <span className="flex items-center gap-2">
                  <UserIcon className="h-3.5 w-3.5" />
                  {t("scope.personal")}
                </span>
              </SelectItem>
              {teams.map((team) => (
                <SelectItem key={team.id} value={team.id}>
                  <span className="flex items-center gap-2">
                    <Users className="h-3.5 w-3.5" />
                    {team.name}
                  </span>
                </SelectItem>
              ))}
              {/* Admin-only: every deployed app across the platform (read-only). */}
              {isAdmin && (
                <SelectItem value={ALL_SCOPE}>
                  <span className="flex items-center gap-2">
                    <Boxes className="h-3.5 w-3.5" />
                    {t("scope.all")}
                  </span>
                </SelectItem>
              )}
            </SelectContent>
          </Select>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative flex-1 min-w-[200px]">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && applySearch()}
              placeholder={t("search")}
              className="pl-8"
            />
          </div>
          <Select value={status} onValueChange={setStatus}>
            <SelectTrigger className="w-[150px]">
              <SelectValue placeholder={t("filters.status")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t("filters.all")}</SelectItem>
              {STATUS_OPTIONS.map((s) => (
                <SelectItem key={s} value={s}>
                  {t(`deployStatus.${s}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={health} onValueChange={setHealth}>
            <SelectTrigger className="w-[150px]">
              <SelectValue placeholder={t("filters.health")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t("filters.all")}</SelectItem>
              {HEALTH_OPTIONS.map((h) => (
                <SelectItem key={h} value={h}>
                  {t(`health.${h}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {/* Card / table view toggle (persisted) */}
          <div className="ml-auto inline-flex overflow-hidden rounded-md border">
            <button
              onClick={() => changeView("card")}
              title={t("view.card")}
              className={`p-2 ${view === "card" ? "bg-primary text-primary-foreground" : "hover:bg-accent"}`}
            >
              <LayoutGrid className="h-4 w-4" />
            </button>
            <button
              onClick={() => changeView("table")}
              title={t("view.table")}
              className={`p-2 ${view === "table" ? "bg-primary text-primary-foreground" : "hover:bg-accent"}`}
            >
              <List className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* List */}
        {isLoading ? (
          <div className="flex items-center justify-center py-16 text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />
          </div>
        ) : error ? (
          <Card className="flex flex-col items-center justify-center gap-3 py-16 text-center text-sm text-destructive">
            {error}
            <Button variant="outline" size="sm" onClick={applySearch}>
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
              {t("actions.retry")}
            </Button>
          </Card>
        ) : apps.length === 0 ? (
          <Card className="flex items-center justify-center py-16 text-center text-sm text-muted-foreground">
            {t("empty")}
          </Card>
        ) : view === "table" ? (
          <AppTable
            apps={apps}
            redeployingId={redeployingId}
            stoppingId={stoppingId}
            onOpen={openDeployedApp}
            onDetail={(id) => navigate(`/apps/${id}`)}
            onDevMode={(id) => navigate(`/code/${id}/dev`)}
            onRedeploy={handleRedeploy}
            onReplay={handleReplay}
            onStop={handleStop}
          />
        ) : (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {apps.map((app) => (
              <AppCard
                key={app.project_id}
                app={app}
                showOwner={adminAll || !!scopeTeamId}
                redeploying={redeployingId === app.project_id}
                stopping={stoppingId === app.project_id}
                onOpen={() => openDeployedApp(app.project_id)}
                onDetail={() => navigate(`/apps/${app.project_id}`)}
                onDevMode={() => navigate(`/code/${app.project_id}/dev`)}
                onRedeploy={() => handleRedeploy(app.project_id)}
                onReplay={() => handleReplay(app.project_id)}
                onStop={() => handleStop(app.project_id)}
              />
            ))}
          </div>
        )}

        {/* Pagination: load the next page when more remain. */}
        {!isLoading && !error && apps.length > 0 && apps.length < total && (
          <div className="flex justify-center pt-2">
            <Button variant="outline" onClick={() => void loadMore()} disabled={isLoadingMore}>
              {isLoadingMore && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}
              {t("loadMore")} ({apps.length}/{total})
            </Button>
          </div>
        )}
      </div>

      <AgentRunPanel />
    </AppLayout>
  )
}

function AppCard({
  app,
  showOwner,
  redeploying,
  stopping,
  onOpen,
  onDetail,
  onDevMode,
  onRedeploy,
  onReplay,
  onStop,
}: {
  app: AppListItem
  showOwner: boolean
  redeploying: boolean
  stopping: boolean
  onOpen: () => void
  onDetail: () => void
  onDevMode: () => void
  onRedeploy: () => void
  onReplay: () => void
  onStop: () => void
}) {
  const { t } = useTranslation("apps")
  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-start justify-between gap-2">
        <button
          onClick={onDetail}
          className="truncate text-left text-base font-semibold hover:underline"
          title={app.title}
        >
          {app.title}
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <DeployStatusBadge status={app.deployment_status} />
        <HealthBadge health={app.health} />
      </div>
      <div className="space-y-0.5 text-xs text-muted-foreground">
        {showOwner && app.owner && (
          <div className="flex items-center gap-1">
            <UserIcon className="h-3 w-3" />
            {t("scope.createdBy", {
              name: app.owner.display_name || t("scope.unknownMember"),
            })}
          </div>
        )}
        <div>
          {t("detail.apiBase")}: <code className="text-[11px]">{app.api_base_path}</code>
        </div>
        {app.deployed_at && (
          <div>
            {t("columns.deployedAt")}: {new Date(app.deployed_at).toLocaleString()}
          </div>
        )}
      </div>
      <div className="mt-auto flex flex-wrap gap-2 pt-1">
        <Button size="sm" variant="default" onClick={onOpen} disabled={!app.is_running}>
          <ExternalLink className="mr-1 h-3.5 w-3.5" />
          {t("actions.open")}
        </Button>
        <Button size="sm" variant="outline" onClick={onDetail}>
          <Eye className="mr-1 h-3.5 w-3.5" />
          {t("actions.detail")}
        </Button>
        <Button size="sm" variant="outline" onClick={onDevMode}>
          <Terminal className="mr-1 h-3.5 w-3.5" />
          {t("actions.devMode")}
        </Button>
        <Button size="sm" variant="outline" onClick={onRedeploy} disabled={redeploying}>
          {redeploying ? (
            <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="mr-1 h-3.5 w-3.5" />
          )}
          {t("actions.redeploy")}
        </Button>
        <Button size="sm" variant="ghost" onClick={onReplay}>
          <History className="mr-1 h-3.5 w-3.5" />
          {t("actions.replay")}
        </Button>
        {app.is_running && (
          <Button size="sm" variant="ghost" className="text-destructive" onClick={onStop} disabled={stopping}>
            {stopping ? (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            ) : (
              <Power className="mr-1 h-3.5 w-3.5" />
            )}
            {t("actions.stop")}
          </Button>
        )}
      </div>
    </Card>
  )
}

function AppTable({
  apps,
  redeployingId,
  stoppingId,
  onOpen,
  onDetail,
  onDevMode,
  onRedeploy,
  onReplay,
  onStop,
}: {
  apps: AppListItem[]
  redeployingId: string | null
  stoppingId: string | null
  onOpen: (id: string) => void
  onDetail: (id: string) => void
  onDevMode: (id: string) => void
  onRedeploy: (id: string) => void
  onReplay: (id: string) => void
  onStop: (id: string) => void
}) {
  const { t } = useTranslation("apps")
  return (
    <Card className="overflow-x-auto p-0">
      <table className="w-full text-left text-sm">
        <thead className="border-b bg-muted/50">
          <tr>
            <th className="px-4 py-2.5 font-medium">{t("columns.app")}</th>
            <th className="px-3 py-2.5 font-medium">{t("columns.status")}</th>
            <th className="px-3 py-2.5 font-medium">{t("columns.health")}</th>
            <th className="px-3 py-2.5 font-medium">{t("columns.deployedAt")}</th>
            <th className="px-3 py-2.5 text-right font-medium">{t("columns.actions")}</th>
          </tr>
        </thead>
        <tbody>
          {apps.map((app) => (
            <tr key={app.project_id} className="border-b last:border-0 hover:bg-accent/40">
              <td className="px-4 py-2.5">
                <button
                  onClick={() => onDetail(app.project_id)}
                  className="font-medium hover:underline"
                  title={app.title}
                >
                  {app.title}
                </button>
              </td>
              <td className="px-3 py-2.5">
                <DeployStatusBadge status={app.deployment_status} />
              </td>
              <td className="px-3 py-2.5">
                <HealthBadge health={app.health} />
              </td>
              <td className="whitespace-nowrap px-3 py-2.5 text-xs text-muted-foreground">
                {app.deployed_at ? new Date(app.deployed_at).toLocaleString() : "—"}
              </td>
              <td className="px-3 py-2.5">
                <div className="flex justify-end gap-1">
                  <Button size="icon" variant="ghost" title={t("actions.open")} onClick={() => onOpen(app.project_id)} disabled={!app.is_running}>
                    <ExternalLink className="h-3.5 w-3.5" />
                  </Button>
                  <Button size="icon" variant="ghost" title={t("actions.detail")} onClick={() => onDetail(app.project_id)}>
                    <Eye className="h-3.5 w-3.5" />
                  </Button>
                  <Button size="icon" variant="ghost" title={t("actions.devMode")} onClick={() => onDevMode(app.project_id)}>
                    <Terminal className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    size="icon"
                    variant="ghost"
                    title={t("actions.redeploy")}
                    onClick={() => onRedeploy(app.project_id)}
                    disabled={redeployingId === app.project_id}
                  >
                    {redeployingId === app.project_id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <RefreshCw className="h-3.5 w-3.5" />
                    )}
                  </Button>
                  <Button size="icon" variant="ghost" title={t("actions.replay")} onClick={() => onReplay(app.project_id)}>
                    <History className="h-3.5 w-3.5" />
                  </Button>
                  {app.is_running && (
                    <Button
                      size="icon"
                      variant="ghost"
                      className="text-destructive"
                      title={t("actions.stop")}
                      onClick={() => onStop(app.project_id)}
                      disabled={stoppingId === app.project_id}
                    >
                      {stoppingId === app.project_id ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Power className="h-3.5 w-3.5" />
                      )}
                    </Button>
                  )}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}
