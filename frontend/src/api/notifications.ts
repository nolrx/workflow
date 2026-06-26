/**
 * In-app notification API client.
 *
 * The feed of notices addressed to the current user (team invites, accept/reject
 * acknowledgements, ...). Uses the unified ``{success, data, message}`` envelope.
 */
import { api } from "@/api/client"

interface Envelope<T> {
  success: boolean
  data: T
  message?: string
}

export type NotificationType =
  | "team_invite"
  | "team_invite_accepted"
  | "team_invite_rejected"
  | "code_deploy_succeeded"
  | "run_failed"
  | string

/** Generic severity driving the dot/icon colour. Mirrors the backend levels. */
export type NotificationLevel = "info" | "success" | "warning" | "error"

export interface AppNotification {
  id: string
  type: NotificationType
  level: NotificationLevel | string | null
  title: string | null
  body: string | null
  data: Record<string, unknown>
  ref_type: string | null
  ref_id: string | null
  is_read: boolean
  read_at: string | null
  created_at: string | null
}

export interface NotificationListResult {
  notifications: AppNotification[]
  total: number
  unread_count: number
  limit: number
  offset: number
}

export interface NotificationListParams {
  unread?: boolean
  limit?: number
  offset?: number
}

export const notificationsApi = {
  list: async (params?: NotificationListParams): Promise<NotificationListResult> => {
    const q = new URLSearchParams()
    if (params?.unread) q.set("unread", "1")
    if (params?.limit != null) q.set("limit", String(params.limit))
    if (params?.offset != null) q.set("offset", String(params.offset))
    const res = await api.get<Envelope<NotificationListResult>>(
      `/notifications?${q.toString()}`
    )
    return res.data
  },

  unreadCount: async (): Promise<number> => {
    const res = await api.get<Envelope<{ count: number }>>("/notifications/unread-count")
    return res.data.count
  },

  markRead: async (id: string): Promise<number> => {
    const res = await api.post<Envelope<{ unread_count: number }>>(
      `/notifications/${id}/read`
    )
    return res.data.unread_count
  },

  markAllRead: async (): Promise<void> => {
    await api.post<Envelope<unknown>>("/notifications/read-all")
  },
}
