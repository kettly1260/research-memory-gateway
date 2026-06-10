import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Clock, Filter, Database, Settings, Shield, Upload, Download, Activity, ChevronLeft, ChevronRight } from 'lucide-react'
import { useAuditEvents } from '@/lib/query'

const eventIcons: Record<string, React.ElementType> = {
  'memory.created': Database,
  'memory.updated': Database,
  'memory.archived': Database,
  'memory.restored': Database,
  'memory.soft_deleted': Database,
  'memory.hard_deleted': Database,
  'security.password_changed': Shield,
  'import.json_validated': Upload,
  'import.json_completed': Upload,
  'export.created': Download,
  'config.updated': Settings,
  'retrieval.backfill_started': Activity,
  'retrieval.backfill_completed': Activity,
  'retrieval.backfill_failed': Activity,
  'retrieval.backfill_cancelled': Activity,
  'retrieval.backfill_dry_run': Activity,
}

const eventColors: Record<string, string> = {
  'memory.created': 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  'memory.updated': 'bg-blue-500/10 text-blue-600 dark:text-blue-400',
  'memory.archived': 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  'memory.restored': 'bg-teal-500/10 text-teal-600 dark:text-teal-400',
  'memory.soft_deleted': 'bg-red-500/10 text-red-600 dark:text-red-400',
  'memory.hard_deleted': 'bg-red-500/10 text-red-600 dark:text-red-400',
  'security.password_changed': 'bg-purple-500/10 text-purple-600 dark:text-purple-400',
  'import.json_validated': 'bg-cyan-500/10 text-cyan-600 dark:text-cyan-400',
  'import.json_completed': 'bg-cyan-500/10 text-cyan-600 dark:text-cyan-400',
  'export.created': 'bg-indigo-500/10 text-indigo-600 dark:text-indigo-400',
  'config.updated': 'bg-orange-500/10 text-orange-600 dark:text-orange-400',
  'retrieval.backfill_started': 'bg-violet-500/10 text-violet-600 dark:text-violet-400',
  'retrieval.backfill_completed': 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  'retrieval.backfill_failed': 'bg-red-500/10 text-red-600 dark:text-red-400',
  'retrieval.backfill_cancelled': 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  'retrieval.backfill_dry_run': 'bg-slate-500/10 text-slate-600 dark:text-slate-400',
}

const PAGE_SIZE = 50

export function Audit() {
  const { t } = useTranslation()
  const [eventType, setEventType] = useState<string | undefined>(undefined)
  const [page, setPage] = useState(0)

  const { data, isLoading } = useAuditEvents({
    limit: String(PAGE_SIZE),
    offset: String(page * PAGE_SIZE),
    event_type: eventType,
  })

  const events = data?.items ?? []
  const total = data?.total ?? 0
  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <div className="p-6 md:p-8 max-w-5xl mx-auto space-y-6 animate-fade-in">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">{t('nav.audit')}</h1>
        <Badge variant="outline" className="text-xs">
          <Clock className="w-3 h-3 mr-1" />
          {total} events
        </Badge>
      </div>

      {/* Filters */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Filter className="w-4 h-4" />
            Filters
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-4 flex-wrap">
            <div className="space-y-1">
              <label className="text-xs text-muted-foreground font-medium">Event Type</label>
              <Select value={eventType ?? '_all'} onValueChange={(v) => { if (v !== null) { setEventType(v === '_all' ? undefined : v); setPage(0) } }}>
                <SelectTrigger className="w-[240px]">
                  <SelectValue placeholder="All Events" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="_all">All Events</SelectItem>
                  <SelectItem value="memory.created">Memory Created</SelectItem>
                  <SelectItem value="memory.updated">Memory Updated</SelectItem>
                  <SelectItem value="memory.archived">Memory Archived</SelectItem>
                  <SelectItem value="memory.restored">Memory Restored</SelectItem>
                  <SelectItem value="memory.soft_deleted">Memory Soft Deleted</SelectItem>
                  <SelectItem value="memory.hard_deleted">Memory Hard Deleted</SelectItem>
                  <SelectItem value="security.password_changed">Password Changed</SelectItem>
                  <SelectItem value="import.json_completed">Import Completed</SelectItem>
                  <SelectItem value="export.created">Export Created</SelectItem>
                  <SelectItem value="retrieval.backfill_started">Backfill Started</SelectItem>
                  <SelectItem value="retrieval.backfill_completed">Backfill Completed</SelectItem>
                  <SelectItem value="retrieval.backfill_failed">Backfill Failed</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Timeline */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Activity className="w-4 h-4" />
            Event Timeline
          </CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="space-y-4">
              {Array.from({ length: 5 }).map((_, i) => (
                <div key={i} className="flex gap-4">
                  <div className="skeleton w-10 h-10 rounded-full" />
                  <div className="flex-1 space-y-2">
                    <div className="skeleton h-4 w-1/3" />
                    <div className="skeleton h-3 w-1/2" />
                  </div>
                </div>
              ))}
            </div>
          ) : events.length === 0 ? (
            <div className="text-center py-16 text-muted-foreground">
              <Clock className="w-8 h-8 mx-auto mb-3 opacity-50" />
              <p className="text-sm">No audit events recorded yet.</p>
              <p className="text-xs mt-1">Events will appear here as you interact with the system.</p>
            </div>
          ) : (
            <div className="relative">
              {/* Timeline line */}
              <div className="absolute left-5 top-0 bottom-0 w-px bg-border" />

              <div className="space-y-6">
                {events.map((event, i) => {
                  const Icon = eventIcons[event.event_type] || Activity
                  const colorClass = eventColors[event.event_type] || 'bg-muted text-muted-foreground'

                  return (
                    <div key={`${event.timestamp}-${i}`} className="flex gap-4 relative stagger-item">
                      <div className={`w-10 h-10 rounded-full flex items-center justify-center shrink-0 z-10 ${colorClass}`}>
                        <Icon className="w-4 h-4" />
                      </div>
                      <div className="flex-1 min-w-0 pt-1">
                        <div className="flex items-center gap-2 flex-wrap">
                          <Badge variant="outline" className="text-[10px]">{event.event_type}</Badge>
                          <span className="text-xs text-muted-foreground">
                            {new Date(event.timestamp).toLocaleString()}
                          </span>
                        </div>
                        {event.memory_id && (
                          <p className="text-xs text-muted-foreground mt-1">
                            Memory: <code className="text-[10px]">{event.memory_id}</code>
                          </p>
                        )}
                        {event.metadata && Object.keys(event.metadata).length > 0 && (
                          <pre className="text-[10px] font-mono mt-1 text-muted-foreground/70">
                            {JSON.stringify(event.metadata, null, 2)}
                          </pre>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between mt-6 pt-4 border-t">
              <p className="text-xs text-muted-foreground">
                Page {page + 1} of {totalPages} · {total} total events
              </p>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage(Math.max(0, page - 1))}
                  disabled={page === 0}
                >
                  <ChevronLeft className="w-4 h-4" />
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setPage(Math.min(totalPages - 1, page + 1))}
                  disabled={page >= totalPages - 1}
                >
                  <ChevronRight className="w-4 h-4" />
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
