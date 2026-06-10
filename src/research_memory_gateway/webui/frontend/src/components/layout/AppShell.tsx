import { useState, useCallback } from 'react'
import { Outlet } from '@tanstack/react-router'
import { Sidebar, MobileSidebarContent } from './Sidebar'
import { Topbar } from './Topbar'
import { CommandPalette } from './CommandPalette'
import { Toaster } from '@/components/ui/sonner'
import { Sheet, SheetContent } from '@/components/ui/sheet'

export function AppShell() {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)
  const [commandOpen, setCommandOpen] = useState(false)

  const handleToggleSidebar = useCallback(() => {
    setSidebarCollapsed((prev) => !prev)
  }, [])

  const handleMenuClick = useCallback(() => {
    setMobileOpen(true)
  }, [])

  const handleSearchClick = useCallback(() => {
    setCommandOpen(true)
  }, [])

  return (
    <div className="flex h-screen w-full bg-background text-foreground overflow-hidden">
      {/* Desktop Sidebar */}
      <Sidebar collapsed={sidebarCollapsed} onToggle={handleToggleSidebar} />

      {/* Mobile Sidebar Sheet */}
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="p-0 w-72">
          <MobileSidebarContent onClose={() => setMobileOpen(false)} />
        </SheetContent>
      </Sheet>

      {/* Main content area */}
      <div className="flex-1 flex flex-col overflow-hidden min-w-0">
        <Topbar onMenuClick={handleMenuClick} onSearchClick={handleSearchClick} />
        <main className="flex-1 overflow-auto bg-muted/10">
          <div className="page-enter">
            <Outlet />
          </div>
        </main>
      </div>

      {/* Global overlays */}
      <CommandPalette open={commandOpen} onOpenChange={setCommandOpen} />
      <Toaster />
    </div>
  )
}
