import { useState, useCallback } from 'react'
import { Outlet } from '@tanstack/react-router'
import { MobileSidebarContent } from './Sidebar'
import { Topbar } from './Topbar'
import { CommandPalette } from './CommandPalette'
import { Toaster } from '@/components/ui/sonner'
import { Sheet, SheetContent } from '@/components/ui/sheet'

export function AppShell() {
  const [mobileOpen, setMobileOpen] = useState(false)
  const [commandOpen, setCommandOpen] = useState(false)

  const handleMenuClick = useCallback(() => {
    setMobileOpen(true)
  }, [])

  const handleSearchClick = useCallback(() => {
    setCommandOpen(true)
  }, [])

  return (
    <div className="flex h-screen w-full bg-background text-foreground overflow-hidden flex-col">
      {/* Topbar navigation now serves as the primary desktop nav */}
      <Topbar onMenuClick={handleMenuClick} onSearchClick={handleSearchClick} />

      {/* Mobile Sidebar Sheet */}
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="p-0 w-72">
          <MobileSidebarContent onClose={() => setMobileOpen(false)} />
        </SheetContent>
      </Sheet>

      {/* Main content area */}
      <div className="flex-1 flex flex-col overflow-hidden min-w-0">
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
