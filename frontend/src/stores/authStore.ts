/**
 * Authentication Store
 * Manages user authentication state, login/logout, and user profile
 */
import { create } from "zustand"
import { persist } from "zustand/middleware"
import { api, tokenManager } from "@/api/client"
import { t } from "@/i18n"

// Types
export interface User {
  id: string
  email: string
  display_name: string | null
  avatar_url: string | null
  role: "user" | "pro" | "admin"
  plan_id: string | null
  is_verified: boolean
  created_at: string
}

interface LoginRequest {
  email: string
  password: string
}

interface RegisterRequest {
  email: string
  password: string
  display_name?: string
}

interface AuthResponse {
  access_token: string
  refresh_token: string
  user: User
}

interface AuthState {
  user: User | null
  isAuthenticated: boolean
  isLoading: boolean
  error: string | null

  // Actions
  login: (credentials: LoginRequest) => Promise<void>
  register: (data: RegisterRequest) => Promise<void>
  logout: () => void
  fetchUser: () => Promise<void>
  updateProfile: (data: Partial<User>) => Promise<void>
  clearError: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      user: null,
      isAuthenticated: false,
      isLoading: false,
      error: null,

      login: async (credentials) => {
        set({ isLoading: true, error: null })
        try {
          const response = await api.post<AuthResponse>(
            "/auth/login",
            credentials
          )
          tokenManager.setTokens(
            response.access_token,
            response.refresh_token
          )
          set({
            user: response.user,
            isAuthenticated: true,
            isLoading: false,
          })
        } catch (error: unknown) {
          const message =
            (error as { response?: { data?: { error?: string } } })?.response
              ?.data?.error || t('errors:auth.loginFailed')
          set({ error: message, isLoading: false })
          throw error
        }
      },

      register: async (data) => {
        set({ isLoading: true, error: null })
        try {
          const response = await api.post<AuthResponse>("/auth/register", data)
          tokenManager.setTokens(
            response.access_token,
            response.refresh_token
          )
          set({
            user: response.user,
            isAuthenticated: true,
            isLoading: false,
          })
        } catch (error: unknown) {
          const message =
            (error as { response?: { data?: { error?: string } } })?.response
              ?.data?.error || t('errors:auth.registrationFailed')
          set({ error: message, isLoading: false })
          throw error
        }
      },

      logout: () => {
        tokenManager.clearTokens()
        set({
          user: null,
          isAuthenticated: false,
          error: null,
        })
      },

      fetchUser: async () => {
        const token = tokenManager.getAccessToken()
        if (!token) {
          set({ isAuthenticated: false, user: null })
          return
        }

        set({ isLoading: true })
        try {
          const response = await api.get<{ user: User }>("/auth/me")
          set({
            user: response.user,
            isAuthenticated: true,
            isLoading: false,
          })
        } catch {
          tokenManager.clearTokens()
          set({
            user: null,
            isAuthenticated: false,
            isLoading: false,
          })
        }
      },

      updateProfile: async (data) => {
        set({ isLoading: true, error: null })
        try {
          const response = await api.put<{ user: User }>("/users/profile", data)
          set({
            user: response.user,
            isLoading: false,
          })
        } catch (error: unknown) {
          const message =
            (error as { response?: { data?: { error?: string } } })?.response
              ?.data?.error || t('errors:auth.updateProfileFailed')
          set({ error: message, isLoading: false })
          throw error
        }
      },

      clearError: () => set({ error: null }),
    }),
    {
      name: "auth-store",
      partialize: (state) => ({
        user: state.user,
        isAuthenticated: state.isAuthenticated,
      }),
    }
  )
)
