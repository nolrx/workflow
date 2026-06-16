import { useState } from "react"
import { Loader2, User } from "lucide-react"
import { useTranslation } from "react-i18next"
import { AppLayout } from "@/components/layout/AppLayout"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { useAuthStore } from "@/stores/authStore"
import { toast } from "sonner"

export function Profile() {
  const { t } = useTranslation("settings")
  const { user, updateProfile, isLoading, error } = useAuthStore()
  const [displayName, setDisplayName] = useState(user?.display_name || "")
  const [avatarUrl, setAvatarUrl] = useState(user?.avatar_url || "")
  const [isSaving, setIsSaving] = useState(false)

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    setIsSaving(true)
    try {
      await updateProfile({
        display_name: displayName || null,
        avatar_url: avatarUrl || null,
      })
      toast.success(t("profile.toast.success"))
    } catch {
      toast.error(error || t("profile.toast.failed"))
    } finally {
      setIsSaving(false)
    }
  }

  return (
    <AppLayout title={t("profile.title")}>
      <div className="space-y-6 max-w-2xl">
        <div>
          <h1 className="text-2xl font-semibold">{t("profile.title")}</h1>
          <p className="text-muted-foreground">
            {t("profile.subtitle")}
          </p>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>{t("profile.personalInfo.title")}</CardTitle>
            <CardDescription>
              {t("profile.personalInfo.description")}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleSave} className="space-y-4">
              <div className="flex items-center gap-4">
                <div className="flex h-16 w-16 items-center justify-center rounded-full bg-muted">
                  {avatarUrl ? (
                    <img
                      src={avatarUrl}
                      alt="Avatar"
                      className="h-full w-full rounded-full object-cover"
                    />
                  ) : (
                    <User className="h-8 w-8 text-muted-foreground" />
                  )}
                </div>
                <div className="flex-1">
                  <Label htmlFor="avatarUrl">{t("profile.personalInfo.avatarLabel")}</Label>
                  <Input
                    id="avatarUrl"
                    placeholder={t("profile.personalInfo.avatarPlaceholder")}
                    value={avatarUrl}
                    onChange={(e) => setAvatarUrl(e.target.value)}
                    disabled={isSaving}
                  />
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="displayName">{t("profile.personalInfo.displayNameLabel")}</Label>
                <Input
                  id="displayName"
                  placeholder={t("profile.personalInfo.displayNamePlaceholder")}
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  disabled={isSaving}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="email">{t("profile.personalInfo.emailLabel")}</Label>
                <Input
                  id="email"
                  type="email"
                  value={user?.email || ""}
                  disabled
                  className="bg-muted"
                />
                <p className="text-xs text-muted-foreground">
                  {t("profile.personalInfo.emailNote")}
                </p>
              </div>

              <div className="flex justify-end">
                <Button type="submit" disabled={isSaving || isLoading}>
                  {isSaving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  {t("profile.personalInfo.saveButton", "Save Changes")}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t("profile.account.title")}</CardTitle>
            <CardDescription>
              {t("profile.account.description")}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="font-medium">{t("profile.account.roleLabel")}</p>
                <p className="text-sm text-muted-foreground capitalize">
                  {user?.role || "user"}
                </p>
              </div>
            </div>

            <div className="flex items-center justify-between">
              <div>
                <p className="font-medium">{t("profile.account.emailVerified.label")}</p>
                <p className="text-sm text-muted-foreground">
                  {user?.is_verified ? t("profile.account.emailVerified.verified") : t("profile.account.emailVerified.notVerified")}
                </p>
              </div>
              {!user?.is_verified && (
                <Button variant="outline" size="sm">
                  {t("profile.account.emailVerified.verifyButton")}
                </Button>
              )}
            </div>

            <div className="flex items-center justify-between">
              <div>
                <p className="font-medium">{t("profile.account.changePassword.label")}</p>
                <p className="text-sm text-muted-foreground">
                  {t("profile.account.changePassword.description")}
                </p>
              </div>
              <Button variant="outline" size="sm">
                {t("profile.account.changePassword.button")}
              </Button>
            </div>

            <div className="border-t pt-4">
              <div className="flex items-center justify-between">
                <div>
                  <p className="font-medium text-destructive">{t("profile.account.deleteAccount.label")}</p>
                  <p className="text-sm text-muted-foreground">
                    {t("profile.account.deleteAccount.description")}
                  </p>
                </div>
                <Button variant="destructive" size="sm">
                  {t("profile.account.deleteAccount.button")}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    </AppLayout>
  )
}
