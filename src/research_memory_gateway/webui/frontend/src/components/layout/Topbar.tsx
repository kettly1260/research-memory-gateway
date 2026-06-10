import { Search, Sun, Moon, Monitor, Menu, Palette } from 'lucide-react'
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


  return (
    <header className="h-14 border-b bg-background/80 backdrop-blur-sm flex items-center justify-between px-4 md:px-6 shrink-0 sticky top-0 z-30">
      {/* Mobile menu button */}
      <Button
        variant="ghost"
        size="sm"
        className="md:hidden mr-2"
        onClick={onMenuClick}
      >
        <Menu className="h-5 w-5" />
      </Button>

      {/* Search bar */}
      <div className="flex items-center flex-1 max-w-md">
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
            <DropdownMenuLabel>{t('common.theme') || 'Theme'}</DropdownMenuLabel>
            <DropdownMenuItem onClick={() => setTheme('light')}>
              <Sun className="mr-2 h-4 w-4" /> Light
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setTheme('dark')}>
              <Moon className="mr-2 h-4 w-4" /> Dark
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setTheme('system')}>
              <Monitor className="mr-2 h-4 w-4" /> System
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuLabel>Accent</DropdownMenuLabel>
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
      </div>
    </header>
  )
}
