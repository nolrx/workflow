import { useEffect, useRef, useState } from "react"
import { Link, useLocation, useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { toast } from "sonner"
import {
  Plus,
  Settings,
  Users,
  CreditCard,
  ChevronDown,
  ScrollText,
  CornerDownRight,
  Loader2,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { DeleteSessionDialog } from "@/components/code/DeleteSessionDialog"
import { SessionContextMenu } from "@/components/code/SessionContextMenu"
import { useAgentStore } from "@/stores/agentStore"
import { useCodeStore } from "@/stores/codeStore"
import { useCreditStore } from "@/stores/creditStore"
import { useAuthStore } from "@/stores/authStore"

const settingsNavItems = [
  { titleKey: "nav.settings", href: "/settings", icon: Settings },
  { titleKey: "nav.team", href: "/team", icon: Users },
  { titleKey: "nav.billing", href: "/settings/billing", icon: CreditCard },
]

interface SessionItem {
  id: string
  label: string
  href: string
  isDeployed: boolean
}

interface SidebarContentProps {
  onNavigate?: () => void
}

/** How many sessions to fetch per page (initial load + each scroll page). */
const SESSIONS_PAGE_SIZE = 15

export function SidebarContent({ onNavigate }: SidebarContentProps) {
  const location = useLocation()
  const navigate = useNavigate()
  const { t } = useTranslation()

  const [contextMenu, setContextMenu] = useState<{
    open: boolean
    x: number
    y: number
    session: SessionItem | null
  }>({ open: false, x: 0, y: 0, session: null })
  const [deleteDialog, setDeleteDialog] = useState<{
    open: boolean
    session: SessionItem | null
  }>({ open: false, session: null })
  const [isDeleting, setIsDeleting] = useState(false)

  // Code-domain sessions (the "creation" entities double as sessions).
  const codeProjects = useCodeStore((s) => s.projects)
  const currentProject = useCodeStore((s) => s.project)
  const fetchCodeProjects = useCodeStore((s) => s.fetchProjects)
  const deleteCodeProject = useCodeStore((s) => s.deleteProject)
  const hasMoreProjects = useCodeStore((s) => s.hasMoreProjects)
  const isLoadingProjects = useCodeStore((s) => s.isLoadingProjects)

  const balance = useCreditStore((s) => s.balance)
  const fetchBalance = useCreditStore((s) => s.fetchBalance)
  const isAdmin = useAuthStore((s) => s.user?.role === "admin")

  // Lazy-load: fetch only the first page on mount; later pages stream in via
  // the infinite-scroll observer below — we never load every session at once.
  useEffect(() => {
    void fetchCodeProjects(SESSIONS_PAGE_SIZE, 0)
  }, [fetchCodeProjects])

  useEffect(() => {
    void fetchBalance()
  }, [fetchBalance])

  // Infinite scroll: when the sentinel at the list's bottom enters the
  // scroll viewport, append the next page. Re-created on each length change so
  // a still-visible sentinel keeps chaining until it scrolls out of view or
  // the server reports no more. Concurrent fetches are de-duped in the store.
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const sentinelRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const root = scrollRef.current
    const sentinel = sentinelRef.current
    if (!root || !sentinel || !hasMoreProjects) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          void fetchCodeProjects(
            SESSIONS_PAGE_SIZE,
            useCodeStore.getState().projects.length
          )
        }
      },
      { root, rootMargin: "120px" }
    )
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [hasMoreProjects, codeProjects.length, fetchCodeProjects])

  const untitled = t("sidebar.untitled")
  const sessions: SessionItem[] = codeProjects.map((p) => ({
    id: p.id,
    label: p.title || p.requirement_input?.slice(0, 40) || untitled,
    href: `/code/${p.id}`,
    isDeployed: !!p.is_deployed,
  }))

  const handleNewSession = () => {
    // Clear the in-memory project AND tear down any replayed agent run so the
    // studio opens to a blank conversation (not the previous session's).
    useCodeStore.getState().setCurrentProject(null)
    useAgentStore.getState().reset()
    onNavigate?.()
    navigate("/code")
  }

  const currentSessionLabel =
    currentProject?.title || currentProject?.requirement_input?.slice(0, 40) || untitled

  return (
    <>
      {/* New session */}
      <div className="p-3">
        <Button
          onClick={handleNewSession}
          className="w-full justify-start gap-2 rounded-sm"
        >
          <Plus className="h-4 w-4" />
          {t("sidebar.newSession")}
        </Button>
      </div>

      {/* Prominent "return to current session" shortcut so users never get
          stranded after navigating to settings/team/admin pages. */}
      {currentProject && (
        <div className="border-y bg-accent/40 px-3 py-3">
          <div className="mb-1.5 flex items-center gap-2 text-xs font-medium uppercase text-muted-foreground">
            <CornerDownRight className="h-3.5 w-3.5" />
            {t("sidebar.currentSession")}
          </div>
          <Link
            to={`/code/${currentProject.id}`}
            onClick={onNavigate}
            title={currentSessionLabel}
            className={cn(
              "block truncate border-l-2 py-1.5 pl-3 pr-2 text-sm font-medium transition-colors",
              location.pathname.includes(currentProject.id)
                ? "border-primary bg-primary/10 text-primary"
                : "border-transparent hover:bg-accent hover:text-accent-foreground"
            )}
          >
            {currentSessionLabel}
          </Link>
        </div>
      )}

      {/* Recent sessions */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
        <div className="px-3 py-2 text-xs font-medium uppercase text-muted-foreground">
          {t("sidebar.recentSessions")}
        </div>
        <nav className="space-y-0">
          {sessions.length === 0 ? (
            !isLoadingProjects && (
              <p className="px-3 py-3 text-sm text-muted-foreground">
                {t("sidebar.noSessions")}
              </p>
            )
          ) : (
            sessions.map((session) => {
              const isActive = location.pathname.includes(session.id)
              return (
                <Link
                  key={session.id}
                  to={session.href}
                  title={session.label}
                  onClick={onNavigate}
                  onContextMenu={(event) => {
                    event.preventDefault()
                    setContextMenu({
                      open: true,
                      x: event.clientX,
                      y: event.clientY,
                      session,
                    })
                  }}
                  className={cn(
                    "block truncate border-l-2 px-3 py-2 text-sm transition-colors",
                    isActive
                      ? "border-primary bg-accent font-medium text-accent-foreground"
                      : "border-transparent text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                  )}
                >
                  {session.label}
                </Link>
              )
            })
          )}
        </nav>
        {/* Sentinel + spinner that drive/indicate infinite-scroll loading. */}
        {hasMoreProjects && <div ref={sentinelRef} className="h-px" />}
        {isLoadingProjects && (
          <div className="flex items-center justify-center py-3 text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
          </div>
        )}
      </div>

      {/* Settings nav */}
      <div className="border-t">
        {settingsNavItems.map((item) => {
          const isActive = location.pathname === item.href
          return (
            <Link
              key={item.href}
              to={item.href}
              onClick={onNavigate}
              className={cn(
                "flex items-center gap-3 border-l-2 px-4 py-2.5 text-sm font-medium transition-colors",
                isActive
                  ? "border-primary bg-primary/10 text-primary"
                  : "border-transparent text-muted-foreground hover:bg-accent hover:text-accent-foreground"
              )}
            >
              <item.icon className="h-4 w-4" />
              {t(item.titleKey)}
            </Link>
          )
        })}
        {isAdmin && (
          <Link
            to="/admin/prompts"
            onClick={onNavigate}
            className={cn(
              "flex items-center gap-3 border-l-2 px-4 py-2.5 text-sm font-medium transition-colors",
              location.pathname.startsWith("/admin")
                ? "border-primary bg-primary/10 text-primary"
                : "border-transparent text-muted-foreground hover:bg-accent hover:text-accent-foreground"
            )}
          >
            <ScrollText className="h-4 w-4" />
            {t("admin:nav.prompts")}
          </Link>
        )}
      </div>

      {/* Team Selector */}
      <div className="border-t p-3">
        <Button
          variant="ghost"
          className="h-auto w-full justify-between rounded-none px-4 py-2.5"
        >
          <span className="flex items-center gap-3">
            <Users className="h-4 w-4" />
            <span className="truncate">{t("sidebar.personal")}</span>
          </span>
          <ChevronDown className="h-4 w-4 shrink-0" />
        </Button>
      </div>

      {/* Credits Display (live balance) */}
      <div className="border-t bg-muted p-4">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium">{t("sidebar.credits")}</span>
          <span className="text-sm text-muted-foreground">
            {balance ? balance.balance : 0}
            {balance?.monthly_allocation ? ` / ${balance.monthly_allocation}` : ""}
          </span>
        </div>
        {balance?.monthly_allocation ? (
          <div className="mt-2 h-2 bg-background">
            <div
              className="h-full bg-primary transition-all"
              style={{
                width: `${Math.min(
                  100,
                  Math.round((balance.balance / balance.monthly_allocation) * 100)
                )}%`,
              }}
            />
          </div>
        ) : null}
      </div>

      <SessionContextMenu
        open={contextMenu.open}
        x={contextMenu.x}
        y={contextMenu.y}
        isDeployed={contextMenu.session?.isDeployed ?? false}
        onClose={() => setContextMenu((prev) => ({ ...prev, open: false }))}
        onDelete={() => {
          const session = contextMenu.session
          if (!session || session.isDeployed) return
          setDeleteDialog({ open: true, session })
          setContextMenu((prev) => ({ ...prev, open: false }))
        }}
      />
      <DeleteSessionDialog
        open={deleteDialog.open}
        onOpenChange={(open) =>
          setDeleteDialog({ open, session: open ? deleteDialog.session : null })
        }
        title={deleteDialog.session?.label ?? ""}
        isDeployed={deleteDialog.session?.isDeployed ?? false}
        isDeleting={isDeleting}
        onConfirm={async () => {
          const session = deleteDialog.session
          if (!session) return
          setIsDeleting(true)
          const ok = await deleteCodeProject(session.id)
          setIsDeleting(false)
          setDeleteDialog({ open: false, session: null })
          if (ok) {
            toast.success(t("toast.deleted"))
            if (currentProject?.id === session.id) {
              useAgentStore.getState().reset()
              navigate("/code")
            }
          } else {
            toast.error(useCodeStore.getState().error || t("toast.error"))
          }
        }}
      />
    </>
  )
}

export function Sidebar() {
  return (
    <aside className="hidden h-full w-64 shrink-0 border-r bg-card lg:block">
      <div className="flex h-full flex-col">
        <SidebarContent />
      </div>
    </aside>
  )
}
