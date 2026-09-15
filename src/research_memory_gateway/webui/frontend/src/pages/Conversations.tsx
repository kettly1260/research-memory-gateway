import * as React from 'react'
import { useTranslation } from 'react-i18next'
import {
  AlertCircle,
  BookOpen,
  Check,
  Copy,
  Cpu,
  FileText,
  Layers,
  Loader2,
  MessagesSquare,
  Play,
  RefreshCw,
  Search,
  Sparkles,
} from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  useConversationStatus,
  useConversationVectorizationCancel,
  useConversationVectorizationDryRun,
  useConversationVectorizationJob,
  useConversationVectorizationStart,
  useConversationSearch,
  useConversationRecall,
  useConversationRead,
} from '@/lib/query'
import type { ConversationSearchResultItem } from '@/types/api'
import { toast } from 'sonner'

function formatThreadRole(role?: string, t?: (key: string) => string): string {
  if (!role) return t ? t('conversations.roleUnknown') : 'Unknown'
  if (role === 'user') return t ? t('conversations.roleUser') : '主会话'
  if (role === 'subagent') return t ? t('conversations.roleSubagent') : '子 Agent'
  if (role === 'guardian_review') return t ? t('conversations.roleGuardianReview') : 'Guardian Review'
  return role
}

function formatRoleBadgeLabel(role?: string, parentThreadId?: string, t?: (key: string) => string): string {
  const roleLabel = formatThreadRole(role, t)
  if (role === 'subagent' && parentThreadId) {
    return `${roleLabel} (Parent: ${parentThreadId.slice(0, 8)}...)`
  }
  return roleLabel
}

function getRoleBadgeClass(role?: string): string {
  if (role === 'user') {
    return 'text-[10px] px-1.5 py-0 h-4 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30'
  }
  if (role === 'subagent') {
    return 'text-[10px] px-1.5 py-0 h-4 bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30'
  }
  if (role === 'guardian_review') {
    return 'text-[10px] px-1.5 py-0 h-4 bg-purple-500/15 text-purple-700 dark:text-purple-300 border-purple-500/30'
  }
  return 'text-[10px] px-1.5 py-0 h-4'
}

