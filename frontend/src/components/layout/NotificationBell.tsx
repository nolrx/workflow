/**
 * Notification bell + dropdown center — fully generic.
 *
 * The bell knows nothing about any concrete notification type. For each notice it
 * looks up a descriptor in the type registry ({@link getNotifDescriptor}) and
 * renders icon / severity colour / title / body / inline actions / click-through
 * link from there. Adding a new notification kind never touches this file.
 */
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { useNavigate } from "react-router-dom"
import { Bell, Loader2 } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"
import type { AppNotification } from "@/api/notifications"
import { useNotificationStore } from "@/stores/notificationStore"
import {
  LEVEL_ICON_CLASS,
  getNotifDescriptor,
  resolveLevel,
  type NotifContext,
} from "./notificationTypes"

export function NotificationBell() {
  const { t } = useTranslation("notifications")
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  // Captured when the panel opens (event handler — impure calls are fine there) so
  // relative timestamps stay pure during render.
  const [nowTs, setNowTs] = useState(0)

  const notifications = useNotificationStore((s) => s.notifications)
  const unreadCount = useNotificationStore((s) => s.unreadCount)
  const isLoading = useNotificationStore((s) => s.isLoading)
  const fetchNotifications = useNotificationStore((s) => s.fetchNotifications)
  const markRead = useNotificationStore((s) => s.markRead)
  const markAllRead = useNotificationStore((s) => s.markAllRead)
  const connect = useNotificationStore((s) => s.connect)
  const disconnect = useNotificationStore((s) => s.disconnect)

  const timeAgo = (iso: string | null): string => {
    if (!iso) return ""
    const then = new Date(iso).getTime()
    if (Number.isNaN(then)) return ""
    const min = Math.floor(((nowTs || then) - then) / 60000)
    if (min < 1) return t("time.justNow")
    if (min < 60) return t("time.minutesAgo", { count: min })
    const hr = Math.floor(min / 60)
    if (hr < 24) return t("time.hoursAgo", { count: hr })
    const day = Math.floor(hr / 24)
    if (day < 7) return t("time.daysAgo", { count: day })
    return new Date(iso).toLocaleDateString()
  }

  // Real-time delivery — module-guarded inside the store so remounts don't stack
  // streams/timers. SSE while visible + a slow fallback poll.
  useEffect(() => {
    connect()
    return () => disconnect()
  }, [connect, disconnect])

  const handleOpenChange = (next: boolean) => {
    setOpen(next)
    if (next) {
      setNowTs(Date.now())
      void fetchNotifications()
    }
  }

  const makeCtx = (n: AppNotification): NotifContext => ({
    t,
    navigate,
    reload: fetchNotifications,
    setBusy: (busy) => setBusyId(busy ? n.id : null),
    close: () => setOpen(false),
  })

  // Whole-row click: mark read, then follow the descriptor's link if any.
  const handleRowClick = (n: AppNotification, link: string | null) => {
    if (!n.is_read) void markRead(n.id)
    if (link) {
      setOpen(false)
      navigate(link)
    }
  }

  return (
    <DropdownMenu open={open} onOpenChange={handleOpenChange}>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" className="relative" aria-label={t("title")}>
          <Bell className="h-4 w-4" />
          {unreadCount > 0 && (
            <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-medium leading-none text-destructive-foreground">
              {unreadCount > 9 ? "9+" : unreadCount}
            </span>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80 p-0">
        <div className="flex items-center justify-between border-b px-3 py-2">
          <span className="text-sm font-semibold">{t("title")}</span>
          {unreadCount > 0 && (
            <button
              type="button"
              className="text-xs text-muted-foreground transition-colors hover:text-foreground"
              onClick={() => void markAllRead()}
            >
              {t("markAllRead")}
            </button>
          )}
        </div>

        <ScrollArea className="max-h-96">
          {isLoading && notifications.length === 0 ? (
            <div className="flex items-center justify-center py-10 text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
            </div>
          ) : notifications.length === 0 ? (
            <div className="px-3 py-10 text-center text-sm text-muted-foreground">
              {t("empty")}
            </div>
          ) : (
            <ul className="divide-y">
              {notifications.map((n) => {
                const descriptor = getNotifDescriptor(n.type)
                const level = resolveLevel(descriptor, n)
                const Icon = descriptor.icon
                const title = descriptor.title(n, t)
                const body = descriptor.body(n, t)
                const actions = descriptor.actions?.(n) ?? []
                const link = actions.length === 0 ? descriptor.link?.(n) ?? null : null
                const clickable = actions.length === 0 && (link !== null || !n.is_read)

                return (
                  <li
                    key={n.id}
                    className={cn(
                      "flex gap-2.5 px-3 py-2.5",
                      !n.is_read && "bg-muted/40",
                      clickable && "cursor-pointer hover:bg-muted/60"
                    )}
                    onClick={
                      clickable ? () => handleRowClick(n, link) : undefined
                    }
                  >
                    <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", LEVEL_ICON_CLASS[level])} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <p className="flex-1 truncate text-sm font-medium leading-snug">
                          {title}
                        </p>
                        {!n.is_read && (
                          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
                        )}
                      </div>
                      {body && (
                        <p className="mt-0.5 text-xs leading-snug text-muted-foreground">
                          {body}
                        </p>
                      )}
                      <p className="mt-1 text-[11px] text-muted-foreground/70">
                        {timeAgo(n.created_at)}
                      </p>

                      {actions.length > 0 && (
                        <div className="mt-1.5 flex gap-2">
                          {actions.map((action) => {
                            const ActionIcon = action.icon
                            return (
                              <Button
                                key={action.key}
                                size="sm"
                                variant={action.variant ?? "default"}
                                className="h-7 px-2.5 text-xs"
                                disabled={busyId === n.id}
                                onClick={(e) => {
                                  e.stopPropagation()
                                  void action.run(n, makeCtx(n))
                                }}
                              >
                                {busyId === n.id && action.variant !== "outline" ? (
                                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                                ) : ActionIcon ? (
                                  <ActionIcon className="mr-1 h-3 w-3" />
                                ) : null}
                                {t(action.labelKey)}
                              </Button>
                            )
                          })}
                        </div>
                      )}
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </ScrollArea>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export default NotificationBell
