/**
 * Notification type registry — the single place that teaches the UI how to render
 * and act on each notification ``type``.
 *
 * The bell component is fully generic: it never references a concrete business
 * type. To add a new notification kind, register one {@link NotifDescriptor} here
 * (icon, severity, title/body text, optional click-through link, optional inline
 * actions) and emit it from the backend with the matching ``type``. Unknown types
 * fall back to the backend-provided ``title``/``body`` + ``data.link``, so a new
 * producer works even before a descriptor is added.
 */
import type { ComponentType } from "react"
import type { TFunction } from "i18next"
import {
  AlertTriangle,
  Bell,
  Check,
  Rocket,
  UserMinus,
  UserPlus,
} from "lucide-react"
import { toast } from "sonner"

import type { AppNotification, NotificationLevel } from "@/api/notifications"
import { useTeamStore } from "@/stores/teamStore"

type IconComponent = ComponentType<{ className?: string }>

/** Side-channel the bell hands to an action so it can navigate / refresh / busy. */
export interface NotifContext {
  t: TFunction
  navigate: (to: string) => void
  reload: () => Promise<void>
  setBusy: (busy: boolean) => void
  close: () => void
}

export interface NotifAction {
  key: string
  /** i18n key under the ``notifications`` namespace, e.g. ``actions.accept``. */
  labelKey: string
  variant?: "default" | "outline"
  icon?: IconComponent
  run: (n: AppNotification, ctx: NotifContext) => Promise<void> | void
}

export interface NotifDescriptor {
  icon: IconComponent
  level: NotificationLevel | ((n: AppNotification) => NotificationLevel)
  title: (n: AppNotification, t: TFunction) => string
  body: (n: AppNotification, t: TFunction) => string
  /** A frontend route to open on click (whole-row), when there are no actions. */
  link?: (n: AppNotification) => string | null
  /** Inline action buttons (e.g. accept / decline). Return [] when not actionable. */
  actions?: (n: AppNotification) => NotifAction[]
}

const str = (v: unknown): string => (v == null ? "" : String(v))

/** Generic "open this route" link carried by any notice in ``data.link``. */
const linkFromData = (n: AppNotification): string | null => {
  const l = n.data?.link
  return typeof l === "string" && l ? l : null
}

// --- Shared actions ----------------------------------------------------------
const acceptInviteAction: NotifAction = {
  key: "accept",
  labelKey: "actions.accept",
  variant: "default",
  icon: Check,
  run: async (n, ctx) => {
    const token = str(n.data?.token)
    if (!token) return
    ctx.setBusy(true)
    try {
      await useTeamStore.getState().acceptInvitation(token)
      toast.success(ctx.t("toast.accepted", { team: str(n.data?.team_name) }))
      await ctx.reload()
    } catch {
      toast.error(ctx.t("toast.failed"))
    } finally {
      ctx.setBusy(false)
    }
  },
}

const declineInviteAction: NotifAction = {
  key: "decline",
  labelKey: "actions.decline",
  variant: "outline",
  icon: UserMinus,
  run: async (n, ctx) => {
    const token = str(n.data?.token)
    if (!token) return
    ctx.setBusy(true)
    try {
      await useTeamStore.getState().rejectInvitation(token)
      toast.success(ctx.t("toast.declined"))
      await ctx.reload()
    } catch {
      toast.error(ctx.t("toast.failed"))
    } finally {
      ctx.setBusy(false)
    }
  },
}

// --- Registry ----------------------------------------------------------------
const REGISTRY: Record<string, NotifDescriptor> = {
  team_invite: {
    icon: UserPlus,
    level: "info",
    title: (_n, t) => t("types.team_invite.title"),
    body: (n, t) =>
      n.data?.inviter_name
        ? t("types.team_invite.message", {
            inviter: str(n.data.inviter_name),
            team: str(n.data.team_name),
          })
        : t("types.team_invite.messageNoInviter", { team: str(n.data?.team_name) }),
    actions: (n) =>
      n.is_read || !n.data?.token ? [] : [acceptInviteAction, declineInviteAction],
  },
  team_invite_accepted: {
    icon: Check,
    level: "success",
    title: (_n, t) => t("types.team_invite_accepted.title"),
    body: (n, t) =>
      t("types.team_invite_accepted.message", {
        user: str(n.data?.user_name),
        team: str(n.data?.team_name),
      }),
  },
  team_invite_rejected: {
    icon: UserMinus,
    level: "warning",
    title: (_n, t) => t("types.team_invite_rejected.title"),
    body: (n, t) =>
      t("types.team_invite_rejected.message", {
        user: str(n.data?.user_name),
        team: str(n.data?.team_name),
      }),
  },
  code_deploy_succeeded: {
    icon: Rocket,
    level: "success",
    title: (_n, t) => t("types.code_deploy_succeeded.title"),
    body: (_n, t) => t("types.code_deploy_succeeded.message"),
    link: linkFromData,
  },
  run_failed: {
    icon: AlertTriangle,
    level: "error",
    title: (_n, t) => t("types.run_failed.title"),
    body: (_n, t) => t("types.run_failed.message"),
    link: linkFromData,
  },
}

const VALID_LEVELS = new Set<NotificationLevel>(["info", "success", "warning", "error"])

/** Fallback descriptor for any type without a registered renderer. */
const DEFAULT_DESCRIPTOR: NotifDescriptor = {
  icon: Bell,
  level: (n) =>
    VALID_LEVELS.has(str(n.level) as NotificationLevel)
      ? (n.level as NotificationLevel)
      : "info",
  title: (n) => n.title ?? "",
  body: (n) => n.body ?? "",
  link: linkFromData,
}

export function getNotifDescriptor(type: string): NotifDescriptor {
  return REGISTRY[type] ?? DEFAULT_DESCRIPTOR
}

export function resolveLevel(d: NotifDescriptor, n: AppNotification): NotificationLevel {
  return typeof d.level === "function" ? d.level(n) : d.level
}

/** Tailwind colour for the leading type icon, by severity. */
export const LEVEL_ICON_CLASS: Record<NotificationLevel, string> = {
  info: "text-sky-500",
  success: "text-emerald-500",
  warning: "text-amber-500",
  error: "text-red-500",
}
