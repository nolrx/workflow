import { useLayoutEffect, type ReactNode } from "react"
import { usePreferenceStore } from "@/stores/preferenceStore"

interface ThemeProviderProps {
  children: ReactNode
}

/**
 * Applies the selected theme to the document root before paint so there is no
 * flash, and re-applies whenever the user changes the theme.
 */
export function ThemeProvider({ children }: ThemeProviderProps) {
  const theme = usePreferenceStore((s) => s.theme)

  useLayoutEffect(() => {
    const root = document.documentElement
    if (theme === "default") {
      root.removeAttribute("data-theme")
    } else {
      root.setAttribute("data-theme", theme)
    }
  }, [theme])

  return <>{children}</>
}
