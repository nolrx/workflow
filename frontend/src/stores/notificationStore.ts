/**
 * Notification store — real-time feed + unread badge.
 *
 * Delivery is push-based over SSE (fetch + ReadableStream, so the bearer token
 * rides the Authorization header — same transport as the agent run stream). New
 * notices arrive live and prepend instantly. Robustness:
 *   - The stream is only held while the tab is VISIBLE (Page Visibility): a
 *     backgrounded tab releases its backend worker thread, so always-on streams
 *     don't starve the single-worker gthread pool. It reconnects on focus.
 *   - A slow fallback poll (60s) keeps the badge fresh when SSE can't connect
 *     (thread saturation / proxy), and on reconnect the on-open list fetch fills
 *     anything missed — the DB row is always the source of truth.
 */
import { create } from "zustand"

import { tokenManager } from "@/api/client"
import { notificationsApi, type AppNotification } from "@/api/notifications"

const API_BASE = import.meta.env.VITE_API_URL || "/api"
const FALLBACK_POLL_MS = 60000
const MAX_RETRY = 6

let streamAbort: AbortController | null = null
let fallbackTimer: ReturnType<typeof setInterval> | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let visibilityHandler: (() => void) | null = null
let retry = 0
let started = false

interface NotificationState {
  notifications: AppNotification[]
  unreadCount: number
  isLoading: boolean

  fetchNotifications: () => Promise<void>
  fetchUnreadCount: () => Promise<void>
  pushNotification: (n: AppNotification) => void
  markRead: (id: string) => Promise<void>
  markAllRead: () => Promise<void>
  /** Begin real-time delivery (idempotent): SSE while visible + fallback poll. */
  connect: () => void
  /** Tear down the stream, fallback poll and visibility listener. */
  disconnect: () => void
}

function closeStream() {
  streamAbort?.abort()
  streamAbort = null
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
}

function scheduleReconnect() {
  if (!started || reconnectTimer) return
  if (typeof document !== "undefined" && document.visibilityState !== "visible") return
  retry = Math.min(retry + 1, MAX_RETRY)
  const delay = Math.min(1000 * retry, 5000)
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    if (started && document.visibilityState === "visible") void openStream()
  }, delay)
}

async function openStream() {
  closeStream()
  const token = tokenManager.getAccessToken()
  if (!token || !started) return
  streamAbort = new AbortController()
  try {
    const res = await fetch(`${API_BASE}/notifications/stream`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: streamAbort.signal,
    })
    if (!res.ok || !res.body) {
      scheduleReconnect()
      return
    }
    retry = 0
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ""
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.indexOf("\n\n")
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        boundary = buffer.indexOf("\n\n")

        let eventType = "message"
        let dataStr = ""
        for (const line of block.split("\n")) {
          if (line.startsWith(":")) continue // keepalive comment
          if (line.startsWith("event:")) eventType = line.slice(6).trim()
          else if (line.startsWith("data:")) dataStr += line.slice(5).trim()
        }
        if (!dataStr) continue
        try {
          const data = JSON.parse(dataStr)
          if (eventType === "ready" && typeof data.unread_count === "number") {
            useNotificationStore.setState({ unreadCount: data.unread_count })
          } else if (eventType === "notification") {
            useNotificationStore.getState().pushNotification(data as AppNotification)
          }
        } catch {
          // Ignore a malformed frame; the next fetch reconciles.
        }
      }
    }
    // Server closed the stream — reconnect if we're still active & visible.
    scheduleReconnect()
  } catch (err) {
    if ((err as Error)?.name === "AbortError") return
    scheduleReconnect()
  }
}

export const useNotificationStore = create<NotificationState>()((set, get) => ({
  notifications: [],
  unreadCount: 0,
  isLoading: false,

  fetchNotifications: async () => {
    set({ isLoading: true })
    try {
      const result = await notificationsApi.list({ limit: 30 })
      set({
        notifications: result.notifications,
        unreadCount: result.unread_count,
        isLoading: false,
      })
    } catch {
      set({ isLoading: false })
    }
  },

  fetchUnreadCount: async () => {
    try {
      set({ unreadCount: await notificationsApi.unreadCount() })
    } catch {
      /* badge poll is best-effort */
    }
  },

  pushNotification: (n) =>
    set((state) => {
      if (state.notifications.some((x) => x.id === n.id)) return {}
      return {
        notifications: [n, ...state.notifications].slice(0, 50),
        unreadCount: n.is_read ? state.unreadCount : state.unreadCount + 1,
      }
    }),

  markRead: async (id) => {
    try {
      const unread = await notificationsApi.markRead(id)
      set((state) => ({
        unreadCount: unread,
        notifications: state.notifications.map((n) =>
          n.id === id ? { ...n, is_read: true } : n
        ),
      }))
    } catch {
      /* ignore */
    }
  },

  markAllRead: async () => {
    try {
      await notificationsApi.markAllRead()
      set((state) => ({
        unreadCount: 0,
        notifications: state.notifications.map((n) => ({ ...n, is_read: true })),
      }))
    } catch {
      /* ignore */
    }
  },

  connect: () => {
    if (started) return
    started = true
    void get().fetchUnreadCount()
    fallbackTimer = setInterval(() => {
      void useNotificationStore.getState().fetchUnreadCount()
    }, FALLBACK_POLL_MS)

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        retry = 0
        void openStream()
      } else {
        closeStream()
      }
    }
    visibilityHandler = onVisibility
    document.addEventListener("visibilitychange", onVisibility)
    if (typeof document === "undefined" || document.visibilityState === "visible") {
      void openStream()
    }
  },

  disconnect: () => {
    started = false
    closeStream()
    if (fallbackTimer) {
      clearInterval(fallbackTimer)
      fallbackTimer = null
    }
    if (visibilityHandler) {
      document.removeEventListener("visibilitychange", visibilityHandler)
      visibilityHandler = null
    }
  },
}))
