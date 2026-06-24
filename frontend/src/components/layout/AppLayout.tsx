import { useState, type ReactNode } from "react"
import { Sidebar, SidebarContent } from "./Sidebar"
import { Header } from "./Header"
import { Sheet, SheetContent } from "@/components/ui/sheet"

interface AppLayoutProps {
  children: ReactNode
  title?: string
}

export function AppLayout({ children, title }: AppLayoutProps) {
  const [mobileOpen, setMobileOpen] = useState(false)

  return (
    // Viewport-locked app shell: the global header sits at the top and spans the
    // full viewport width. The desktop sidebar and main area sit side-by-side
    // below it with a visible gap between them.
    <div className="flex h-dvh flex-col overflow-hidden bg-background">
      <Header title={title} onMenuClick={() => setMobileOpen(true)} />

      <div className="flex h-[calc(100dvh-4rem)] w-full gap-4 lg:gap-6">
        <Sidebar />

        <main className="min-h-0 flex-1 overflow-y-auto p-4 lg:p-6">
          {children}
        </main>
      </div>

      {/* Mobile sidebar drawer */}
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent
          side="left"
          className="mobile-sidebar-sheet w-64 rounded-none p-0 sm:max-w-[16rem]"
        >
          {/* pt-12 reserves space for the Sheet's built-in close button. */}
          <div className="mobile-sidebar-content flex h-full flex-col pt-12">
            <SidebarContent onNavigate={() => setMobileOpen(false)} />
          </div>
        </SheetContent>
      </Sheet>
    </div>
  )
}
