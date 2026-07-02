import { useState, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Check,
  X,
  Loader2,
  Wifi,
  RefreshCw,
  Play,
  Square,
  ChevronDown,
  ChevronUp,
  Database,
  AlertTriangle
} from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import {
  useEffectiveConfig,
  usePatchWebConfig,
  usePatchSecrets,
  useTestConnection,
  useFetchModels,
  useVectorCoverage,
  useBackfillJob,
  useBackfillStart,
  useBackfillCancel,
  useProjects,
  useTaxonomy
} from '@/lib/query'
import { api } from '@/lib/api'
import { MEMORY_TYPES, formatMemoryType } from '@/constants/memoryTypes'
import { toast } from 'sonner'

function ConnectionStatus({ result }: { result: { ok: boolean; status: string; latency_ms?: number; error?: string } | null }) {
  if (!result) return null
  return (
    <div className={`flex items-center gap-2 mt-2 text-sm animate-fade-in ${result.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-destructive'}`}>
      {result.ok ? <Check className="w-4 h-4" /> : <X className="w-4 h-4" />}
      <span>{result.status}</span>
      {result.latency_ms && <Badge variant="outline" className="text-[10px]">{result.latency_ms}ms</Badge>}
      {result.error && <span className="text-xs">{result.error}</span>}
    </div>
  )
}

