import { useEffect, useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { Plus, Users, ChevronRight, Loader2 } from "lucide-react"
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
import { EmptyState } from "@/components/common/EmptyState"
import { useTeamStore } from "@/stores/teamStore"
import { toast } from "sonner"

export function TeamList() {
  const { t } = useTranslation("team")
  const navigate = useNavigate()
  const { teams, fetchTeams, createTeam, isLoading, error } = useTeamStore()
  const [isCreateOpen, setIsCreateOpen] = useState(false)
  const [newTeamName, setNewTeamName] = useState("")
  const [isCreating, setIsCreating] = useState(false)

  useEffect(() => {
    fetchTeams()
  }, [fetchTeams])

  const handleCreateTeam = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newTeamName.trim()) return

    setIsCreating(true)
    try {
      const team = await createTeam({ name: newTeamName })
      toast.success(t("list.toast.createSuccess"))
      setIsCreateOpen(false)
      setNewTeamName("")
      navigate(`/team/${team.slug}`)
    } catch {
      toast.error(error || t("list.toast.createFailed"))
    } finally {
      setIsCreating(false)
    }
  }

  return (
    <AppLayout title={t("list.title")}>
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold">{t("list.title")}</h1>
            <p className="text-muted-foreground">
              {t("list.subtitle")}
            </p>
          </div>
          <Dialog open={isCreateOpen} onOpenChange={setIsCreateOpen}>
            <DialogTrigger asChild>
              <Button>
                <Plus className="mr-2 h-4 w-4" />
                {t("list.createTeam")}
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>{t("list.dialog.title")}</DialogTitle>
                <DialogDescription>
                  {t("list.dialog.description")}
                </DialogDescription>
              </DialogHeader>
              <form onSubmit={handleCreateTeam} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="teamName">{t("list.dialog.nameLabel")}</Label>
                  <Input
                    id="teamName"
                    placeholder={t("list.dialog.namePlaceholder")}
                    value={newTeamName}
                    onChange={(e) => setNewTeamName(e.target.value)}
                    disabled={isCreating}
                  />
                </div>
                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setIsCreateOpen(false)}
                    disabled={isCreating}
                  >
                    {t("list.dialog.cancel")}
                  </Button>
                  <Button type="submit" disabled={isCreating || !newTeamName.trim()}>
                    {isCreating && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                    {t("list.dialog.create")}
                  </Button>
                </div>
              </form>
            </DialogContent>
          </Dialog>
        </div>

        {isLoading && teams.length === 0 ? (
          <div className="flex items-center justify-center py-12">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
          </div>
        ) : teams.length === 0 ? (
          <EmptyState
            icon={<Users />}
            title={t("list.empty.title")}
            description={t("list.empty.description")}
            action={
              <Button onClick={() => setIsCreateOpen(true)}>
                <Plus className="mr-2 h-4 w-4" />
                {t("list.createTeam")}
              </Button>
            }
          />
        ) : (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {teams.map((team) => (
              <Link key={team.id} to={`/team/${team.slug}`}>
                <Card className="transition-colors hover:bg-muted/50">
                  <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
                      <Users className="h-5 w-5 text-primary" />
                    </div>
                    <ChevronRight className="h-5 w-5 text-muted-foreground" />
                  </CardHeader>
                  <CardContent>
                    <CardTitle className="text-lg">{team.name}</CardTitle>
                    <CardDescription className="mt-1">
                      {t("list.created", { date: new Date(team.created_at).toLocaleDateString() })}
                    </CardDescription>
                  </CardContent>
                </Card>
              </Link>
            ))}
          </div>
        )}
      </div>
    </AppLayout>
  )
}
