import { useEffect } from "react"
import { Link, useLocation, useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import {
  Plus,
  Settings,
  Users,
  CreditCard,
  ChevronDown,
  ScrollText,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
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
}

interface SidebarContentProps {
  onNavigate?: () => void
}

export function SidebarContent({ onNavigate }: SidebarContentProps) {
  const location = useLocation()
  const navigate = useNavigate()
  const { t } = useTranslation()

  // Code-domain sessions (the "creation" entities double as sessions).
  const codeProjects = useCodeStore((s) => s.projects)
  const fetchCodeProjects = useCodeStore((s) => s.fetchProjects)

  const balance = useCreditStore((s) => s.balance)
  const fetchBalance = useCreditStore((s) => s.fetchBalance)
  const isAdmin = useAuthStore((s) => s.user?.role === "admin")

  useEffect(() => {
    void fetchCodeProjects(15, 0)
  }, [fetchCodeProjects])

  useEffect(() => {
    void fetchBalance()
  }, [fetchBalance])

  const untitled = t("sidebar.untitled")
  const sessions: SessionItem[] = codeProjects.map((p) => ({
    id: p.id,
    label: p.title || p.requirement_input?.slice(0, 40) || untitled,
    href: `/code/${p.id}`,
  }))

  const handleNewSession = () => {
    // Clear the in-memory project AND tear down any replayed agent run so the
    // studio opens to a blank conversation (not the previous session's).
    useCodeStore.getState().setCurrentProject(null)
    useAgentStore.getState().reset()
    onNavigate?.()
    navigate("/code")
  }

  return (
    <>
      {/* Logo */}
      <div className="flex h-16 items-center border-b px-6">
        <Link to="/" className="flex items-center gap-2" onClick={onNavigate}>
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <span className="text-sm font-bold">{t("brand.name")}</span>
          </div>
          <span className="font-semibold">{t("brand.subtitle")}</span>
        </Link>
      </div>

      {/* Team Selector */}
      <div className="border-b p-4">
        <Button variant="outline" className="w-full justify-between">
          <span className="truncate">{t("sidebar.personal")}</span>
          <ChevronDown className="h-4 w-4 shrink-0" />
        </Button>
      </div>

      {/* New session + session list */}
      <div className="flex min-h-0 flex-1 flex-col p-3">
        <Button
          onClick={handleNewSession}
          className="mb-3 w-full justify-start gap-2"
        >
          <Plus className="h-4 w-4" />
          {t("sidebar.newSession")}
        </Button>

        <div className="mb-1 px-1 text-xs font-medium uppercase text-muted-foreground">
          {t("sidebar.recentSessions")}
        </div>

        <nav className="min-h-0 flex-1 space-y-1 overflow-y-auto">
          {sessions.length === 0 ? (
            <p className="px-2 py-3 text-sm text-muted-foreground">
              {t("sidebar.noSessions")}
            </p>
          ) : (
            sessions.map((session) => {
              const isActive = location.pathname.includes(session.id)
              return (
                <Link
                  key={session.id}
                  to={session.href}
                  title={session.label}
                  onClick={onNavigate}
                  className={cn(
                    "block truncate rounded-md px-3 py-2 text-sm transition-colors",
                    isActive
                      ? "bg-accent font-medium text-accent-foreground"
                      : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                  )}
                >
                  {session.label}
                </Link>
              )
            })
          )}
        </nav>

        {/* Settings nav */}
        <div className="mt-2 space-y-1 border-t pt-2">
          {settingsNavItems.map((item) => {
            const isActive = location.pathname === item.href
            return (
              <Link
                key={item.href}
                to={item.href}
                onClick={onNavigate}
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
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
                "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                location.pathname.startsWith("/admin")
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
              )}
            >
              <ScrollText className="h-4 w-4" />
              {t("admin:nav.prompts")}
            </Link>
          )}
        </div>
      </div>

      {/* Credits Display (live balance) */}
      <div className="border-t p-4">
        <div className="rounded-lg bg-muted p-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium">{t("sidebar.credits")}</span>
            <span className="text-sm text-muted-foreground">
              {balance ? balance.balance : 0}
              {balance?.monthly_allocation ? ` / ${balance.monthly_allocation}` : ""}
            </span>
          </div>
          {balance?.monthly_allocation ? (
            <div className="mt-2 h-2 overflow-hidden rounded-full bg-background">
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
      </div>
    </>
  )
}

export function Sidebar() {
  return (
    <aside className="fixed left-0 top-0 z-40 h-screen w-64 border-r bg-card">
      <div className="flex h-full flex-col">
        <SidebarContent />
      </div>
    </aside>
  )
}
