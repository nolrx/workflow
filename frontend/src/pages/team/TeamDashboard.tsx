import { useEffect, useState } from "react"
import { useParams } from "react-router-dom"
import { Users, Settings, CreditCard, Mail, Loader2, Crown, Shield, User } from "lucide-react"
import { useTranslation } from "react-i18next"
import { AppLayout } from "@/components/layout/AppLayout"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Loading } from "@/components/common/Loading"
import { useTeamStore, type TeamMember } from "@/stores/teamStore"
import { toast } from "sonner"

const roleIcons = {
  owner: Crown,
  admin: Shield,
  member: User,
  viewer: User,
}

export function TeamDashboard() {
  const { t } = useTranslation("team")
  const { slug } = useParams<{ slug: string }>()
  const {
    teams,
    currentTeam,
    members,
    fetchTeam,
    fetchMembers,
    inviteMember,
    setCurrentTeam,
    isLoading,
    error,
  } = useTeamStore()

  const [isInviteOpen, setIsInviteOpen] = useState(false)
  const [inviteEmail, setInviteEmail] = useState("")
  const [inviteRole, setInviteRole] = useState<"admin" | "member" | "viewer">("member")
  const [isInviting, setIsInviting] = useState(false)

  useEffect(() => {
    // Find team by slug
    const team = teams.find((t) => t.slug === slug)
    if (team) {
      setCurrentTeam(team)
      fetchMembers(team.id)
    } else if (slug) {
      // Try to fetch from API
      fetchTeam(slug)
    }
  }, [slug, teams, fetchTeam, fetchMembers, setCurrentTeam])

  const handleInvite = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!currentTeam || !inviteEmail.trim()) return

    setIsInviting(true)
    try {
      await inviteMember(currentTeam.id, { email: inviteEmail, role: inviteRole })
      toast.success(t("dashboard.toast.inviteSuccess"))
      setIsInviteOpen(false)
      setInviteEmail("")
      setInviteRole("member")
    } catch {
      toast.error(error || t("dashboard.toast.inviteFailed"))
    } finally {
      setIsInviting(false)
    }
  }

  if (isLoading && !currentTeam) {
    return (
      <AppLayout title={t("dashboard.subtitle")}>
        <div className="flex items-center justify-center py-12">
          <Loading size="lg" />
        </div>
      </AppLayout>
    )
  }

  if (!currentTeam) {
    return (
      <AppLayout title={t("dashboard.subtitle")}>
        <div className="text-center py-12">
          <h2 className="text-xl font-semibold">{t("dashboard.notFound.title")}</h2>
          <p className="text-muted-foreground mt-2">
            {t("dashboard.notFound.description")}
          </p>
        </div>
      </AppLayout>
    )
  }

  return (
    <AppLayout title={currentTeam.name}>
      <div className="space-y-8">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold">{currentTeam.name}</h1>
            <p className="text-muted-foreground">
              {t("dashboard.subtitle")}
            </p>
          </div>
          <div className="flex gap-2">
            <Button variant="outline">
              <Settings className="mr-2 h-4 w-4" />
              {t("dashboard.buttons.settings")}
            </Button>
            <Button variant="outline">
              <CreditCard className="mr-2 h-4 w-4" />
              {t("dashboard.buttons.billing")}
            </Button>
          </div>
        </div>

        {/* Stats */}
        <div className="grid gap-4 md:grid-cols-3">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">{t("dashboard.stats.members.title")}</CardTitle>
              <Users className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">{members.length}</div>
              <p className="text-xs text-muted-foreground">
                {t("dashboard.stats.members.description")}
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">{t("dashboard.stats.credits.title")}</CardTitle>
              <CreditCard className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">0</div>
              <p className="text-xs text-muted-foreground">
                {t("dashboard.stats.credits.description")}
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
              <CardTitle className="text-sm font-medium">{t("dashboard.stats.projects.title")}</CardTitle>
              <Settings className="h-4 w-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-bold">0</div>
              <p className="text-xs text-muted-foreground">
                {t("dashboard.stats.projects.description")}
              </p>
            </CardContent>
          </Card>
        </div>

        {/* Members */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle>{t("dashboard.members.title")}</CardTitle>
              <CardDescription>
                {t("dashboard.members.description")}
              </CardDescription>
            </div>
            <Dialog open={isInviteOpen} onOpenChange={setIsInviteOpen}>
              <DialogTrigger asChild>
                <Button>
                  <Mail className="mr-2 h-4 w-4" />
                  {t("dashboard.members.invite")}
                </Button>
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>{t("dashboard.inviteDialog.title")}</DialogTitle>
                  <DialogDescription>
                    {t("dashboard.inviteDialog.description")}
                  </DialogDescription>
                </DialogHeader>
                <form onSubmit={handleInvite} className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="email">{t("dashboard.inviteDialog.emailLabel")}</Label>
                    <Input
                      id="email"
                      type="email"
                      placeholder={t("dashboard.inviteDialog.emailPlaceholder")}
                      value={inviteEmail}
                      onChange={(e) => setInviteEmail(e.target.value)}
                      disabled={isInviting}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="role">{t("dashboard.inviteDialog.roleLabel")}</Label>
                    <select
                      id="role"
                      className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      value={inviteRole}
                      onChange={(e) =>
                        setInviteRole(e.target.value as "admin" | "member" | "viewer")
                      }
                      disabled={isInviting}
                    >
                      <option value="admin">{t("dashboard.inviteDialog.roles.admin")}</option>
                      <option value="member">{t("dashboard.inviteDialog.roles.member")}</option>
                      <option value="viewer">{t("dashboard.inviteDialog.roles.viewer")}</option>
                    </select>
                  </div>
                  <div className="flex justify-end gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => setIsInviteOpen(false)}
                      disabled={isInviting}
                    >
                      {t("dashboard.inviteDialog.cancel")}
                    </Button>
                    <Button type="submit" disabled={isInviting || !inviteEmail.trim()}>
                      {isInviting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                      {t("dashboard.inviteDialog.invite")}
                    </Button>
                  </div>
                </form>
              </DialogContent>
            </Dialog>
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <div className="flex items-center justify-center py-8">
                <Loading />
              </div>
            ) : members.length === 0 ? (
              <div className="text-center py-8 text-muted-foreground">
                {t("dashboard.members.noMembers")}
              </div>
            ) : (
              <div className="space-y-4">
                {members.map((member: TeamMember) => {
                  const RoleIcon = roleIcons[member.role]
                  return (
                    <div
                      key={member.id}
                      className="flex items-center justify-between rounded-lg border p-4"
                    >
                      <div className="flex items-center gap-4">
                        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-muted">
                          <User className="h-5 w-5" />
                        </div>
                        <div>
                          <p className="font-medium">
                            {member.user?.display_name || member.user?.email || "Unknown"}
                          </p>
                          <p className="text-sm text-muted-foreground">
                            {member.user?.email}
                          </p>
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        <RoleIcon className="h-4 w-4 text-muted-foreground" />
                        <span className="text-sm">{t(`dashboard.roles.${member.role}`)}</span>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </AppLayout>
  )
}
