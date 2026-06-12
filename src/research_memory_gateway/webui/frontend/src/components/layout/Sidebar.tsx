import { Link, useMatchRoute } from '@tanstack/react-router'
import { useTranslation } from 'react-i18next'
import {
  Database,
  LogOut,
  ChevronLeft,
  ChevronRight,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { api } from '@/lib/api'

interface SidebarProps {
  collapsed: boolean
  onToggle: () => void
}

import { navItems } from './Topbar'

export function Sidebar({ collapsed, onToggle }: SidebarProps) {
  const { t } = useTranslation()
  const matchRoute = useMatchRoute()

  return (
    <aside
      className={cn(
        'hidden md:flex flex-col h-full shrink-0 border-r bg-sidebar transition-all duration-300 ease-in-out',
        collapsed ? 'w-16' : 'w-64'
      )}
    >
      {/* Brand */}
      <div className="h-14 flex items-center px-4 border-b border-sidebar-border">
        <Database className="w-5 h-5 text-sidebar-primary shrink-0" />
        {!collapsed && (
          <span className="ml-2 font-semibold tracking-tight text-sidebar-foreground animate-fade-in whitespace-nowrap">
            {t('common.app_name')}
          </span>
        )}
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto py-3 px-2 space-y-0.5">
        {navItems.map((item) => {
          const isActive = !!matchRoute({ to: item.to, fuzzy: item.to !== '/' })
            || (item.to === '/' && !!matchRoute({ to: '/' }))
          return (
            <Link
              key={item.to}
              to={item.to}
              className={cn(
                'flex items-center gap-3 rounded-lg text-sm transition-all duration-200 group relative',
                collapsed ? 'justify-center px-2 py-2.5' : 'px-3 py-2',
                isActive
                  ? 'bg-sidebar-primary/10 text-sidebar-primary font-medium shadow-sm'
                  : 'text-sidebar-foreground/70 hover:bg-sidebar-accent hover:text-sidebar-foreground',
              )}
            >
              <item.icon className={cn('w-4 h-4 shrink-0', isActive && 'text-sidebar-primary')} />
              {!collapsed && <span className="truncate">{t(item.labelKey)}</span>}
              {/* Active indicator */}
              {isActive && (
                <div className="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-5 rounded-r bg-sidebar-primary" />
              )}
              {/* Collapsed tooltip */}
              {collapsed && (
                <div className="absolute left-full ml-2 px-2 py-1 rounded-md bg-popover text-popover-foreground text-xs shadow-md border opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none z-50 whitespace-nowrap">
                  {t(item.labelKey)}
                </div>
              )}
            </Link>
          )
        })}
      </nav>

      {/* Footer */}
      <div className="border-t border-sidebar-border p-2 space-y-1">
        <button
          onClick={() => {
            const refreshToken = localStorage.getItem('refresh_token') || undefined
            api.auth.logout(refreshToken).finally(() => {
              localStorage.removeItem('access_token')
              localStorage.removeItem('refresh_token')
              window.location.href = '/admin/login'
            })
          }}
          className={cn(
            'flex w-full items-center gap-3 rounded-lg text-sm transition-colors text-sidebar-foreground/70 hover:bg-destructive/10 hover:text-destructive',
            collapsed ? 'justify-center px-2 py-2.5' : 'px-3 py-2',
          )}
        >
          <LogOut className="w-4 h-4 shrink-0" />
          {!collapsed && t('nav.logout')}
        </button>

        {/* Collapse toggle */}
        <Button
          variant="ghost"
          size="sm"
          onClick={onToggle}
          className={cn('w-full', collapsed ? 'justify-center px-2' : 'justify-end')}
        >
          {collapsed ? <ChevronRight className="w-4 h-4" /> : <ChevronLeft className="w-4 h-4" />}
        </Button>
      </div>
    </aside>
  )
}

// Mobile Sidebar content (for Sheet)
export function MobileSidebarContent({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation()
  const matchRoute = useMatchRoute()

  return (
    <div className="flex flex-col h-full">
      <div className="h-14 flex items-center px-4 border-b">
        <Database className="w-5 h-5 text-primary" />
        <span className="ml-2 font-semibold tracking-tight">{t('common.app_name')}</span>
      </div>
      <nav className="flex-1 overflow-y-auto py-3 px-2 space-y-0.5">
        {navItems.map((item) => {
          const isActive = !!matchRoute({ to: item.to, fuzzy: item.to !== '/' })
            || (item.to === '/' && !!matchRoute({ to: '/' }))
          return (
            <Link
              key={item.to}
              to={item.to}
              onClick={onClose}
              className={cn(
                'flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-colors',
                isActive
                  ? 'bg-primary/10 text-primary font-medium'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground',
              )}
            >
              <item.icon className="w-4 h-4" />
              {t(item.labelKey)}
            </Link>
          )
        })}
      </nav>
      <div className="p-4 border-t">
        <button
          onClick={() => {
            const refreshToken = localStorage.getItem('refresh_token') || undefined
            api.auth.logout(refreshToken).finally(() => {
              localStorage.removeItem('access_token')
              localStorage.removeItem('refresh_token')
              window.location.href = '/admin/login'
            })
          }}
          className="flex w-full items-center gap-3 px-3 py-2 rounded-lg text-sm text-muted-foreground hover:bg-destructive/10 hover:text-destructive transition-colors"
        >
          <LogOut className="w-4 h-4" />
          {t('nav.logout')}
        </button>
      </div>
    </div>
  )
}
