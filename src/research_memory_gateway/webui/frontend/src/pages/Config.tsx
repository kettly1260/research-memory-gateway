import { useState } from 'react'
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
import { Check, X, Loader2, Wifi, RefreshCw } from 'lucide-react'
import { useEffectiveConfig, usePatchWebConfig, usePatchSecrets, useTestConnection, useFetchModels } from '@/lib/query'
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
        onSuccess: () => toast.success('Secrets saved'),
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
          toast.success(`Found ${data.models.length} models`)
        } else {
          toast.error(data.status || 'Failed')
        }
      },
    })
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-2">
          <Label>Enabled</Label>
          <Select value={get('enabled')} onValueChange={(v) => { if (v !== null) set('enabled', v) }}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="true">Enabled</SelectItem>
              <SelectItem value="false">Disabled</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-2">
          <Label>Base URL</Label>
          <Input value={get('base_url')} onChange={(e) => set('base_url', e.target.value)} placeholder="http://localhost:11434" />
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-2">
          <Label>Model</Label>
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
          <Label>Endpoint Path</Label>
          <Input value={get('endpoint_path')} onChange={(e) => set('endpoint_path', e.target.value)} placeholder={`/${provider === 'embedding' ? 'embeddings' : 'rerank'}`} />
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <div className="space-y-2">
          <Label>Timeout (s)</Label>
          <Input type="number" value={get('timeout_seconds')} onChange={(e) => set('timeout_seconds', e.target.value)} />
        </div>
        <div className="space-y-2">
          <Label>Max Retries</Label>
          <Input type="number" value={get('max_retries')} onChange={(e) => set('max_retries', e.target.value)} />
        </div>
        <div className="space-y-2">
          <Label>API Key</Label>
          <Input type="password" value={get('api_key')} onChange={(e) => set('api_key', e.target.value)} placeholder="••••••••" autoComplete="off" />
          <Badge variant="outline" className="text-[10px]">
            {effective.api_key?.source || 'default'}
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

        <TabsContent value="retrieval" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t('config.tab_retrieval')}</CardTitle>
              <CardDescription>Choose between keyword-only or hybrid (keyword + vector) retrieval.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label>Retrieval Mode</Label>
                <Select value={effectiveMode} onValueChange={setRetrievalMode}>
                  <SelectTrigger className="w-[200px]"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="keyword">Keyword</SelectItem>
                    <SelectItem value="hybrid">Hybrid</SelectItem>
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
        </TabsContent>

        <TabsContent value="embedding" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t('config.tab_embedding')}</CardTitle>
              <CardDescription>Configure the embedding model for vector search.</CardDescription>
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
              <CardDescription>Configure the reranking model for improved search quality.</CardDescription>
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
              <CardDescription>Nocturne v1 reserved configuration. Sync/import APIs return not_implemented.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-2">
                  <Label>Transport</Label>
                  <Select defaultValue={String(config.nocturne.transport.value)}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="unknown">Unknown</SelectItem>
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
