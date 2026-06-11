import { useTheme as useNextTheme } from 'next-themes'
import { useCallback, useEffect, useState } from 'react'

const THEME_HUES: Record<string, number> = {
  blue: 215,
  indigo: 240,
  violet: 270,
  pink: 330,
  rose: 350,
  orange: 30,
  amber: 45,
  emerald: 160,
  teal: 175,
  cyan: 195,
}

/**
 * Generate CSS color variables for a given hue.
 * This replaces the hue channel in all theme HSL values.
 */
function applyHueToDocument(hue: number, isDark: boolean) {
  const root = document.documentElement

  if (!isDark) {
    // Light theme
    root.style.setProperty('--background', `hsl(${hue} 30% 98%)`)
    root.style.setProperty('--foreground', `hsl(${hue} 25% 12%)`)
    root.style.setProperty('--card', `hsl(${hue} 42% 99%)`)
    root.style.setProperty('--card-foreground', `hsl(${hue} 25% 12%)`)
    root.style.setProperty('--popover', `hsl(${hue} 45% 99%)`)
    root.style.setProperty('--popover-foreground', `hsl(${hue} 25% 12%)`)
    root.style.setProperty('--primary', `hsl(${hue} 70% 50%)`)
    root.style.setProperty('--primary-foreground', `hsl(${hue} 30% 98%)`)
    root.style.setProperty('--secondary', `hsl(${hue} 20% 94%)`)
    root.style.setProperty('--secondary-foreground', `hsl(${hue} 20% 25%)`)
    root.style.setProperty('--muted', `hsl(${hue} 15% 95%)`)
    root.style.setProperty('--muted-foreground', `hsl(${hue} 10% 45%)`)
    root.style.setProperty('--accent', `hsl(${hue} 20% 94%)`)
    root.style.setProperty('--accent-foreground', `hsl(${hue} 20% 25%)`)
    root.style.setProperty('--border', `hsl(${hue} 15% 90%)`)
    root.style.setProperty('--input', `hsl(${hue} 15% 90%)`)
    root.style.setProperty('--ring', `hsl(${hue} 70% 50%)`)
    root.style.setProperty('--chart-1', `hsl(${hue} 70% 50%)`)
    root.style.setProperty('--sidebar', `hsl(${hue} 20% 97%)`)
    root.style.setProperty('--sidebar-foreground', `hsl(${hue} 20% 25%)`)
    root.style.setProperty('--sidebar-primary', `hsl(${hue} 70% 50%)`)
    root.style.setProperty('--sidebar-primary-foreground', `hsl(${hue} 30% 98%)`)
    root.style.setProperty('--sidebar-accent', `hsl(${hue} 20% 94%)`)
    root.style.setProperty('--sidebar-accent-foreground', `hsl(${hue} 20% 25%)`)
    root.style.setProperty('--sidebar-border', `hsl(${hue} 15% 90%)`)
    root.style.setProperty('--sidebar-ring', `hsl(${hue} 70% 50%)`)
  } else {
    // Dark theme
    root.style.setProperty('--background', `hsl(${hue} 25% 9%)`)
    root.style.setProperty('--foreground', `hsl(${hue} 10% 93%)`)
    root.style.setProperty('--card', `hsl(${hue} 20% 12%)`)
    root.style.setProperty('--card-foreground', `hsl(${hue} 10% 93%)`)
    root.style.setProperty('--popover', `hsl(${hue} 20% 12%)`)
    root.style.setProperty('--popover-foreground', `hsl(${hue} 10% 93%)`)
    root.style.setProperty('--primary', `hsl(${hue} 70% 60%)`)
    root.style.setProperty('--primary-foreground', `hsl(${hue} 25% 9%)`)
    root.style.setProperty('--secondary', `hsl(${hue} 15% 18%)`)
    root.style.setProperty('--secondary-foreground', `hsl(${hue} 10% 88%)`)
    root.style.setProperty('--muted', `hsl(${hue} 15% 18%)`)
    root.style.setProperty('--muted-foreground', `hsl(${hue} 10% 55%)`)
    root.style.setProperty('--accent', `hsl(${hue} 15% 18%)`)
    root.style.setProperty('--accent-foreground', `hsl(${hue} 10% 88%)`)
    root.style.setProperty('--border', `hsl(${hue} 15% 22%)`)
    root.style.setProperty('--input', `hsl(${hue} 15% 22%)`)
    root.style.setProperty('--ring', `hsl(${hue} 70% 60%)`)
    root.style.setProperty('--chart-1', `hsl(${hue} 70% 60%)`)
    root.style.setProperty('--sidebar', `hsl(${hue} 20% 10%)`)
    root.style.setProperty('--sidebar-foreground', `hsl(${hue} 10% 88%)`)
    root.style.setProperty('--sidebar-primary', `hsl(${hue} 70% 60%)`)
    root.style.setProperty('--sidebar-primary-foreground', `hsl(${hue} 25% 9%)`)
    root.style.setProperty('--sidebar-accent', `hsl(${hue} 15% 18%)`)
    root.style.setProperty('--sidebar-accent-foreground', `hsl(${hue} 10% 88%)`)
    root.style.setProperty('--sidebar-border', `hsl(${hue} 15% 22%)`)
    root.style.setProperty('--sidebar-ring', `hsl(${hue} 70% 60%)`)
  }
}

export function useThemeConfig() {
  const { theme, setTheme, systemTheme, resolvedTheme } = useNextTheme()
  const [hue, setHueState] = useState<number>(() => {
    if (typeof window === 'undefined') return 215
    return parseInt(localStorage.getItem('theme-hue') || '215', 10)
  })

  const isDark = resolvedTheme === 'dark'

  // Apply hue when it changes or when dark/light mode changes
  useEffect(() => {
    applyHueToDocument(hue, isDark)
  }, [hue, isDark])

  const setHue = useCallback((value: number) => {
    setHueState(value)
    localStorage.setItem('theme-hue', String(value))
  }, [])

  const setPreset = useCallback((preset: string) => {
    const h = THEME_HUES[preset]
    if (h !== undefined) setHue(h)
  }, [setHue])

  return {
    theme: theme || 'system',
    setTheme,
    systemTheme,
    resolvedTheme,
    isDark,
    hue,
    setHue,
    setPreset,
    presets: THEME_HUES,
  }
}
