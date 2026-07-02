import { Search, Sun, Moon, Monitor, Menu, Palette, Database, LogOut } from 'lucide-react'
import { Link, useMatchRoute } from '@tanstack/react-router'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
  DropdownMenuLabel,
} from '@/components/ui/dropdown-menu'
import { useTranslation } from 'react-i18next'
import { useThemeConfig } from '@/hooks/useTheme'
import { cn } from '@/lib/utils'
import { api } from '@/lib/api'
import { navItems } from './navigation'

interface TopbarProps {
  onMenuClick: () => void
  onSearchClick: () => void
}

export function Topbar({ onMenuClick, onSearchClick }: TopbarProps) {
  const { t, i18n } = useTranslation()
  const { setTheme, setPreset, presets } = useThemeConfig()

  const toggleLanguage = () => {
    const nextLang = i18n.language === 'zh-CN' ? 'en' : 'zh-CN'
    i18n.changeLanguage(nextLang)
    localStorage.setItem('i18nextLng', nextLang)
  }


  const matchRoute = useMatchRoute()

  return (
    <header className="h-16 border-b bg-background flex items-center justify-between px-4 md:px-6 shrink-0 sticky top-0 z-50 shadow-sm">
      <div className="flex items-center gap-4 flex-1">
        {/* Mobile menu button */}
        <Button
          variant="ghost"
          size="sm"
          className="md:hidden"
          onClick={onMenuClick}
        >
          <Menu className="h-5 w-5" />
        </Button>

        {/* Brand */}
        <div className="hidden md:flex items-center gap-2 mr-4">
          <Database className="w-5 h-5 text-primary shrink-0" />
          <span className="font-semibold tracking-tight text-foreground whitespace-nowrap">
            {t('common.app_name')}
          </span>
        </div>

        {/* Desktop Navigation */}
        <nav className="hidden md:flex items-center gap-1">
          {navItems.map((item) => {
            const isActive = !!matchRoute({ to: item.to, fuzzy: item.to !== '/' })
              || (item.to === '/' && !!matchRoute({ to: '/' }))
            return (
              <Link
                key={item.to}
                to={item.to}
                className={cn(
                  'flex items-center gap-2 px-4 py-2 rounded-full text-sm font-medium transition-all duration-200',
                  isActive
                    ? 'bg-primary text-primary-foreground shadow-sm'
                    : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                )}
              >
                <item.icon className="w-4 h-4 shrink-0" />
                <span>{t(item.labelKey)}</span>
              </Link>
            )
          })}
        </nav>
      </div>

      {/* Right side group: Search and actions */}
      <div className="flex items-center gap-2">
        {/* Search bar */}
        <div className="flex items-center w-full max-w-xs md:max-w-md">
        <button
          onClick={onSearchClick}
          className="relative w-full group"
        >
          <div className="flex items-center w-full h-9 px-3 py-2 rounded-lg bg-muted/50 border border-transparent hover:border-border transition-colors text-sm text-muted-foreground cursor-pointer">
            <Search className="h-4 w-4 mr-2 shrink-0" />
            <span className="truncate">{t('common.search')}</span>
            <kbd className="pointer-events-none ml-auto hidden h-5 select-none items-center gap-1 rounded border bg-background px-1.5 font-mono text-[10px] font-medium sm:flex">
              <span className="text-xs">⌘</span>K
            </kbd>
          </div>
        </button>
      </div>

      {/* Right side actions */}
      <div className="flex items-center gap-1 ml-2">
        {/* Theme / Color Picker */}
        <DropdownMenu>
          <DropdownMenuTrigger>
            <Button variant="ghost" size="sm" className="h-8 w-8 p-0">
              <Palette className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-48">
            <DropdownMenuLabel>{t('common.theme')}</DropdownMenuLabel>
            <DropdownMenuItem onClick={() => setTheme('light')}>
              <Sun className="mr-2 h-4 w-4" /> {t('theme.light')}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setTheme('dark')}>
              <Moon className="mr-2 h-4 w-4" /> {t('theme.dark')}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setTheme('system')}>
              <Monitor className="mr-2 h-4 w-4" /> {t('theme.system')}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuLabel>{t('theme.accent')}</DropdownMenuLabel>
            <div className="grid grid-cols-5 gap-1.5 px-2 py-1.5">
              {Object.entries(presets).map(([name, hue]) => (
                <button
                  key={name}
                  onClick={() => setPreset(name)}
                  className="w-6 h-6 rounded-full border-2 border-transparent hover:border-foreground/30 transition-colors focus:outline-none focus:ring-2 focus:ring-ring"
                  style={{
                    background: `oklch(0.55 0.16 ${hue})`,
                  }}
                  title={name}
                />
              ))}
            </div>
          </DropdownMenuContent>
        </DropdownMenu>

        {/* Language toggle */}
        <Button
          variant="ghost"
          size="sm"
          onClick={toggleLanguage}
          className="text-xs font-semibold h-8 px-2"
        >
          {i18n.language === 'zh-CN' ? 'EN' : '中'}
        </Button>

        {/* Logout */}
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            const refreshToken = localStorage.getItem('refresh_token') || undefined
            api.auth.logout(refreshToken).finally(() => {
              localStorage.removeItem('access_token')
              localStorage.removeItem('refresh_token')
              window.location.href = '/admin/login'
            })
          }}
          className="h-8 w-8 p-0 text-muted-foreground hover:text-destructive hover:bg-destructive/10"
          title={t('nav.logout')}
        >
          <LogOut className="h-4 w-4" />
        </Button>
      </div>
      </div>
    </header>
  )
}
