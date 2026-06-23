import type { ReactNode } from "react"
import { Sidebar } from "./Sidebar"
import { Header } from "./Header"

interface AppLayoutProps {
  children: ReactNode
  title?: string
}

export function AppLayout({ children, title }: AppLayoutProps) {
  return (
    // Viewport-locked app shell: the shell is exactly one (dynamic) viewport tall
    // and never scrolls, so <main> owns the only scrollbar. This avoids the
    // double-scrollbar that a `min-h-screen` body + an inner `100vh`-math panel
    // produced (sub-pixel/overflow made the body marginally taller than the
    // viewport). dvh keeps the bottom reachable on mobile where vh over-reports.
    <div className="h-dvh overflow-hidden bg-background">
      <Sidebar />
      <div className="flex h-full flex-col pl-64">
        <Header title={title} />
        <main className="min-h-0 flex-1 overflow-y-auto p-6">{children}</main>
      </div>
    </div>
  )
}
