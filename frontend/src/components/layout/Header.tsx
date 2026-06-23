import { useTranslation } from "react-i18next"
import { Bell, User } from "lucide-react"
import { Button } from "@/components/ui/button"
import { LanguageSwitcher } from "@/components/common/LanguageSwitcher"

interface HeaderProps {
  title?: string
}

export function Header({ title }: HeaderProps) {
  const { t } = useTranslation("common")
  return (
    <header className="z-30 flex h-16 shrink-0 items-center justify-between border-b bg-background/95 px-6 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="flex items-center gap-4">
        {title && <h1 className="text-lg font-semibold">{title}</h1>}
      </div>

      <div className="flex items-center gap-2">
        <LanguageSwitcher />
        <Button variant="ghost" size="icon" aria-label={t("a11y.notifications")}>
          <Bell className="h-4 w-4" />
        </Button>
        <Button variant="ghost" size="icon" className="rounded-full" aria-label={t("a11y.profile")}>
          <User className="h-4 w-4" />
        </Button>
      </div>
    </header>
  )
}
