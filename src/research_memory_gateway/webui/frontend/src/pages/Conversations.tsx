import * as React from 'react'
import { useTranslation } from 'react-i18next'
import {
  AlertCircle,
  BookOpen,
  Check,
  Copy,
  FileText,
  Layers,
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
  useConversationSearch,
  useConversationRecall,
  useConversationRead,
} from '@/lib/query'
import type { ConversationSearchResultItem } from '@/types/api'
import { toast } from 'sonner'

export function Conversations() {
  const { t } = useTranslation()

  // ─── Status Query ───
  const statusQuery = useConversationStatus()
  const status = statusQuery.data

  // ─── Search State & Query ───
  const [searchInput, setSearchInput] = React.useState('')
  const [searchProject, setSearchProject] = React.useState('')
  const [searchCid, setSearchCid] = React.useState('')
  const [searchPid, setSearchPid] = React.useState('')
  const [activeSearch, setActiveSearch] = React.useState<{
    query: string
    project?: string
    conversation_id?: string
    parent_thread_id?: string
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
    })
  }

  // ─── Recall State & Query ───
  const [recallInput, setRecallInput] = React.useState('')
  const [tokenBudget, setTokenBudget] = React.useState('1500')
  const [recallProject, setRecallProject] = React.useState('')
  const [recallCid, setRecallCid] = React.useState('')
  const [recallPid, setRecallPid] = React.useState('')
  const [activeRecall, setActiveRecall] = React.useState<{
    query: string
    token_budget?: string
    project?: string
    conversation_id?: string
    parent_thread_id?: string
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
              Read-Only
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
              {status?.embedding_version ?? 'v1'}
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
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 pt-1 text-xs">
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
                        <div>
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
                          <div className="flex items-center gap-2">
                            <span>ID:</span>
                            <code className="font-mono bg-muted/60 px-1 py-0.5 rounded">
                              {item.conversation_id.slice(0, 8)}...
                            </code>
                            {item.parent_thread_id && (
                              <>
                                <span className="ml-1">Parent:</span>
                                <code className="font-mono bg-muted/60 px-1 py-0.5 rounded">
                                  {item.parent_thread_id.slice(0, 8)}
                                </code>
                              </>
                            )}
                            {item.thread_source && (
                              <span className="capitalize">({item.thread_source})</span>
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
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 pt-1 text-xs">
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
