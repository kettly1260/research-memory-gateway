import * as React from "react"
import { useTranslation } from "react-i18next"
import { Link, useNavigate } from "@tanstack/react-router"
import {
  type ColumnDef,
  type ColumnFiltersState,
  type SortingState,
  type VisibilityState,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table"
import { MoreHorizontal, Plus, Search, LayoutGrid, LayoutList, ChevronLeft, ChevronRight, ArrowUpDown, Database } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  DropdownMenuCheckboxItem,
} from "@/components/ui/dropdown-menu"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { useMemories, useArchiveMemory, useSoftDeleteMemory, useRestoreMemory, useProjects, useTaxonomy } from "@/lib/query"
import type { ResearchMemory } from "@/types/api"
import { MEMORY_TYPES, formatMemoryType } from "@/constants/memoryTypes"
import { toast } from "sonner"

const statusVariants: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  active: 'default',
  archived: 'secondary',
  deleted: 'destructive',
}

export function Memories() {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const [sorting, setSorting] = React.useState<SortingState>([{ id: 'updatedAt', desc: true }])
  const [columnFilters, setColumnFilters] = React.useState<ColumnFiltersState>([])
  const [columnVisibility, setColumnVisibility] = React.useState<VisibilityState>({})
  const [rowSelection, setRowSelection] = React.useState({})
  const [globalFilter, setGlobalFilter] = React.useState("")
  const [statusFilter, setStatusFilter] = React.useState("active")
  const [projectFilter, setProjectFilter] = React.useState<string | undefined>()
  const [typeFilter, setTypeFilter] = React.useState<string | undefined>()
  const [viewMode, setViewMode] = React.useState<'table' | 'card'>('table')

  const { data: memories = [], isLoading } = useMemories({
    status: statusFilter,
    query: globalFilter || undefined,
    project: projectFilter,
    memory_type: typeFilter,
  })
  const { data: projects = [] } = useProjects()
  const { data: taxonomy } = useTaxonomy()
  const memoryTypeKeys = taxonomy?.memory_types.map((item) => item.key) || [...MEMORY_TYPES]

  const archiveMutation = useArchiveMemory()
  const softDeleteMutation = useSoftDeleteMemory()
  const restoreMutation = useRestoreMemory()

  const columns: ColumnDef<ResearchMemory>[] = React.useMemo(() => [
    {
      accessorKey: "title",
      header: ({ column }) => (
        <Button variant="ghost" className="-ml-3 h-8" onClick={() => column.toggleSorting()}>
          {t("memories.col_title")}
          <ArrowUpDown className="ml-2 h-3 w-3" />
        </Button>
      ),
      cell: ({ row }) => (
        <Link
          to={`/memories/${row.original.memory_id}` as string}
          className="font-medium max-w-[300px] truncate block hover:text-primary transition-colors"
        >
          {row.getValue("title")}
        </Link>
      ),
    },
    {
      accessorKey: "project",
      header: t("memories.col_project"),
    },
    {
      accessorKey: "topic",
      header: t("memories.col_topic"),
      cell: ({ row }) => <span className="text-muted-foreground truncate max-w-[160px] block">{row.getValue("topic")}</span>,
    },
    {
      accessorKey: "memory_type",
      header: t("memories.col_type"),
      cell: ({ row }) => (
        <Badge variant="outline" className="text-[11px]">
          {formatMemoryType(String(row.getValue("memory_type")), taxonomy?.memory_types, i18n.language)}
        </Badge>
      ),
    },
    {
      accessorKey: "memory_status",
      header: t("memories.col_status"),
      cell: ({ row }) => {
        const status = row.getValue("memory_status") as string
        return (
          <Badge variant={statusVariants[status] || 'outline'} className="capitalize text-[11px]">
            {t(`common.${status}`)}
          </Badge>
        )
      },
    },
    {
      accessorKey: "updated_at",
      id: "updatedAt",
      header: ({ column }) => (
        <Button variant="ghost" className="-ml-3 h-8" onClick={() => column.toggleSorting()}>
          {t("memories.col_updated")}
          <ArrowUpDown className="ml-2 h-3 w-3" />
        </Button>
      ),
      cell: ({ row }) => {
        const date = new Date(row.original.updated_at)
        return <span className="text-muted-foreground text-sm tabular-nums">{date.toLocaleDateString()} {date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
      },
    },
    {
      id: "actions",
      cell: ({ row }) => {
        const memory = row.original
        return (
          <DropdownMenu>
            <DropdownMenuTrigger>
              <Button variant="ghost" className="h-8 w-8 p-0">
                <span className="sr-only">{t("common.open_menu")}</span>
                <MoreHorizontal className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuLabel>{t("common.actions")}</DropdownMenuLabel>
              <DropdownMenuItem onClick={() => navigator.clipboard.writeText(memory.memory_id)}>
                {t("common.copy_id")}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={() => navigate({ to: '/memories/$id', params: { id: memory.memory_id } })}>
                {t("common.edit")}
              </DropdownMenuItem>
              {memory.memory_status === "active" && (
                <DropdownMenuItem onClick={() => {
                  archiveMutation.mutate({ id: memory.memory_id }, {
                    onSuccess: () => toast.success(t("memories.archived_success")),
                  })
                }}>
                  {t("memories.archive")}
                </DropdownMenuItem>
              )}
              {memory.memory_status === "archived" && (
                <DropdownMenuItem onClick={() => {
                  restoreMutation.mutate({ id: memory.memory_id }, {
                    onSuccess: () => toast.success(t("memories.restored_success")),
                  })
                }}>
                  {t("memories.restore")}
                </DropdownMenuItem>
              )}
              {memory.memory_status !== "deleted" && (
                <DropdownMenuItem
                  className="text-destructive focus:bg-destructive focus:text-destructive-foreground"
                  onClick={() => {
                    softDeleteMutation.mutate({ id: memory.memory_id }, {
                      onSuccess: () => toast.success(t("memories.deleted_success")),
                    })
                  }}
                >
                  {t("common.delete")}
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )
      },
    },
  ], [t, i18n.language, taxonomy?.memory_types, archiveMutation, softDeleteMutation, restoreMutation, navigate])

  const table = useReactTable({
    data: memories,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    onSortingChange: setSorting,
    getSortedRowModel: getSortedRowModel(),
    onColumnFiltersChange: setColumnFilters,
    getFilteredRowModel: getFilteredRowModel(),
    onColumnVisibilityChange: setColumnVisibility,
    onRowSelectionChange: setRowSelection,
    state: {
      sorting,
      columnFilters,
      columnVisibility,
      rowSelection,
    },
    initialState: {
      pagination: { pageSize: 20 },
    },
  })

  return (
    <div className="flex-1 space-y-4 p-6 md:p-8 pt-6 max-w-7xl mx-auto animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold tracking-tight">{t("memories.title")}</h1>
        <div className="flex items-center gap-2">
          <Link to="/memories/new" className="hidden sm:block">
            <Button>
              <Plus className="mr-2 h-4 w-4" /> {t("memories.new_memory")}
            </Button>
          </Link>
        </div>
      </div>

      {/* Filters Bar */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative max-w-sm w-full">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder={t("common.search")}
            value={globalFilter}
            onChange={(e) => setGlobalFilter(e.target.value)}
            className="pl-9 h-9"
          />
        </div>

        <Select value={statusFilter} onValueChange={(v) => { if (v !== null) setStatusFilter(v) }}>
          <SelectTrigger className="w-[130px] h-9">
            <SelectValue placeholder={t("memories.filter_status")} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="active">{t("common.active")}</SelectItem>
            <SelectItem value="archived">{t("common.archived")}</SelectItem>
            <SelectItem value="deleted">{t("common.deleted")}</SelectItem>
            <SelectItem value="all">{t("common.all")}</SelectItem>
          </SelectContent>
        </Select>

        {projects.length > 0 && (
          <Select value={projectFilter || '_all'} onValueChange={(v) => { if (v !== null) setProjectFilter(v === '_all' ? undefined : v) }}>
            <SelectTrigger className="w-[160px] h-9">
              <SelectValue placeholder={t("memories.filter_project")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="_all">{t("memories.all_projects")}</SelectItem>
              {projects.map((p) => (
                <SelectItem key={p} value={p}>{p}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}

        <Select value={typeFilter || '_all'} onValueChange={(v) => { if (v !== null) setTypeFilter(v === '_all' ? undefined : v) }}>
          <SelectTrigger className="w-[170px] h-9">
            <SelectValue placeholder={t("memories.filter_type")} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="_all">{t("memories.all_types")}</SelectItem>
            {memoryTypeKeys.map((type) => (
              <SelectItem key={type} value={type}>{formatMemoryType(type, taxonomy?.memory_types, i18n.language)}</SelectItem>
            ))}
          </SelectContent>
        </Select>

        <div className="flex items-center gap-1 ml-auto">
          {/* Column visibility */}
          <DropdownMenu>
            <DropdownMenuTrigger>
              <Button variant="outline" size="sm" className="h-9">
                {t("memories.columns")}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {table.getAllColumns().filter((col) => col.getCanHide()).map((col) => (
                <DropdownMenuCheckboxItem
                  key={col.id}
                  checked={col.getIsVisible()}
                  onCheckedChange={(checked) => col.toggleVisibility(!!checked)}
                >
                  {col.id}
                </DropdownMenuCheckboxItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>

          {/* View toggle */}
          <Button
            variant="outline"
            size="sm"
            className="h-9 w-9 p-0"
            onClick={() => setViewMode(viewMode === 'table' ? 'card' : 'table')}
          >
            {viewMode === 'table' ? <LayoutGrid className="h-4 w-4" /> : <LayoutList className="h-4 w-4" />}
          </Button>
        </div>
      </div>

      {/* Table View */}
      {viewMode === 'table' ? (
        <div className="rounded-lg border bg-card overflow-hidden">
          <Table>
            <TableHeader>
              {table.getHeaderGroups().map((headerGroup) => (
                <TableRow key={headerGroup.id} className="bg-muted/30 hover:bg-muted/30">
                  {headerGroup.headers.map((header) => (
                    <TableHead key={header.id} className="h-10 text-xs font-semibold uppercase tracking-wider">
                      {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                    </TableHead>
                  ))}
                </TableRow>
              ))}
            </TableHeader>
            <TableBody>
              {isLoading ? (
                Array.from({ length: 5 }).map((_, i) => (
                  <TableRow key={i}>
                    {columns.map((_, j) => (
                      <TableCell key={j}>
                        <div className="skeleton h-4 w-full" />
                      </TableCell>
                    ))}
                  </TableRow>
                ))
              ) : table.getRowModel().rows?.length ? (
                table.getRowModel().rows.map((row) => (
                  <TableRow key={row.id} data-state={row.getIsSelected() && "selected"} className="stagger-item">
                    {row.getVisibleCells().map((cell) => (
                      <TableCell key={cell.id}>
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </TableCell>
                    ))}
                  </TableRow>
                ))
              ) : (
                <TableRow>
                  <TableCell colSpan={columns.length} className="h-32 text-center text-muted-foreground">
                    {t("common.no_results")}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
      ) : (
        /* Card View */
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {isLoading ? (
            Array.from({ length: 6 }).map((_, i) => (
              <Card key={i}>
                <CardContent className="p-4 space-y-3">
                  <div className="skeleton h-5 w-3/4" />
                  <div className="skeleton h-3 w-1/2" />
                  <div className="skeleton h-3 w-full" />
                </CardContent>
              </Card>
            ))
          ) : memories.map((memory) => (
            <Link key={memory.memory_id} to={`/memories/${memory.memory_id}` as string}>
              <Card className="hover:shadow-md transition-all duration-200 hover:-translate-y-0.5 cursor-pointer group">
                <CardHeader className="pb-2">
                  <div className="flex items-start justify-between">
                    <div className="flex items-center gap-2">
                      <div className="w-7 h-7 rounded-md bg-primary/10 flex items-center justify-center shrink-0 group-hover:bg-primary/15 transition-colors">
                        <Database className="w-3.5 h-3.5 text-primary" />
                      </div>
                      <CardTitle className="text-sm font-medium line-clamp-2">{memory.title}</CardTitle>
                    </div>
                  </div>
                </CardHeader>
                <CardContent className="pt-0">
                  <p className="text-xs text-muted-foreground line-clamp-2 mb-3">{memory.summary}</p>
                  <div className="flex items-center gap-2 flex-wrap">
                    <Badge variant={statusVariants[memory.memory_status] || 'outline'} className="text-[10px]">
                      {t(`common.${memory.memory_status}`)}
                    </Badge>
                    <Badge variant="outline" className="text-[10px]">
                      {formatMemoryType(memory.memory_type, taxonomy?.memory_types, i18n.language)}
                    </Badge>
                    <span className="text-[10px] text-muted-foreground ml-auto">{memory.project}</span>
                  </div>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      )}

      {/* Pagination */}
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {memories.length} {memories.length === 1 ? t("memories.memory_singular") : t("memories.memory_plural")}
        </p>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => table.previousPage()}
            disabled={!table.getCanPreviousPage()}
          >
            <ChevronLeft className="h-4 w-4 mr-1" /> {t("common.previous")}
          </Button>
          <span className="text-sm text-muted-foreground tabular-nums">
            {table.getState().pagination.pageIndex + 1} / {table.getPageCount() || 1}
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => table.nextPage()}
            disabled={!table.getCanNextPage()}
          >
            {t("common.next")} <ChevronRight className="h-4 w-4 ml-1" />
          </Button>
        </div>
      </div>
    </div>
  )
}