export function Conversations() {
  const { t } = useTranslation()

  // ─── Status Query ───
  const statusQuery = useConversationStatus()
  const status = statusQuery.data

  // ─── Conversation Vectorization ───
  const vectorDryRun = useConversationVectorizationDryRun()
  const vectorStart = useConversationVectorizationStart()
  const vectorCancel = useConversationVectorizationCancel()
  const [vectorJobId, setVectorJobId] = React.useState<string | null>(null)
  const effectiveVectorJobId = vectorJobId || (
    status?.vectorization_job?.status === 'running'
      ? status.vectorization_job.job_id
      : null
  )
  const vectorJobQuery = useConversationVectorizationJob(effectiveVectorJobId)
  const vectorJob = vectorJobQuery.data
  const refreshedVectorJob = React.useRef<string | null>(null)

  React.useEffect(() => {
    if (!vectorJob || vectorJob.status === 'running') return
    if (refreshedVectorJob.current === vectorJob.job_id) return
    refreshedVectorJob.current = vectorJob.job_id
    void statusQuery.refetch()
  }, [vectorJob, statusQuery])

  const handleVectorDryRun = async () => {
    try {
      const result = await vectorDryRun.mutateAsync({})
      toast.success(t('conversations.vectorDryRunResult', {
        documents: result.documents,
        sections: result.sections,
      }))
    } catch (err) {
      toast.error(String(err))
    }
  }

  const handleVectorStart = async () => {
    try {
      const job = await vectorStart.mutateAsync({ job_timeout_seconds: 86400 })
      setVectorJobId(job.job_id)
      refreshedVectorJob.current = null
      toast.success(t('conversations.vectorStarted', {
        documents: job.total,
        sections: job.total_sections,
      }))
    } catch (err) {
      toast.error(String(err))
    }
  }

  const handleVectorCancel = async () => {
    if (!effectiveVectorJobId) return
    try {
      await vectorCancel.mutateAsync(effectiveVectorJobId)
      toast.success(t('conversations.vectorCancelRequested'))
    } catch (err) {
      toast.error(String(err))
    }
  }

  const vectorJobRunning = vectorJob?.status === 'running' || status?.vectorization_job?.status === 'running'
  const vectorProcessed = vectorJob
    ? vectorJob.completed + vectorJob.failed + vectorJob.skipped
    : 0
  const vectorProgressPct = vectorJob?.total
    ? Math.min(100, Math.round((vectorProcessed / vectorJob.total) * 100))
    : 0

  // ─── Search State & Query ───
  const [searchInput, setSearchInput] = React.useState('')
  const [searchProject, setSearchProject] = React.useState('')
  const [searchCid, setSearchCid] = React.useState('')
  const [searchPid, setSearchPid] = React.useState('')
  const [searchSourceSystem, setSearchSourceSystem] = React.useState('')
  const [searchThreadSource, setSearchThreadSource] = React.useState('')
  const [activeSearch, setActiveSearch] = React.useState<{
    query: string
    project?: string
    conversation_id?: string
    parent_thread_id?: string
    source_system?: string
    thread_source?: string
  } | null>(null)

  const searchQuery = useConversationSearch(
    activeSearch || { query: '' },
    !!activeSearch?.query
  )

  const handleSearchSubmit = (e?: React.FormEvent) => {
    if (e) e.preventDefault()
    const q = searchInput.trim()
    if (!q) {
      toast.error(t('conversations.emptySearch'))
      return
    }
    setActiveSearch({
      query: q,
      project: searchProject.trim() || undefined,
      conversation_id: searchCid.trim() || undefined,
      parent_thread_id: searchPid.trim() || undefined,
      source_system: searchSourceSystem.trim() || undefined,
      thread_source: searchThreadSource.trim() || undefined,
    })
  }

  // ─── Recall State & Query ───
  const [recallInput, setRecallInput] = React.useState('')
  const [tokenBudget, setTokenBudget] = React.useState('1500')
  const [recallProject, setRecallProject] = React.useState('')
  const [recallCid, setRecallCid] = React.useState('')
  const [recallPid, setRecallPid] = React.useState('')
  const [recallSourceSystem, setRecallSourceSystem] = React.useState('')
  const [recallThreadSource, setRecallThreadSource] = React.useState('')
  const [activeRecall, setActiveRecall] = React.useState<{
    query: string
    token_budget?: string
    project?: string
    conversation_id?: string
    parent_thread_id?: string
    source_system?: string
    thread_source?: string
  } | null>(null)

  const recallQuery = useConversationRecall(
    activeRecall || { query: '' },
    !!activeRecall?.query
  )

  const handleRecallSubmit = (e?: React.FormEvent) => {
    if (e) e.preventDefault()
    const q = recallInput.trim()
    if (!q) {
      toast.error(t('conversations.emptyRecall'))
      return
    }
    setActiveRecall({
      query: q,
      token_budget: tokenBudget.trim() || '1500',
      project: recallProject.trim() || undefined,
      conversation_id: recallCid.trim() || undefined,
      parent_thread_id: recallPid.trim() || undefined,
      source_system: recallSourceSystem.trim() || undefined,
      thread_source: recallThreadSource.trim() || undefined,
    })
  }

  // ─── Reader Drawer State ───
  const [selectedResult, setSelectedResult] = React.useState<ConversationSearchResultItem | null>(null)
  const [readFullNote, setReadFullNote] = React.useState(false)
  const [copiedText, setCopiedText] = React.useState<string | null>(null)

  const selectedHeading = (!readFullNote && selectedResult?.heading_path?.length)
    ? selectedResult.heading_path[selectedResult.heading_path.length - 1]
    : undefined

  const readQuery = useConversationRead(
    {
      file_path: selectedResult?.vault_path || '',
      heading: selectedHeading,
    },
    !!selectedResult?.vault_path
  )

  const handleCopy = (text: string, label: string) => {
    navigator.clipboard.writeText(text)
    setCopiedText(label)
    toast.success(`${label} ${t('common.copied')}`)
    setTimeout(() => setCopiedText(null), 2000)
  }

  return (
    <div className="p-6 md:p-8 max-w-7xl mx-auto space-y-6 animate-fade-in">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <MessagesSquare className="size-6 text-primary" />
            <h1 className="text-2xl font-bold tracking-tight">{t('conversations.title')}</h1>
            <Badge variant="outline" className="text-xs font-normal">
              {t('conversations.archiveReadOnly')}
            </Badge>
          </div>
          <p className="text-sm text-muted-foreground mt-1">
            {t('conversations.description')}
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => statusQuery.refetch()}
          disabled={statusQuery.isFetching}
        >
          <RefreshCw className={`size-3.5 mr-1.5 ${statusQuery.isFetching ? 'animate-spin' : ''}`} />
          {t('common.refresh')}
        </Button>
      </div>

      {/* Archive Disabled Notice */}
      {status && !status.enabled && (
        <div className="flex items-center gap-3 p-4 rounded-lg border border-amber-500/30 bg-amber-500/10 text-amber-900 dark:text-amber-200">
          <AlertCircle className="size-5 shrink-0" />
          <div className="text-sm">
            <p className="font-semibold">{t('conversations.disabledNotice')}</p>
          </div>
        </div>
      )}

      {/* ─── A. Status Cards ─── */}
      <div className="grid grid-cols-2 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Documents */}
        <Card className="rounded-lg">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-xs font-medium text-muted-foreground">
              {t('conversations.documents')}
            </CardTitle>
            <FileText className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{status?.documents ?? 0}</div>
            <p className="text-xs text-muted-foreground mt-1">
              {t('conversations.status')}
            </p>
          </CardContent>
        </Card>

        {/* Sections */}
        <Card className="rounded-lg">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-xs font-medium text-muted-foreground">
              {t('conversations.sections')}
            </CardTitle>
            <Layers className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{status?.sections ?? 0}</div>
            <p className="text-xs text-muted-foreground mt-1">
              {status?.sections_with_embedding ?? 0} {t('conversations.embeddings')}
            </p>
          </CardContent>
        </Card>

        {/* Embeddings / Vector Coverage */}
        <Card className="rounded-lg">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-xs font-medium text-muted-foreground">
              {t('conversations.vectorCoverage')}
            </CardTitle>
            <Sparkles className="size-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            {status?.embedding_model ? (
              <>
                <div className="text-2xl font-bold">
                  {((status?.vector_coverage ?? 0) * 100).toFixed(1)}%
                </div>
                <p className="text-xs text-muted-foreground mt-1">
                  {status.sections_with_embedding} / {status.sections}
                </p>
              </>
            ) : (
              <>
                <div className="text-base font-semibold text-muted-foreground pt-1">
                  <Badge variant="secondary" className="font-normal">
                    {t('conversations.lexicalOnly')}
                  </Badge>
                </div>
                <p className="text-xs text-muted-foreground mt-1">
                  FTS5 BM25
                </p>
              </>
            )}
          </CardContent>
        </Card>

        {/* Model Specs */}
        <Card className="rounded-lg">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-xs font-medium text-muted-foreground">
              {t('conversations.activeModel')}
            </CardTitle>
            <Badge variant="outline" className="text-[10px] uppercase font-mono">
              {status?.vector_generation != null ? `g${status.vector_generation}` : '—'}
            </Badge>
          </CardHeader>
          <CardContent>
            <div className="text-lg font-bold font-mono truncate">
              {status?.embedding_model || '—'}
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              {status?.embedding_dimension ? `${status.embedding_dimension}d` : 'No vectors'}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* ─── Conversation Vectorization ─── */}
      {status?.enabled && (
        <Card className="rounded-lg border-blue-500/20 bg-blue-500/[0.025]">
          <CardHeader className="pb-3">
            <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
              <div>
                <CardTitle className="text-base flex items-center gap-2">
                  <Sparkles className="size-4 text-blue-600" />
                  {t('conversations.vectorizationTitle')}
                </CardTitle>
                <CardDescription className="mt-1">
                  {t('conversations.vectorizationDesc')}
                </CardDescription>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleVectorDryRun}
                  disabled={vectorDryRun.isPending || vectorJobRunning || !status.embedding_model}
                >
                  {vectorDryRun.isPending ? (
                    <Loader2 className="size-3.5 mr-1.5 animate-spin" />
                  ) : (
                    <Search className="size-3.5 mr-1.5" />
                  )}
                  {t('conversations.vectorDryRun')}
                </Button>
                <Button
                  size="sm"
                  onClick={handleVectorStart}
                  disabled={
                    vectorStart.isPending ||
                    vectorJobRunning ||
                    !status.embedding_model ||
                    status.sections_without_embedding === 0
                  }
                >
                  {vectorStart.isPending || vectorJobRunning ? (
                    <Loader2 className="size-3.5 mr-1.5 animate-spin" />
                  ) : (
                    <Play className="size-3.5 mr-1.5" />
                  )}
                  {status.sections_with_embedding > 0
                    ? t('conversations.vectorCompleteMissing')
                    : t('conversations.vectorStart')}
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-3 text-sm">
              <div className="rounded-md border bg-background/70 p-3">
                <div className="text-xs text-muted-foreground">{t('conversations.vectorMissing')}</div>
                <div className="mt-1 font-semibold">{status.sections_without_embedding}</div>
              </div>
              <div className="rounded-md border bg-background/70 p-3">
                <div className="text-xs text-muted-foreground">{t('conversations.activeModel')}</div>
                <div className="mt-1 font-mono text-xs break-all">{status.embedding_model || '—'}</div>
              </div>
              <div className="rounded-md border bg-background/70 p-3">
                <div className="text-xs text-muted-foreground">{t('conversations.vectorWriteScope')}</div>
                <div className="mt-1 text-xs">{t('conversations.vectorIndexOnly')}</div>
              </div>
            </div>

            {!status.embedding_model && (
              <div className="flex items-center gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200">
                <AlertCircle className="size-4 shrink-0" />
                {t('conversations.vectorEmbeddingDisabled')}
              </div>
            )}

            {vectorJob && (
              <div className="space-y-3 rounded-lg border bg-background/80 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    {vectorJobRunning && <Loader2 className="size-4 animate-spin text-blue-600" />}
                    {t('conversations.vectorJobStatus')}
                  </div>
                  <Badge
                    variant={vectorJob.status === 'failed' ? 'destructive' : 'secondary'}
                    className={vectorJob.status === 'completed' ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300' : ''}
                  >
                    {t(`conversations.vectorStatus_${vectorJob.status}`)}
                  </Badge>
                </div>

                <div className="space-y-1.5">
                  <div className="flex justify-between text-xs text-muted-foreground">
                    <span>{t('conversations.vectorDocumentsProgress')}</span>
                    <span>{vectorProcessed} / {vectorJob.total} ({vectorProgressPct}%)</span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full rounded-full bg-blue-600 transition-all duration-300"
                      style={{ width: `${vectorProgressPct}%` }}
                    />
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 text-center text-xs">
                  <div className="rounded border p-2">
                    <div className="font-semibold text-emerald-600">{vectorJob.embedded_sections}</div>
                    <div className="mt-0.5 text-muted-foreground">{t('conversations.vectorNew')}</div>
                  </div>
                  <div className="rounded border p-2">
                    <div className="font-semibold">{vectorJob.cache_reused_sections}</div>
                    <div className="mt-0.5 text-muted-foreground">{t('conversations.vectorReused')}</div>
                  </div>
                  <div className="rounded border p-2">
                    <div className="font-semibold text-destructive">{vectorJob.failed_sections}</div>
                    <div className="mt-0.5 text-muted-foreground">{t('conversations.vectorFailed')}</div>
                  </div>
                  <div className="rounded border p-2">
                    <div className="font-semibold">{vectorJob.completed}</div>
                    <div className="mt-0.5 text-muted-foreground">{t('conversations.vectorDocumentsDone')}</div>
                  </div>
                </div>

                {vectorJob.current_file && vectorJobRunning && (
                  <div className="truncate text-[11px] font-mono text-muted-foreground" title={vectorJob.current_file}>
                    {vectorJob.current_file}
                  </div>
                )}

                {vectorJob.last_error && (
                  <div className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-xs text-destructive">
                    {vectorJob.last_error}
                  </div>
                )}

                {vectorJobRunning && (
                  <div className="flex justify-end">
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={handleVectorCancel}
                      disabled={vectorCancel.isPending}
                    >
                      {t('conversations.vectorCancel')}
                    </Button>
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* ─── Source & Role Distribution ─── */}
      {status && (status.source_system_distribution || status.thread_source_distribution) && (
        <Card className="rounded-lg bg-muted/20 border-border/60">
          <CardContent className="p-3.5 flex flex-wrap items-center justify-between gap-4 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-muted-foreground">{t('conversations.sourcePlatform')}:</span>
              {Object.entries(status.source_system_distribution || {}).map(([src, count]) => (
                <Badge key={src} variant="default" className="font-mono text-xs uppercase px-2 py-0.5 bg-blue-600/15 text-blue-700 dark:text-blue-300 border-blue-500/30">
                  {src}: {count}
                </Badge>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-muted-foreground">{t('conversations.threadRole')}:</span>
              {Object.entries(status.thread_source_distribution || {}).map(([role, count]) => (
                <Badge key={role} variant="secondary" className={`font-mono text-xs px-2 py-0.5 ${getRoleBadgeClass(role)}`}>
                  {formatThreadRole(role, t)}: {count}
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* ─── Main Tabs: Search & Recall Preview ─── */}
      <Tabs defaultValue="search" className="space-y-4">
        <TabsList>
          <TabsTrigger value="search">
            <Search className="size-3.5 mr-1.5" />
            {t('conversations.search')}
          </TabsTrigger>
          <TabsTrigger value="recall">
            <Play className="size-3.5 mr-1.5" />
            {t('conversations.recallPreview')}
          </TabsTrigger>
        </TabsList>

        {/* ─── B. Search Tab ─── */}
        <TabsContent value="search" className="space-y-4">
          <Card className="rounded-lg">
            <CardHeader className="pb-3">
              <CardTitle className="text-base">{t('conversations.search')}</CardTitle>
              <CardDescription>
                支持查询实验关键词 (如: Fe3+, 紫外, B36)、附件文件名 (如: TOC_Focused.png)、错误码或会话 UUID
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleSearchSubmit} className="space-y-3">
                <div className="flex gap-2">
                  <div className="relative flex-1">
                    <Search className="absolute left-3 top-2.5 size-4 text-muted-foreground" />
                    <Input
                      value={searchInput}
                      onChange={(e) => setSearchInput(e.target.value)}
                      placeholder={t('conversations.searchPlaceholder')}
                      className="pl-9"
                    />
                  </div>
                  <Button type="submit" disabled={searchQuery.isFetching}>
                    {searchQuery.isFetching ? (
                      <RefreshCw className="size-4 animate-spin mr-1.5" />
                    ) : (
                      <Search className="size-4 mr-1.5" />
                    )}
                    {t('conversations.runSearch')}
                  </Button>
                </div>

                {/* Optional Filters */}
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-2 pt-1 text-xs">
                  <Input
                    value={searchProject}
                    onChange={(e) => setSearchProject(e.target.value)}
                    placeholder={t('conversations.projectFilter')}
                    className="h-8 text-xs"
                  />
                  <Input
                    value={searchCid}
                    onChange={(e) => setSearchCid(e.target.value)}
                    placeholder={t('conversations.conversationIdFilter')}
                    className="h-8 text-xs"
                  />
                  <Input
                    value={searchPid}
                    onChange={(e) => setSearchPid(e.target.value)}
                    placeholder={t('conversations.parentThreadIdFilter')}
                    className="h-8 text-xs"
                  />
                  <Input
                    value={searchSourceSystem}
                    onChange={(e) => setSearchSourceSystem(e.target.value)}
                    placeholder={t('conversations.sourceSystemFilter')}
                    className="h-8 text-xs"
                  />
                  <Input
                    value={searchThreadSource}
                    onChange={(e) => setSearchThreadSource(e.target.value)}
                    placeholder={t('conversations.threadSourceFilter')}
                    className="h-8 text-xs"
                  />
                </div>
              </form>
            </CardContent>
          </Card>

          {/* Search Results List */}
          {searchQuery.isLoading && (
            <div className="text-center py-8 text-sm text-muted-foreground">
              <RefreshCw className="size-5 animate-spin mx-auto mb-2" />
              {t('common.loading')}
            </div>
          )}

          {searchQuery.data && (
            <div className="space-y-3">
              <div className="flex items-center justify-between text-xs text-muted-foreground px-1">
                <span>
                  共找到 {searchQuery.data.count} 条匹配结果
                  {searchQuery.data.fallback_to_lexical && (
                    <Badge variant="outline" className="ml-2 text-amber-600 dark:text-amber-400 border-amber-400/40">
                      FTS fallback: {searchQuery.data.fallback_reason}
                    </Badge>
                  )}
                </span>
              </div>

              {searchQuery.data.results.length === 0 ? (
                <Card className="rounded-lg p-8 text-center text-sm text-muted-foreground">
                  {t('common.no_results')}
                </Card>
              ) : (
                searchQuery.data.results.map((item, idx) => {
                  const hasAnchors = item.source_anchors && item.source_anchors.length > 0
                  return (
                    <Card key={item.section_id} className="rounded-lg hover:border-primary/50 transition-colors">
                      <CardContent className="p-4 space-y-2">
                        {/* Top Meta Row */}
                        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                          <div className="flex flex-wrap items-center gap-1.5">
                            {idx === 0 && (
                              <Badge variant="default" className="text-[10px] px-1.5 py-0 h-4">
                                {t('conversations.topResult')}
                              </Badge>
                            )}
                            {hasAnchors ? (
                              <Badge
                                variant="secondary"
                                className="text-[10px] px-1.5 py-0 h-4 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30"
                              >
                                {t('conversations.anchoredProvenance')} ({item.source_anchors!.length})
                              </Badge>
                            ) : (
                              <Badge variant="outline" className="text-[10px] px-1.5 py-0 h-4 text-muted-foreground">
                                {t('conversations.noAnchors')}
                              </Badge>
                            )}
                            <Badge variant="outline" className="text-[10px] px-1.5 py-0 h-4 uppercase font-mono">
                              {item.score_type} | {item.final_score.toFixed(3)}
                            </Badge>
                          </div>

                          <div className="flex items-center gap-2 text-muted-foreground">
                            {item.date && <span>{item.date}</span>}
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-6 text-xs px-2"
                              onClick={() => {
                                setSelectedResult(item)
                                setReadFullNote(false)
                              }}
                            >
                              <BookOpen className="size-3.5 mr-1" />
                              {t('conversations.readNote')}
                            </Button>
                          </div>
                        </div>

                        {/* Title & Heading */}
                        <div className="space-y-1">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <Badge variant="default" className="text-[10px] px-1.5 py-0 h-4 font-bold uppercase bg-blue-600/15 text-blue-700 dark:text-blue-300 border-blue-500/30">
                              {item.source_system || 'codex'}
                            </Badge>
                            <Badge variant="secondary" className={getRoleBadgeClass(item.thread_source)}>
                              {formatRoleBadgeLabel(item.thread_source, item.parent_thread_id, t)}
                            </Badge>
                            {item.model_name && (
                              <Badge variant="outline" className="text-[10px] px-1.5 py-0 h-4 font-mono text-muted-foreground">
                                {item.model_name}
                              </Badge>
                            )}
                          </div>
                          <h3 className="text-sm font-semibold text-foreground">
                            {item.title || 'Untitled Session'}
                          </h3>
                          {item.heading_path && item.heading_path.length > 0 && (
                            <p className="text-xs text-primary font-medium mt-0.5">
                              {item.heading_path.join(' > ')}
                            </p>
                          )}
                        </div>

                        {/* Content Excerpt */}
                        <p className="text-xs text-muted-foreground line-clamp-3 font-mono bg-muted/30 p-2 rounded border border-border/50">
                          {item.content}
                        </p>

                        {/* Identifiers & Details Row */}
                        <div className="flex flex-wrap items-center justify-between gap-2 pt-1 text-[11px] text-muted-foreground border-t border-border/40">
                          <div className="flex flex-wrap items-center gap-2">
                            <span>ID:</span>
                            <code className="font-mono bg-muted/60 px-1 py-0.5 rounded">
                              {item.conversation_id.slice(0, 8)}...
                            </code>
                            {item.source_originator && (
                              <span>{item.source_originator}</span>
                            )}
                            {item.source_surface && (
                              <span className="capitalize">({item.source_surface})</span>
                            )}
                            {item.source_version && (
                              <span className="font-mono">v{item.source_version}</span>
                            )}
                          </div>
                          <span className="truncate max-w-[300px]">{item.vault_path}</span>
                        </div>
                      </CardContent>
                    </Card>
                  )
                })
              )}
            </div>
          )}
        </TabsContent>

        {/* ─── C. Recall Preview Tab ─── */}
        <TabsContent value="recall" className="space-y-4">
          <Card className="rounded-lg">
            <CardHeader className="pb-3">
              <CardTitle className="text-base">{t('conversations.recallPreview')}</CardTitle>
              <CardDescription>
                完全复用 MCP conversation_recall 逻辑，预览在给定的 Token 预算下实际注入给 Agent 的上下文文本
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleRecallSubmit} className="space-y-3">
                <div className="flex gap-2">
                  <div className="relative flex-1">
                    <Search className="absolute left-3 top-2.5 size-4 text-muted-foreground" />
                    <Input
                      value={recallInput}
                      onChange={(e) => setRecallInput(e.target.value)}
                      placeholder="输入召回问题 (如: 总结上一次 Fe3+ 催化剂优化的实验结果与错误码...)"
                      className="pl-9"
                    />
                  </div>
                  <Button type="submit" disabled={recallQuery.isFetching}>
                    {recallQuery.isFetching ? (
                      <RefreshCw className="size-4 animate-spin mr-1.5" />
                    ) : (
                      <Play className="size-4 mr-1.5" />
                    )}
                    {t('conversations.runRecall')}
                  </Button>
                </div>

                {/* Token Budget Controls */}
                <div className="flex flex-wrap items-center gap-3 pt-1 text-xs">
                  <div className="flex items-center gap-1.5">
                    <span className="text-muted-foreground">{t('conversations.tokenBudget')}:</span>
                    <Input
                      type="number"
                      value={tokenBudget}
                      onChange={(e) => setTokenBudget(e.target.value)}
                      className="w-24 h-8 text-xs font-mono"
                    />
                  </div>
                  <div className="flex items-center gap-1">
                    {[100, 500, 1500, 3000, 4000].map((b) => (
                      <Button
                        key={b}
                        type="button"
                        variant={tokenBudget === String(b) ? 'secondary' : 'ghost'}
                        size="sm"
                        className="h-7 text-xs px-2"
                        onClick={() => setTokenBudget(String(b))}
                      >
                        {b}
                      </Button>
                    ))}
                  </div>
                  <span className="text-[11px] text-muted-foreground">
                    {t('conversations.tokenBudgetHint')}
                  </span>
                </div>

                {/* Optional Filters */}
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-2 pt-1 text-xs">
                  <Input
                    value={recallProject}
                    onChange={(e) => setRecallProject(e.target.value)}
                    placeholder={t('conversations.projectFilter')}
                    className="h-8 text-xs"
                  />
                  <Input
                    value={recallCid}
                    onChange={(e) => setRecallCid(e.target.value)}
                    placeholder={t('conversations.conversationIdFilter')}
                    className="h-8 text-xs"
                  />
                  <Input
                    value={recallPid}
                    onChange={(e) => setRecallPid(e.target.value)}
                    placeholder={t('conversations.parentThreadIdFilter')}
                    className="h-8 text-xs"
                  />
                  <Input
                    value={recallSourceSystem}
                    onChange={(e) => setRecallSourceSystem(e.target.value)}
                    placeholder={t('conversations.sourceSystemFilter')}
                    className="h-8 text-xs"
                  />
                  <Input
                    value={recallThreadSource}
                    onChange={(e) => setRecallThreadSource(e.target.value)}
                    placeholder={t('conversations.threadSourceFilter')}
                    className="h-8 text-xs"
                  />
                </div>
              </form>
            </CardContent>
          </Card>

          {recallQuery.isLoading && (
            <div className="text-center py-8 text-sm text-muted-foreground">
              <RefreshCw className="size-5 animate-spin mx-auto mb-2" />
              {t('common.loading')}
            </div>
          )}

          {recallQuery.data && (
            <div className="space-y-4">
              {/* Recall Metrics Summary */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <Card className="rounded-lg p-3">
                  <div className="text-xs text-muted-foreground">{t('conversations.charsUsed')}</div>
                  <div className="text-lg font-bold font-mono mt-0.5">
                    {recallQuery.data.context.length} / {recallQuery.data.context_char_budget}
                  </div>
                  <div className="text-[10px] text-muted-foreground">
                    {recallQuery.data.context_char_budget > 0
                      ? `${((recallQuery.data.context.length / recallQuery.data.context_char_budget) * 100).toFixed(1)}%`
                      : '0%'}
                  </div>
                </Card>

                <Card className="rounded-lg p-3">
                  <div className="text-xs text-muted-foreground">召回条目数</div>
                  <div className="text-lg font-bold font-mono mt-0.5">
                    {recallQuery.data.items.length} items
                  </div>
                </Card>

                <Card className="rounded-lg p-3 sm:col-span-2">
                  <div className="text-xs text-muted-foreground">{t('conversations.scoreType')}</div>
                  <div className="text-sm font-medium mt-1 flex items-center gap-2">
                    {recallQuery.data.fallback_to_lexical ? (
                      <Badge variant="outline" className="text-amber-600 dark:text-amber-400">
                        Fallback to lexical ({recallQuery.data.fallback_reason || 'disabled'})
                      </Badge>
                    ) : (
                      <Badge variant="default" className="bg-emerald-600">
                        Hybrid Vector + Lexical
                      </Badge>
                    )}
                  </div>
                </Card>
              </div>

              {/* Context Render Box */}
              <Card className="rounded-lg">
                <CardHeader className="py-3 px-4 flex flex-row items-center justify-between">
                  <CardTitle className="text-sm font-semibold">
                    MCP conversation_recall 注入上下文预览 (Agent Context)
                  </CardTitle>
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-7 text-xs"
                    onClick={() => handleCopy(recallQuery.data!.context, 'Context')}
                  >
                    {copiedText === 'Context' ? <Check className="size-3 mr-1" /> : <Copy className="size-3 mr-1" />}
                    复制上下文
                  </Button>
                </CardHeader>
                <CardContent className="p-4 pt-0">
                  <pre className="p-3 bg-muted/40 rounded-lg text-xs font-mono whitespace-pre-wrap max-h-[400px] overflow-y-auto border border-border">
                    {recallQuery.data.context || '(Empty context within budget)'}
                  </pre>
                </CardContent>
              </Card>

              {/* Recalled Items Breakdown */}
              <Card className="rounded-lg">
                <CardHeader className="py-3 px-4">
                  <CardTitle className="text-sm font-semibold">召回小节明细清单</CardTitle>
                </CardHeader>
                <CardContent className="p-0">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead className="w-12 text-center">#</TableHead>
                        <TableHead>Heading / Session</TableHead>
                        <TableHead>Score</TableHead>
                        <TableHead>Anchors</TableHead>
                        <TableHead className="text-right">Actions</TableHead>
                      </TableRow>
                    </TableHeader>
                      <TableBody>
                        {recallQuery.data.items.map((item, idx) => (
                          <TableRow key={`${item.conversation_id}-${idx}`}>
                            <TableCell className="text-center font-mono text-xs">{idx + 1}</TableCell>
                            <TableCell>
                              <div className="flex flex-wrap items-center gap-1 mb-1">
                                <Badge variant="default" className="text-[9px] px-1 py-0 font-bold uppercase bg-blue-600/15 text-blue-700 dark:text-blue-300 border-blue-500/30">
                                  {item.source_system || 'codex'}
                                </Badge>
                                <Badge variant="secondary" className={getRoleBadgeClass(item.thread_source)}>
                                  {formatRoleBadgeLabel(item.thread_source, item.parent_thread_id, t)}
                                </Badge>
                                {item.model_name && (
                                  <Badge variant="outline" className="text-[9px] px-1 py-0 font-mono text-muted-foreground">
                                    {item.model_name}
                                  </Badge>
                                )}
                              </div>
                              <div className="font-medium text-xs">{item.heading}</div>
                              <div className="text-[11px] text-muted-foreground font-mono truncate max-w-[280px]">
                                {item.conversation_id}
                              </div>
                            </TableCell>
                            <TableCell className="font-mono text-xs">{item.score.toFixed(3)}</TableCell>
                            <TableCell>
                              {item.source_anchors && item.source_anchors.length > 0 ? (
                                <Badge variant="secondary" className="text-[10px] font-mono">
                                  {item.source_anchors.map((a) => `ord:${a.ordinal ?? '?'}`).join(', ')}
                                </Badge>
                              ) : (
                                <span className="text-muted-foreground text-xs">—</span>
                              )}
                            </TableCell>
                            <TableCell className="text-right">
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-7 text-xs"
                                onClick={() => {
                                  setSelectedResult({
                                    section_id: `${item.conversation_id}-${idx}`,
                                    conversation_id: item.conversation_id,
                                    vault_path: item.vault_path,
                                    heading_path: [item.heading],
                                    title: item.heading,
                                    content: item.content,
                                    lexical_score: null,
                                    vector_score: null,
                                    final_score: item.score,
                                    score_type: 'hybrid',
                                    source_anchors: item.source_anchors,
                                    parent_thread_id: item.parent_thread_id,
                                    thread_source: item.thread_source,
                                    source_system: item.source_system,
                                    source_originator: item.source_originator,
                                    source_surface: item.source_surface,
                                    source_version: item.source_version,
                                    model_provider: item.model_provider,
                                    model_name: item.model_name,
                                    agent_path: item.agent_path,
                                  })
                                  setReadFullNote(false)
                                }}
                              >
                              <BookOpen className="size-3.5 mr-1" />
                              {t('conversations.readNote')}
                            </Button>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
            </div>
          )}
        </TabsContent>
      </Tabs>

      {/* ─── D. Conversation Detail / Reader Drawer ─── */}
      <Sheet open={!!selectedResult} onOpenChange={(open) => { if (!open) setSelectedResult(null) }}>
        <SheetContent side="right" className="w-[90vw] sm:max-w-2xl overflow-y-auto p-6 space-y-4">
            <SheetHeader className="p-0 pb-2 border-b">
              <div className="flex items-center justify-between pr-8">
                <SheetTitle className="text-lg font-bold">
                  {selectedResult?.title || 'Conversation Detail'}
                </SheetTitle>
              </div>
              <SheetDescription className="font-mono text-xs text-muted-foreground break-all">
                {selectedResult?.conversation_id}
              </SheetDescription>
            </SheetHeader>

            {/* Source Identity Card */}
            <Card className="rounded-lg bg-muted/20 border-border/60">
              <CardHeader className="py-2.5 px-3 border-b border-border/40">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-1.5 font-semibold text-xs text-foreground">
                    <Cpu className="size-3.5 text-primary" />
                    {t('conversations.sourceIdentity')}
                  </div>
                  <div className="flex items-center gap-1.5">
                    <Badge variant="default" className="text-[10px] px-1.5 py-0 h-4 font-bold uppercase bg-blue-600/15 text-blue-700 dark:text-blue-300 border-blue-500/30">
                      {selectedResult?.source_system || readQuery.data?.metadata?.source_system || 'codex'}
                    </Badge>
                    <Badge variant="secondary" className={getRoleBadgeClass(selectedResult?.thread_source || readQuery.data?.metadata?.thread_source)}>
                      {formatRoleBadgeLabel(
                        selectedResult?.thread_source || readQuery.data?.metadata?.thread_source,
                        selectedResult?.parent_thread_id || readQuery.data?.metadata?.parent_thread_id,
                        t
                      )}
                    </Badge>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="p-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px]">
                <div>
                  <span className="text-muted-foreground">{t('conversations.sourcePlatform')}: </span>
                  <span className="font-mono font-medium">{selectedResult?.source_system || readQuery.data?.metadata?.source_system || 'codex'}</span>
                </div>
                <div>
                  <span className="text-muted-foreground">{t('conversations.threadRole')}: </span>
                  <span className="font-medium">{formatThreadRole(selectedResult?.thread_source || readQuery.data?.metadata?.thread_source, t)}</span>
                </div>
                {(selectedResult?.parent_thread_id || readQuery.data?.metadata?.parent_thread_id) && (
                  <div className="col-span-2">
                    <span className="text-muted-foreground">{t('conversations.parentThread')}: </span>
                    <code className="font-mono text-[10px] bg-background/80 px-1 py-0.5 rounded border">
                      {selectedResult?.parent_thread_id || readQuery.data?.metadata?.parent_thread_id}
                    </code>
                  </div>
                )}
                {(selectedResult?.agent_path || readQuery.data?.metadata?.agent_path) && (
                  <div className="col-span-2">
                    <span className="text-muted-foreground">{t('conversations.agentPath')}: </span>
                    <code className="font-mono text-[10px] bg-background/80 px-1 py-0.5 rounded border">
                      {selectedResult?.agent_path || readQuery.data?.metadata?.agent_path}
                    </code>
                  </div>
                )}
                {(selectedResult?.model_name || readQuery.data?.metadata?.model_name) && (
                  <div>
                    <span className="text-muted-foreground">{t('conversations.model')}: </span>
                    <span className="font-mono font-medium">{selectedResult?.model_name || readQuery.data?.metadata?.model_name}</span>
                  </div>
                )}
                {(selectedResult?.source_originator || readQuery.data?.metadata?.source_originator) && (
                  <div>
                    <span className="text-muted-foreground">{t('conversations.originator')}: </span>
                    <span>{selectedResult?.source_originator || readQuery.data?.metadata?.source_originator}</span>
                  </div>
                )}
                {(selectedResult?.source_surface || readQuery.data?.metadata?.source_surface) && (
                  <div>
                    <span className="text-muted-foreground">{t('conversations.surface')}: </span>
                    <span className="capitalize">{selectedResult?.source_surface || readQuery.data?.metadata?.source_surface}</span>
                  </div>
                )}
                {(selectedResult?.source_version || readQuery.data?.metadata?.source_version) && (
                  <div>
                    <span className="text-muted-foreground">{t('conversations.clientVersion')}: </span>
                    <span className="font-mono">{selectedResult?.source_version || readQuery.data?.metadata?.source_version}</span>
                  </div>
                )}
              </CardContent>
            </Card>

            {/* Path & Controls */}
            <div className="space-y-3 text-xs">
            <div className="p-2.5 rounded-lg bg-muted/40 border border-border/60 space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground font-medium">{t('conversations.filePath')}:</span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 text-[11px] px-2"
                  onClick={() => handleCopy(selectedResult?.vault_path || '', 'Path')}
                >
                  {copiedText === 'Path' ? <Check className="size-3 mr-1" /> : <Copy className="size-3 mr-1" />}
                  复制
                </Button>
              </div>
              <code className="block font-mono text-[11px] break-all bg-background/80 p-1.5 rounded border">
                {selectedResult?.vault_path}
              </code>
            </div>

            {/* Heading Toggle */}
            {selectedResult?.heading_path && selectedResult.heading_path.length > 0 && (
              <div className="flex items-center justify-between p-2 rounded border bg-card">
                <div>
                  <span className="text-muted-foreground mr-1.5">小节:</span>
                  <span className="font-semibold text-primary">
                    {selectedResult.heading_path.join(' > ')}
                  </span>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-6 text-[11px]"
                  onClick={() => setReadFullNote((prev) => !prev)}
                >
                  {readFullNote ? '仅看匹配小节' : '查看完整会话'}
                </Button>
              </div>
            )}

            {/* Source Anchors Display */}
            {selectedResult?.source_anchors && selectedResult.source_anchors.length > 0 && (
              <div className="space-y-1">
                <span className="text-muted-foreground font-medium">{t('conversations.sourceAnchors')}:</span>
                <div className="flex flex-wrap gap-1.5">
                  {selectedResult.source_anchors.map((anchor, i) => (
                    <Badge key={i} variant="secondary" className="font-mono text-[11px]">
                      ordinal: {anchor.ordinal ?? '—'}
                      {anchor.message_id ? ` | ${anchor.message_id}` : ''}
                      {anchor.turn_id ? ` | ${anchor.turn_id}` : ''}
                    </Badge>
                  ))}
                </div>
              </div>
            )}

            {/* Note Reader Body */}
            <div className="space-y-1 pt-2">
              <div className="flex items-center justify-between">
                <span className="font-semibold text-sm text-foreground">
                  {readFullNote ? 'Markdown 完整文档' : '小节正文内容'}
                </span>
                {readQuery.data?.content && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 text-[11px]"
                    onClick={() => handleCopy(readQuery.data!.content, 'Content')}
                  >
                    {copiedText === 'Content' ? <Check className="size-3 mr-1" /> : <Copy className="size-3 mr-1" />}
                    复制内容
                  </Button>
                )}
              </div>

              {readQuery.isLoading ? (
                <div className="py-12 text-center text-muted-foreground">
                  <RefreshCw className="size-5 animate-spin mx-auto mb-2" />
                  {t('common.loading')}
                </div>
              ) : readQuery.isError ? (
                <div className="p-4 rounded border border-destructive/40 bg-destructive/10 text-destructive text-xs">
                  读取文件失败：无法访问指定路径或文件不存在。
                </div>
              ) : (
                <div className="relative">
                  <pre className="p-4 bg-muted/30 rounded-lg text-xs font-mono whitespace-pre-wrap max-h-[55vh] overflow-y-auto border border-border leading-relaxed">
                    {readQuery.data?.content || selectedResult?.content}
                  </pre>
                </div>
              )}
            </div>
          </div>
        </SheetContent>
      </Sheet>
    </div>
  )
}
