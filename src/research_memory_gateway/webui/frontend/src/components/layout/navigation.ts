import {
  Database,
  Download,
  FileClock,
  History,
  Import,
  LayoutDashboard,
  MessagesSquare,
  Settings,
  Shield,
} from 'lucide-react'

export const navItems = [
  { to: '/' as const, icon: LayoutDashboard, labelKey: 'nav.dashboard' },
  { to: '/memories' as const, icon: Database, labelKey: 'nav.memories' },
  { to: '/conversations' as const, icon: MessagesSquare, labelKey: 'nav.conversations' },
  { to: '/proposals' as const, icon: FileClock, labelKey: 'nav.proposals' },
  { to: '/config' as const, icon: Settings, labelKey: 'nav.config' },
  { to: '/security' as const, icon: Shield, labelKey: 'nav.security' },
  { to: '/import' as const, icon: Import, labelKey: 'nav.import' },
  { to: '/exports' as const, icon: Download, labelKey: 'nav.export' },
  { to: '/audit' as const, icon: History, labelKey: 'nav.audit' },
]
