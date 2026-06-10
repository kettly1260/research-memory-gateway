import * as React from "react"
import { useNavigate } from "@tanstack/react-router"
import { useTranslation } from "react-i18next"
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command"
import { LayoutDashboard, Database, Settings, Shield, Import, Download, History, Plus } from "lucide-react"

interface CommandPaletteProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function CommandPalette({ open, onOpenChange }: CommandPaletteProps) {
  const navigate = useNavigate()
  const { t } = useTranslation()

  // Keyboard shortcut
  React.useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        onOpenChange(!open)
      }
    }
    document.addEventListener("keydown", down)
    return () => document.removeEventListener("keydown", down)
  }, [open, onOpenChange])

  const runCommand = React.useCallback((command: () => unknown) => {
    onOpenChange(false)
    command()
  }, [onOpenChange])

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange}>
      <CommandInput placeholder={t("common.search")} />
      <CommandList>
        <CommandEmpty>{t("common.no_results") || "No results found."}</CommandEmpty>
        <CommandGroup heading={t("nav.dashboard")}>
          <CommandItem onSelect={() => runCommand(() => navigate({ to: "/" }))}>
            <LayoutDashboard className="mr-2 h-4 w-4" />
            {t("nav.dashboard")}
          </CommandItem>
          <CommandItem onSelect={() => runCommand(() => navigate({ to: "/memories" }))}>
            <Database className="mr-2 h-4 w-4" />
            {t("nav.memories")}
          </CommandItem>
          <CommandItem onSelect={() => runCommand(() => navigate({ to: "/config" }))}>
            <Settings className="mr-2 h-4 w-4" />
            {t("nav.config")}
          </CommandItem>
          <CommandItem onSelect={() => runCommand(() => navigate({ to: "/security" }))}>
            <Shield className="mr-2 h-4 w-4" />
            {t("nav.security")}
          </CommandItem>
          <CommandItem onSelect={() => runCommand(() => navigate({ to: "/audit" }))}>
            <History className="mr-2 h-4 w-4" />
            {t("nav.audit")}
          </CommandItem>
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading={t("common.actions")}>
          <CommandItem onSelect={() => runCommand(() => navigate({ to: "/memories/new" as string }))}>
            <Plus className="mr-2 h-4 w-4" />
            {t("memories.new_memory")}
          </CommandItem>
          <CommandItem onSelect={() => runCommand(() => navigate({ to: "/import" }))}>
            <Import className="mr-2 h-4 w-4" />
            {t("nav.import")}
          </CommandItem>
          <CommandItem onSelect={() => runCommand(() => navigate({ to: "/exports" }))}>
            <Download className="mr-2 h-4 w-4" />
            {t("nav.export")}
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  )
}
