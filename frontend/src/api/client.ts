/**
 * API Client with Axios
 * Handles authentication, request/response interceptors, and token refresh
 */
import axios, {
  type AxiosError,
  type AxiosRequestConfig,
  type InternalAxiosRequestConfig,
} from "axios"

const API_BASE_URL = import.meta.env.VITE_API_URL || "/api"

// Create axios instance
export const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    "Content-Type": "application/json",
  },
  timeout: 30000,
})

// Token storage keys
const ACCESS_TOKEN_KEY = "access_token"
const REFRESH_TOKEN_KEY = "refresh_token"

// Token management
export const tokenManager = {
  getAccessToken: () => localStorage.getItem(ACCESS_TOKEN_KEY),
  getRefreshToken: () => localStorage.getItem(REFRESH_TOKEN_KEY),
  setTokens: (access: string, refresh: string) => {
    localStorage.setItem(ACCESS_TOKEN_KEY, access)
    localStorage.setItem(REFRESH_TOKEN_KEY, refresh)
  },
  clearTokens: () => {
    localStorage.removeItem(ACCESS_TOKEN_KEY)
    localStorage.removeItem(REFRESH_TOKEN_KEY)
  },
}

// Request interceptor - add auth token
apiClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const token = tokenManager.getAccessToken()
    if (token && config.headers) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error)
)

// Bounded retries for TRANSIENT infrastructure failures (e.g. the backend briefly
// unreachable during a graceful redeploy). Only idempotent GETs are retried; a
// deliberate "draining" 503 is surfaced immediately so the UI can tell the user
// the platform is deploying.
const MAX_TRANSIENT_RETRIES = 6

// Response interceptor - handle token refresh
let isRefreshing = false
let failedQueue: Array<{
  resolve: (value?: unknown) => void
  reject: (reason?: unknown) => void
}> = []

const processQueue = (error: Error | null, token: string | null = null) => {
  failedQueue.forEach((prom) => {
    if (error) {
      prom.reject(error)
    } else {
      prom.resolve(token)
    }
  })
  failedQueue = []
}

apiClient.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as InternalAxiosRequestConfig & {
      _retry?: boolean
    }

    // If 401 and we haven't already retried
    if (error.response?.status === 401 && !originalRequest._retry) {
      if (isRefreshing) {
        // Wait for the refresh to complete
        return new Promise((resolve, reject) => {
          failedQueue.push({ resolve, reject })
        }).then((token) => {
          if (originalRequest.headers) {
            originalRequest.headers.Authorization = `Bearer ${token}`
          }
          return apiClient(originalRequest)
        })
      }

      originalRequest._retry = true
      isRefreshing = true

      const refreshToken = tokenManager.getRefreshToken()
      if (!refreshToken) {
        tokenManager.clearTokens()
        window.location.href = "/login"
        return Promise.reject(error)
      }

      try {
        const response = await axios.post(`${API_BASE_URL}/auth/refresh`, {
          refresh_token: refreshToken,
        })

        const { access_token, refresh_token: newRefresh } = response.data
        tokenManager.setTokens(access_token, newRefresh || refreshToken)

        processQueue(null, access_token)

        if (originalRequest.headers) {
          originalRequest.headers.Authorization = `Bearer ${access_token}`
        }
        return apiClient(originalRequest)
      } catch (refreshError) {
        processQueue(refreshError as Error, null)
        tokenManager.clearTokens()
        window.location.href = "/login"
        return Promise.reject(refreshError)
      } finally {
        isRefreshing = false
      }
    }

    // Transient infrastructure failure (no response, or 502/503/504 — e.g. the
    // backend swapping during a graceful redeploy): retry idempotent GETs a few
    // times with backoff so a click in the deploy window rides through instead of
    // hard-failing. A deliberate "draining" 503 is NOT retried — surface it so the
    // UI can show "发布中，请稍后重试".
    const cfg = error.config as
      | (InternalAxiosRequestConfig & { _retryCount?: number })
      | undefined
    const status = error.response?.status
    const isDraining =
      status === 503 && (error.response?.data as ApiError | undefined)?.error === "DRAINING"
    const transient = !error.response || status === 502 || status === 503 || status === 504
    const idempotent = (cfg?.method ?? "get").toLowerCase() === "get"
    if (cfg && idempotent && transient && !isDraining) {
      const attempt = (cfg._retryCount ?? 0) + 1
      if (attempt <= MAX_TRANSIENT_RETRIES) {
        cfg._retryCount = attempt
        await new Promise((r) => setTimeout(r, Math.min(400 * attempt, 2000)))
        return apiClient(cfg)
      }
    }

    return Promise.reject(error)
  }
)

// API response types
export interface ApiResponse<T> {
  data: T
  message?: string
}

export interface ApiError {
  error: string
  message?: string
}

// Generic API request helpers. An optional per-request `config` lets callers
// override defaults such as `timeout` for slow AI-generation endpoints, without
// raising the global timeout for ordinary CRUD calls.
export const api = {
  get: <T>(url: string, config?: AxiosRequestConfig) =>
    apiClient.get<T>(url, config).then((res) => res.data),
  post: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    apiClient.post<T>(url, data, config).then((res) => res.data),
  put: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    apiClient.put<T>(url, data, config).then((res) => res.data),
  patch: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    apiClient.patch<T>(url, data, config).then((res) => res.data),
  delete: <T>(url: string, config?: AxiosRequestConfig) =>
    apiClient.delete<T>(url, config).then((res) => res.data),
}

export default apiClient
