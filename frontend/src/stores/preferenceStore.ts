/**
 * User preference store
 * Persists UI-level preferences such as theme style.
 */
import { create } from "zustand"
import { persist } from "zustand/middleware"

export type AppTheme =
  | "default"
  | "vscode"
  | "game-engine"
  | "synthwave84"
  | "mint"
  | "paper"
  | "ocean"
  | "lavender"

interface PreferenceState {
  theme: AppTheme
  setTheme: (theme: AppTheme) => void
}

export const usePreferenceStore = create<PreferenceState>()(
  persist(
    (set) => ({
      theme: "default",
      setTheme: (theme) => set({ theme }),
    }),
    {
      name: "preference-store",
      partialize: (state) => ({ theme: state.theme }),
    }
  )
)
