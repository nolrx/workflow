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
    // Viewport-locked app shell: the shell is exactly one (dynamic) viewport tall
    // and never scrolls, so <main> owns the only scrollbar.
    <div className="h-dvh overflow-hidden bg-background">
      {/* Desktop sidebar */}
      <div className="hidden lg:block">
        <Sidebar />
      </div>

      <div className="flex h-full flex-col pl-0 lg:pl-64">
        <Header title={title} onMenuClick={() => setMobileOpen(true)} />
        <main className="min-h-0 flex-1 overflow-y-auto p-4 lg:p-6">
          {children}
        </main>
      </div>

      {/* Mobile sidebar drawer */}
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="w-64 p-0 sm:max-w-[16rem]">
          <div className="flex h-full flex-col">
            <SidebarContent onNavigate={() => setMobileOpen(false)} />
          </div>
        </SheetContent>
      </Sheet>
    </div>
  )
}
