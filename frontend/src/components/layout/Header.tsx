import { Link } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { Bell, Menu, User } from "lucide-react"
import { Button } from "@/components/ui/button"

interface HeaderProps {
  title?: string
  onMenuClick?: () => void
}

export function Header({ title, onMenuClick }: HeaderProps) {
  const { t } = useTranslation("common")
  return (
    <header className="z-40 flex h-16 shrink-0 items-center justify-between border-b bg-background/95 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60 lg:px-6">
      <div className="flex items-center gap-3">
        <Button
          variant="ghost"
          size="icon"
          className="lg:hidden"
          aria-label={t("a11y.openMenu")}
          onClick={onMenuClick}
        >
          <Menu className="h-5 w-5" />
        </Button>

        <Link to="/" className="flex items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <span className="text-sm font-bold">{t("brand.name")}</span>
          </div>
          <span className="font-semibold">{t("brand.subtitle")}</span>
        </Link>

        {title && (
          <>
            <div className="hidden h-6 w-px bg-border lg:block" />
            <h1 className="hidden text-lg font-semibold lg:block">{title}</h1>
          </>
        )}
      </div>

      <div className="flex items-center gap-2">
        <Button variant="ghost" size="icon" aria-label={t("a11y.notifications")}>
          <Bell className="h-4 w-4" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="rounded-full"
          aria-label={t("a11y.profile")}
        >
          <User className="h-4 w-4" />
        </Button>
      </div>
    </header>
  )
}