function ProviderForm({ provider, effective }: {
  provider: 'embedding' | 'rerank'
  effective: Record<string, { value: unknown; source: string }>
}) {
  const { t } = useTranslation()
  const patchWebConfig = usePatchWebConfig()
  const patchSecrets = usePatchSecrets()
  const testConnection = useTestConnection()
  const fetchModels = useFetchModels(provider)
  const [models, setModels] = useState<string[]>([])
  const [form, setForm] = useState<Record<string, string>>({})

  const get = (field: string) => form[`${provider}.${field}`] ?? String(effective[field]?.value ?? '')
  const set = (field: string, value: string) => setForm((f) => ({ ...f, [`${provider}.${field}`]: value }))

  const handleSave = () => {
    const configFields: Record<string, unknown> = {}
    const secretFields: Record<string, string> = {}

    Object.entries(form).forEach(([key, value]) => {
      if (key.endsWith('.api_key')) {
        if (value) secretFields[key] = value
      } else {
        // Convert booleans and numbers
        if (key.endsWith('.enabled')) {
          configFields[key] = value === 'true'
        } else if (key.endsWith('.timeout_seconds') || key.endsWith('.max_retries')) {
          configFields[key] = parseInt(value, 10)
        } else {
          configFields[key] = value
        }
      }
    })

    if (Object.keys(configFields).length > 0) {
      patchWebConfig.mutate(configFields, {
        onSuccess: () => toast.success(t('common.success')),
        onError: (err) => toast.error(String(err)),
      })
    }
    if (Object.keys(secretFields).length > 0) {
      patchSecrets.mutate(secretFields, {
        onSuccess: () => toast.success(t('config.secrets_saved')),
        onError: (err) => toast.error(String(err)),
      })
    }
  }

  const handleTest = () => {
    testConnection.mutate(provider)
  }

  const handleFetchModels = () => {
    fetchModels.mutate(get('base_url') || undefined, {
      onSuccess: (data) => {
        if (data.ok) {
          setModels(data.models)
          toast.success(t('config.models_found', { count: data.models.length }))
        } else {
          toast.error(data.status || t('config.failed'))
        }
      },
    })
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-2">
          <Label>{t('config.enabled')}</Label>
          <Select value={get('enabled')} onValueChange={(v) => { if (v !== null) set('enabled', v) }}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="true">{t('config.enabled')}</SelectItem>
              <SelectItem value="false">{t('config.disabled')}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-2">
          <Label>{t('config.base_url')}</Label>
          <Input value={get('base_url')} onChange={(e) => set('base_url', e.target.value)} placeholder="http://localhost:11434" />
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-2">
          <Label>{t('config.model')}</Label>
          <div className="flex gap-2">
            {models.length > 0 ? (
              <Select value={get('model')} onValueChange={(v) => { if (v !== null) set('model', v) }}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {models.map((m) => <SelectItem key={m} value={m}>{m}</SelectItem>)}
                </SelectContent>
              </Select>
            ) : (
              <Input value={get('model')} onChange={(e) => set('model', e.target.value)} />
            )}
            <Button variant="outline" size="sm" onClick={handleFetchModels} disabled={fetchModels.isPending}>
              {fetchModels.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            </Button>
          </div>
        </div>
        <div className="space-y-2">
          <Label>{t('config.endpoint_path')}</Label>
          <Input value={get('endpoint_path')} onChange={(e) => set('endpoint_path', e.target.value)} placeholder={`/${provider === 'embedding' ? 'embeddings' : 'rerank'}`} />
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <div className="space-y-2">
          <Label>{t('config.timeout_seconds')}</Label>
          <Input type="number" value={get('timeout_seconds')} onChange={(e) => set('timeout_seconds', e.target.value)} />
        </div>
        <div className="space-y-2">
          <Label>{t('config.max_retries')}</Label>
          <Input type="number" value={get('max_retries')} onChange={(e) => set('max_retries', e.target.value)} />
        </div>
        <div className="space-y-2">
          <Label>{t('config.api_key')}</Label>
          <Input type="password" value={get('api_key')} onChange={(e) => set('api_key', e.target.value)} placeholder="••••••••" autoComplete="off" />
          <Badge variant="outline" className="text-[10px]">
            {effective.api_key?.source || t('config.source_default')}
          </Badge>
        </div>
      </div>

      <div className="flex items-center gap-2 pt-2">
        <Button onClick={handleSave} disabled={patchWebConfig.isPending || patchSecrets.isPending}>
          {t('common.save')}
        </Button>
        <Button variant="outline" onClick={handleTest} disabled={testConnection.isPending}>
          {testConnection.isPending ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : <Wifi className="w-4 h-4 mr-2" />}
          {t('config.test_connection')}
        </Button>
      </div>

      <ConnectionStatus result={testConnection.data ?? null} />
    </div>
  )
}

function VectorBackfillSection() {
  const { t, i18n } = useTranslation()
  const queryClient = useQueryClient()
  const { data: coverage, isLoading: isCoverageLoading, refetch: refetchCoverage } = useVectorCoverage()
  const { data: projects } = useProjects()
  const { data: taxonomy } = useTaxonomy()
  const memoryTypeKeys = taxonomy?.memory_types.map((item) => item.key) || [...MEMORY_TYPES]

  // Form states
  const [scope, setScope] = useState<'active' | 'active_archived' | 'all'>('active')
  const [project, setProject] = useState<string>('all')
  const [memoryType, setMemoryType] = useState<string>('all')
  const [limitType, setLimitType] = useState<'all' | 'custom'>('custom')
  const [limitValue, setLimitValue] = useState<number>(100)
  const [force, setForce] = useState<boolean>(false)

  // Advanced options toggle
  const [showAdvanced, setShowAdvanced] = useState<boolean>(false)
  // Advanced options states
  const [concurrency, setConcurrency] = useState<number>(2)
  const [batchSize, setBatchSize] = useState<number>(8)
  const [requestTimeout, setRequestTimeout] = useState<number>(30)
  const [jobTimeout, setJobTimeout] = useState<number>(1800)

  // Job states
  const [activeJobId, setActiveJobId] = useState<string | null>(null)
  const [dryRunResult, setDryRunResult] = useState<{ total: number; memory_ids: string[] } | null>(null)
  const [isDryRunning, setIsDryRunning] = useState<boolean>(false)

  // Hooks
  const backfillStart = useBackfillStart()
  const backfillCancel = useBackfillCancel()
  const { data: job } = useBackfillJob(activeJobId)
  const jobStatus = job?.status

  // Effect to handle job completion/cancellation
  useEffect(() => {
    if (jobStatus === 'completed' || jobStatus === 'failed' || jobStatus === 'cancelled') {
      queryClient.invalidateQueries({ queryKey: ['retrieval', 'vector-coverage'] })
      queryClient.invalidateQueries({ queryKey: ['stats'] })
    }
  }, [jobStatus, queryClient])

  const getParams = () => {
    return {
      scope,
      project: project === 'all' ? undefined : project,
      memory_type: memoryType === 'all' ? undefined : memoryType,
      limit: limitType === 'all' ? 'all' : limitValue,
      force,
      concurrency,
      batch_size: batchSize,
      request_timeout_seconds: requestTimeout,
      job_timeout_seconds: jobTimeout,
    }
  }

  const handleDryRun = async () => {
    setIsDryRunning(true)
    setDryRunResult(null)
    try {
      const res = await api.retrieval.backfillDryRun(getParams())
      setDryRunResult(res)
      toast.success(t('config.dry_run_results', { total: res.total }))
    } catch (err) {
      toast.error(t('config.dry_run_failed', { error: String(err) }))
    } finally {
      setIsDryRunning(false)
    }
  }

  const handleStartBackfill = () => {
    setDryRunResult(null)
    backfillStart.mutate(getParams(), {
      onSuccess: (data) => {
        setActiveJobId(data.job_id)
        toast.success(t('config.backfill_started'))
      },
      onError: (err) => {
        toast.error(t('config.start_job_failed', { error: String(err) }))
      },
    })
  }

  const handleCancelBackfill = () => {
    if (!activeJobId) return
    backfillCancel.mutate(activeJobId, {
      onSuccess: () => {
        toast.info(t('config.cancellation_sent'))
      },
      onError: (err) => {
        toast.error(t('config.cancel_job_failed', { error: String(err) }))
      },
    })
  }

  // Calculate coverage stats
  const total = coverage?.total ?? 0
  const embedded = coverage?.embedded ?? 0
  const missing = coverage?.missing ?? 0
  const pct = total > 0 ? Math.round((embedded / total) * 100) : 100

  // Calculate active job stats
  const isJobRunning = job && job.status === 'running'
  const jobTotal = job?.total ?? 0
  const jobProcessed = (job?.completed ?? 0) + (job?.failed ?? 0) + (job?.skipped ?? 0)
  const jobPct = jobTotal > 0 ? Math.round((jobProcessed / jobTotal) * 100) : 0

  return (
    <Card className="shadow-md border border-slate-200/60 dark:border-slate-800/80 transition-all duration-300 hover:shadow-lg">
      <CardHeader>
        <div className="flex items-center gap-3">
          <div className="p-2 bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 rounded-lg">
            <Database className="w-5 h-5" />
          </div>
          <div>
            <CardTitle className="text-base">{t('config.vector_backfill_title')}</CardTitle>
            <CardDescription className="text-xs mt-1">{t('config.vector_backfill_desc')}</CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Coverage Section */}
        <div className="bg-slate-50 dark:bg-slate-900/40 p-4 rounded-xl border border-slate-100 dark:border-slate-800/50">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-semibold text-slate-700 dark:text-slate-300">
              {t('config.vector_coverage')}
            </span>
            <div className="flex items-center gap-2">
              <span className="text-sm font-bold text-blue-600 dark:text-blue-400">{pct}%</span>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 w-7 p-0"
                onClick={() => { refetchCoverage() }}
                disabled={isCoverageLoading}
              >
                <RefreshCw className={`w-3.5 h-3.5 ${isCoverageLoading ? 'animate-spin' : ''}`} />
              </Button>
            </div>
          </div>

          <div className="w-full bg-slate-200 dark:bg-slate-800 rounded-full h-3 overflow-hidden shadow-inner">
            <div
              className="bg-gradient-to-r from-blue-500 via-indigo-500 to-violet-600 h-3 rounded-full transition-all duration-500 ease-out"
              style={{ width: `${pct}%` }}
            />
          </div>

          <div className="grid grid-cols-3 gap-2 mt-3 text-center text-xs text-slate-500 dark:text-slate-400">
            <div className="bg-white dark:bg-slate-900 p-2 rounded-lg border border-slate-100 dark:border-slate-800/80">
              <div className="font-semibold text-slate-800 dark:text-slate-200">{total}</div>
              <div>{t('config.total_memories')}</div>
            </div>
            <div className="bg-white dark:bg-slate-900 p-2 rounded-lg border border-slate-100 dark:border-slate-800/80">
              <div className="font-semibold text-emerald-600 dark:text-emerald-400">{embedded}</div>
              <div>{t('config.embedded_memories')}</div>
            </div>
            <div className="bg-white dark:bg-slate-900 p-2 rounded-lg border border-slate-100 dark:border-slate-800/80">
              <div className="font-semibold text-amber-600 dark:text-amber-500">{missing}</div>
              <div>{t('config.missing_embeddings')}</div>
            </div>
          </div>
        </div>

        {/* Backfill Config Form */}
        <div className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="backfill-scope" className="text-xs font-semibold text-slate-500 dark:text-slate-400">{t('config.backfill_scope')}</Label>
              <Select value={scope} onValueChange={(v: string | null) => {
                if (v === 'active' || v === 'active_archived' || v === 'all') setScope(v)
              }}>
                <SelectTrigger id="backfill-scope" className="h-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="active">{t('config.backfill_scope_active')}</SelectItem>
                  <SelectItem value="active_archived">{t('config.backfill_scope_active_archived')}</SelectItem>
                  <SelectItem value="all">{t('config.backfill_scope_all')}</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="backfill-project" className="text-xs font-semibold text-slate-500 dark:text-slate-400">{t('config.backfill_project')}</Label>
              <Select value={project} onValueChange={(v: string | null) => { if (v !== null) setProject(v) }}>
                <SelectTrigger id="backfill-project" className="h-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t('config.backfill_project_all')}</SelectItem>
                  {projects?.map((p) => (
                    <SelectItem key={p} value={p}>{p}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="backfill-type" className="text-xs font-semibold text-slate-500 dark:text-slate-400">{t('config.backfill_type')}</Label>
              <Select value={memoryType} onValueChange={(v: string | null) => { if (v !== null) setMemoryType(v) }}>
                <SelectTrigger id="backfill-type" className="h-9">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t('config.backfill_type_all')}</SelectItem>
                  {memoryTypeKeys.map((type) => (
                    <SelectItem key={type} value={type}>{formatMemoryType(type, taxonomy?.memory_types, i18n.language)}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs font-semibold text-slate-500 dark:text-slate-400">{t('config.backfill_limit')}</Label>
              <div className="flex gap-4 items-center h-9">
                <Select
                  value={limitType}
                  onValueChange={(v: string | null) => {
                    if (v === 'all' || v === 'custom') setLimitType(v)
                  }}
                >
                  <SelectTrigger className="w-1/2 h-9">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="custom">{t('config.backfill_limit')}</SelectItem>
                    <SelectItem value="all">{t('config.backfill_limit_all')}</SelectItem>
                  </SelectContent>
                </Select>
                {limitType === 'custom' && (
                  <Input
                    type="number"
                    min={1}
                    max={1000}
                    className="w-1/2 h-9"
                    value={limitValue}
                    onChange={(e) => { setLimitValue(parseInt(e.target.value, 10) || 100) }}
                  />
                )}
              </div>
            </div>
          </div>

          {/* Force Checkbox */}
          <div className="flex items-center space-x-2 bg-slate-50 dark:bg-slate-900/30 p-2.5 rounded-lg border border-slate-100/50 dark:border-slate-800/50">
            <input
              id="backfill-force"
              type="checkbox"
              checked={force}
              onChange={(e) => { setForce(e.target.checked) }}
              className="h-4 w-4 rounded border-slate-300 dark:border-slate-700 text-blue-600 focus:ring-blue-500"
            />
            <Label htmlFor="backfill-force" className="text-xs font-medium cursor-pointer text-slate-700 dark:text-slate-300">
              {t('config.backfill_force')}
            </Label>
          </div>

          {/* Advanced options accordion */}
          <div className="border border-slate-200/50 dark:border-slate-800/80 rounded-lg overflow-hidden">
            <button
              type="button"
              className="w-full flex items-center justify-between p-3 bg-slate-50/50 dark:bg-slate-900/10 text-xs font-semibold text-slate-700 dark:text-slate-300 transition-colors hover:bg-slate-100/40"
              onClick={() => { setShowAdvanced(!showAdvanced) }}
            >
              <span className="flex items-center gap-1.5">
                {t('config.advanced_params')}
              </span>
              {showAdvanced ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
            </button>

            {showAdvanced && (
              <div className="p-4 bg-white dark:bg-slate-900/20 border-t border-slate-100 dark:border-slate-800/60 grid gap-4 sm:grid-cols-2 animate-fade-in">
                <div className="space-y-1.5">
                  <Label className="text-xs font-medium">{t('config.concurrency')}</Label>
                  <Select value={String(concurrency)} onValueChange={(v: string | null) => { if (v !== null) setConcurrency(parseInt(v, 10)) }}>
                    <SelectTrigger className="h-8 text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="1">1</SelectItem>
                      <SelectItem value="2">2 ({t('config.default_option')})</SelectItem>
                      <SelectItem value="3">3</SelectItem>
                      <SelectItem value="4">4</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs font-medium">{t('config.batch_size')}</Label>
                  <Input
                    type="number"
                    min={1}
                    max={32}
                    className="h-8 text-xs"
                    value={batchSize}
                    onChange={(e) => { setBatchSize(parseInt(e.target.value, 10) || 8) }}
                  />
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs font-medium">{t('config.request_timeout')}</Label>
                  <Input
                    type="number"
                    min={5}
                    max={120}
                    className="h-8 text-xs"
                    value={requestTimeout}
                    onChange={(e) => { setRequestTimeout(parseInt(e.target.value, 10) || 30) }}
                  />
                </div>

                <div className="space-y-1.5">
                  <Label className="text-xs font-medium">{t('config.job_timeout')}</Label>
                  <Input
                    type="number"
                    min={60}
                    max={86400}
                    className="h-8 text-xs"
                    value={jobTimeout}
                    onChange={(e) => { setJobTimeout(parseInt(e.target.value, 10) || 1800) }}
                  />
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Action Buttons */}
        <div className="flex flex-wrap items-center gap-3">
          <Button
            variant="outline"
            onClick={handleDryRun}
            disabled={isDryRunning || isJobRunning}
            className="flex-1 sm:flex-initial h-9"
          >
            {isDryRunning ? (
              <Loader2 className="w-4 h-4 animate-spin mr-2" />
            ) : (
              <Check className="w-4 h-4 mr-2" />
            )}
            {t('config.dry_run_btn')}
          </Button>

          <Button
            onClick={handleStartBackfill}
            disabled={isJobRunning || backfillStart.isPending}
            className="flex-1 sm:flex-initial bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white border-0 shadow h-9"
          >
            {backfillStart.isPending ? (
              <Loader2 className="w-4 h-4 animate-spin mr-2" />
            ) : (
              <Play className="w-4 h-4 mr-2" />
            )}
            {t('config.start_backfill_btn')}
          </Button>
        </div>

        {/* Dry Run Results Banner */}
        {dryRunResult !== null && (
          <div className="bg-blue-50/50 dark:bg-blue-900/10 p-3 rounded-lg border border-blue-100 dark:border-blue-900/20 text-xs text-blue-700 dark:text-blue-300 animate-fade-in">
            {t('config.dry_run_results', { total: dryRunResult.total })}
          </div>
        )}

        {/* Active Job Progress */}
        {job && (
          <div className="bg-slate-50 dark:bg-slate-900/50 p-4 rounded-xl border border-slate-100 dark:border-slate-800/80 space-y-4 animate-fade-in">
            <div className="flex items-center justify-between">
              <span className="text-sm font-semibold flex items-center gap-2">
                {isJobRunning && (
                  <span className="flex h-2.5 w-2.5 relative">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75"></span>
                    <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-blue-500"></span>
                  </span>
                )}
                {t('config.job_status')}
              </span>
              <Badge
                variant={job.status === 'running' ? 'secondary' : job.status === 'failed' ? 'destructive' : 'outline'}
                className={`text-[10px] font-bold ${
                  job.status === 'completed' || job.status === 'completed_with_errors'
                    ? 'bg-emerald-500/10 text-emerald-600 dark:bg-emerald-500/20 dark:text-emerald-400 border-emerald-500/30'
                    : ''
                }`}
              >
                {job.status === 'running'
                  ? t('config.job_running')
                  : job.status === 'completed'
                  ? t('config.job_completed')
                  : job.status === 'cancelled'
                  ? t('config.job_cancelled')
                  : job.status === 'completed_with_errors'
                  ? t('config.job_completed_errors')
                  : t('config.job_failed')}
              </Badge>
            </div>

            <div className="space-y-1.5">
              <div className="flex justify-between text-xs text-slate-500 dark:text-slate-400">
                <span>{t('config.progress')}</span>
                <span>
                  {jobProcessed} / {jobTotal} ({jobPct}%)
                </span>
              </div>
              <div className="w-full bg-slate-200 dark:bg-slate-800 rounded-full h-2 overflow-hidden">
                <div
                  className={`h-2 rounded-full transition-all duration-300 ease-out ${
                    job.status === 'failed' ? 'bg-destructive' : job.status === 'cancelled' ? 'bg-amber-500' : 'bg-blue-600 dark:bg-blue-500'
                  }`}
                  style={{ width: `${jobPct}%` }}
                />
              </div>
            </div>

            <div className="grid grid-cols-3 gap-2 text-center text-xs">
              <div className="bg-white dark:bg-slate-900/80 p-2 rounded-lg border border-slate-100 dark:border-slate-800/50">
                <div className="font-semibold text-emerald-600 dark:text-emerald-400">
                  {job.completed}
                </div>
                <div className="text-[10px] text-slate-400 mt-0.5">{t('config.stats_completed', { count: job.completed })}</div>
              </div>
              <div className="bg-white dark:bg-slate-900/80 p-2 rounded-lg border border-slate-100 dark:border-slate-800/50">
                <div className="font-semibold text-destructive">
                  {job.failed}
                </div>
                <div className="text-[10px] text-slate-400 mt-0.5">{t('config.stats_failed', { count: job.failed })}</div>
              </div>
              <div className="bg-white dark:bg-slate-900/80 p-2 rounded-lg border border-slate-100 dark:border-slate-800/50">
                <div className="font-semibold text-slate-500">
                  {job.skipped}
                </div>
                <div className="text-[10px] text-slate-400 mt-0.5">{t('config.stats_skipped', { count: job.skipped })}</div>
              </div>
            </div>

            {job.last_error && (
              <div className="flex gap-2 p-3 bg-destructive/10 dark:bg-destructive/20 border border-destructive/20 rounded-lg text-xs text-destructive">
                <AlertTriangle className="w-4 h-4 shrink-0" />
                <div className="space-y-0.5">
                  <div className="font-semibold">{t('config.last_error')}</div>
                  <div>{job.last_error}</div>
                </div>
              </div>
            )}

            {isJobRunning && (
              <div className="flex justify-end pt-1">
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={handleCancelBackfill}
                  disabled={backfillCancel.isPending}
                  className="h-8 px-3 text-xs"
                >
                  <Square className="w-3.5 h-3.5 mr-1.5 fill-current" />
                  {t('config.cancel_backfill_btn')}
                </Button>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export function Config() {
  const { t } = useTranslation()
  const { data: config, isLoading } = useEffectiveConfig()
  const patchWebConfig = usePatchWebConfig()
  const testConnection = useTestConnection()
  const [retrievalMode, setRetrievalMode] = useState<string | null>(null)

  if (isLoading || !config) {
    return (
      <div className="p-6 md:p-8 max-w-5xl mx-auto space-y-4 animate-fade-in">
        <div className="skeleton h-8 w-48" />
        <div className="skeleton h-64 w-full" />
      </div>
    )
  }

  const effectiveMode = retrievalMode ?? String(config.retrieval.mode.value)

  return (
    <div className="p-6 md:p-8 max-w-5xl mx-auto space-y-6 animate-fade-in">
      <h1 className="text-2xl font-bold tracking-tight">{t('config.title')}</h1>

      <Tabs defaultValue="retrieval">
        <TabsList>
          <TabsTrigger value="retrieval">{t('config.tab_retrieval')}</TabsTrigger>
          <TabsTrigger value="embedding">{t('config.tab_embedding')}</TabsTrigger>
          <TabsTrigger value="rerank">{t('config.tab_rerank')}</TabsTrigger>
          <TabsTrigger value="nocturne">{t('config.tab_nocturne')}</TabsTrigger>
        </TabsList>

        <TabsContent value="retrieval" className="mt-4 space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t('config.tab_retrieval')}</CardTitle>
              <CardDescription>{t('config.retrieval_desc')}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label>{t('config.retrieval_mode')}</Label>
                <Select value={effectiveMode} onValueChange={(v: string | null) => { if (v !== null) setRetrievalMode(v) }}>
                  <SelectTrigger className="w-[200px]"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="keyword">{t('config.keyword')}</SelectItem>
                    <SelectItem value="hybrid">{t('config.hybrid')}</SelectItem>
                  </SelectContent>
                </Select>
                <Badge variant="outline" className="text-[10px]">{config.retrieval.mode.source}</Badge>
              </div>
              <Button
                onClick={() => {
                  patchWebConfig.mutate({ 'retrieval.mode': retrievalMode }, {
                    onSuccess: () => toast.success(t('common.success')),
                  })
                }}
                disabled={patchWebConfig.isPending}
              >
                {t('common.save')}
              </Button>
            </CardContent>
          </Card>
          <VectorBackfillSection />
        </TabsContent>

        <TabsContent value="embedding" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t('config.tab_embedding')}</CardTitle>
              <CardDescription>{t('config.embedding_desc')}</CardDescription>
            </CardHeader>
            <CardContent>
              <ProviderForm provider="embedding" effective={config.embedding} />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="rerank" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t('config.tab_rerank')}</CardTitle>
              <CardDescription>{t('config.rerank_desc')}</CardDescription>
            </CardHeader>
            <CardContent>
              <ProviderForm provider="rerank" effective={config.rerank} />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="nocturne" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t('config.tab_nocturne')}</CardTitle>
              <CardDescription>{t('config.nocturne_desc')}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-2">
                  <Label>{t('config.transport')}</Label>
                  <Select defaultValue={String(config.nocturne.transport.value)}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="unknown">{t('config.unknown')}</SelectItem>
                      <SelectItem value="rest">REST</SelectItem>
                      <SelectItem value="sse">SSE</SelectItem>
                      <SelectItem value="streamable_http">Streamable HTTP</SelectItem>
                      <SelectItem value="stdio">Stdio</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label>URL</Label>
                  <Input defaultValue={String(config.nocturne.url.value || '')} />
                </div>
              </div>
              <div className="flex gap-2">
                <Button onClick={() => {
                  testConnection.mutate('nocturne')
                }} disabled={testConnection.isPending}>
                  {testConnection.isPending ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : <Wifi className="w-4 h-4 mr-2" />}
                  {t('config.test_connection')}
                </Button>
              </div>
              <ConnectionStatus result={testConnection.data ?? null} />
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  )
}
