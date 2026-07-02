import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, Link } from '@tanstack/react-router'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { ArrowLeft, Plus, AlertCircle } from 'lucide-react'
import { useCreateMemory, useTaxonomy } from '@/lib/query'
import { ApiError } from '@/lib/api'
import { MEMORY_TYPES, formatMemoryType } from '@/constants/memoryTypes'
import { toast } from 'sonner'

export function MemoryNew() {
  const { t, i18n } = useTranslation()
  const navigate = useNavigate()
  const createMutation = useCreateMemory()
  const { data: taxonomy } = useTaxonomy()

  const [form, setForm] = useState({
    project: '',
    topic: '',
    memory_type: 'paper_note',
    plan_status: 'draft',
    plan_type: '',
    title: '',
    summary: '',
    tags: '',
  })
  const [jsonMode, setJsonMode] = useState(false)
  const [jsonContent, setJsonContent] = useState('')
  const [overlaps, setOverlaps] = useState<Array<{ memory_id: string; title: string; similarity: number }>>([])

  const handleChange = (field: string, value: string) => {
    setForm((f) => ({ ...f, [field]: value }))
  }

  const handleSubmit = (confirmed = false) => {
    let data: Record<string, unknown>
    if (jsonMode) {
      try {
        data = JSON.parse(jsonContent)
      } catch {
        toast.error(t('common.invalid_json'))
        return
      }
    } else {
      const selectedType = taxonomy?.memory_types.find((item) => item.key === form.memory_type)
      const metadata: Record<string, string> = {}
      if (selectedType?.requires_plan_status) {
        metadata.plan_status = form.plan_status
      }
      if (form.plan_type) {
        metadata.plan_type = form.plan_type
      }
      data = {
        ...form,
        metadata,
        tags: form.tags.split(',').map((t) => t.trim()).filter(Boolean),
      }
      delete data.plan_status
      delete data.plan_type
    }

    if (confirmed) data.confirmed = true

    createMutation.mutate(data as Parameters<typeof createMutation.mutate>[0], {
      onSuccess: (memory) => {
        toast.success(t('memories.created_success'))
        navigate({ to: `/memories/${memory.memory_id}` as string })
      },
      onError: (err) => {
        if (err instanceof ApiError && err.body.error === 'overlap_confirmation_required') {
          setOverlaps(err.body.overlap_candidates as typeof overlaps || [])
          return
        }
        toast.error(String(err))
      },
    })
  }

  const memoryTypeKeys = taxonomy?.memory_types.map((item) => item.key) || [...MEMORY_TYPES]
  const selectedMemoryType = taxonomy?.memory_types.find((item) => item.key === form.memory_type)
  const requiresPlanStatus = !!selectedMemoryType?.requires_plan_status

  return (
    <div className="p-6 md:p-8 max-w-3xl mx-auto space-y-6 animate-fade-in">
      <Link to="/memories" className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors">
        <ArrowLeft className="w-4 h-4" />
        {t('common.back')}
      </Link>

      <h1 className="text-2xl font-bold tracking-tight">{t('memories.new_memory')}</h1>

      {/* Mode toggle */}
      <div className="flex gap-2">
        <Button variant={jsonMode ? 'outline' : 'default'} size="sm" onClick={() => setJsonMode(false)}>
          {t('memories.form')}
        </Button>
        <Button variant={jsonMode ? 'default' : 'outline'} size="sm" onClick={() => setJsonMode(true)}>
          JSON
        </Button>
      </div>

      {/* Overlap warning */}
      {overlaps.length > 0 && (
        <Card className="border-amber-500/50 bg-amber-500/5">
          <CardContent className="py-4 space-y-3">
            <div className="flex items-center gap-2 text-amber-600 dark:text-amber-400">
              <AlertCircle className="w-4 h-4" />
              <span className="text-sm font-medium">{t('memories.overlaps_detected')}</span>
            </div>
            {overlaps.map((o) => (
              <div key={o.memory_id} className="flex items-center justify-between text-sm">
                <span className="truncate">{o.title}</span>
                <Badge variant="outline" className="text-[10px]">{(o.similarity * 100).toFixed(0)}%</Badge>
              </div>
            ))}
            <Button size="sm" onClick={() => handleSubmit(true)} disabled={createMutation.isPending}>
              {t('memories.confirm_save_anyway')}
            </Button>
          </CardContent>
        </Card>
      )}

      {jsonMode ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">{t('memories.json_editor')}</CardTitle>
          </CardHeader>
          <CardContent>
            <Textarea
              value={jsonContent}
              onChange={(e) => setJsonContent(e.target.value)}
              className="font-mono text-xs min-h-[400px]"
              placeholder='{"project":"demo","topic":"...","memory_type":"paper_note","title":"...","summary":"..."}'
            />
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="pt-6 space-y-4">
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-2">
                <Label>{t('memories.col_project')}</Label>
                <Input
                  value={form.project}
                  onChange={(e) => handleChange('project', e.target.value)}
                  placeholder={t('memories.placeholder_project')}
                  required
                />
              </div>
              <div className="space-y-2">
                <Label>{t('memories.col_topic')}</Label>
                <Input
                  value={form.topic}
                  onChange={(e) => handleChange('topic', e.target.value)}
                  placeholder={t('memories.placeholder_topic')}
                  required
                />
              </div>
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-2">
                <Label>{t('memories.col_type')}</Label>
                <Select value={form.memory_type} onValueChange={(v) => { if (v !== null) handleChange('memory_type', v) }}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {memoryTypeKeys.map((type) => (
                      <SelectItem key={type} value={type}>{formatMemoryType(type, taxonomy?.memory_types, i18n.language)}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label>{t('memories.tags_comma')}</Label>
                <Input
                  value={form.tags}
                  onChange={(e) => handleChange('tags', e.target.value)}
                  placeholder={t('memories.placeholder_tags')}
                />
              </div>
            </div>
            {(requiresPlanStatus || form.plan_type) && (
              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-2">
                  <Label>{t('taxonomy.plan_status')}</Label>
                  <Select value={form.plan_status} onValueChange={(v) => { if (v !== null) handleChange('plan_status', v) }}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {(taxonomy?.plan_statuses || []).map((status) => (
                        <SelectItem key={status.key} value={status.key}>
                          {i18n.language.startsWith('zh') ? status.label_zh : status.label_en}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label>{t('taxonomy.plan_type')}</Label>
                  <Select value={form.plan_type || '_none'} onValueChange={(v) => { if (v !== null) handleChange('plan_type', v === '_none' ? '' : v) }}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="_none">{t('common.no')}</SelectItem>
                      {(taxonomy?.plan_types || []).map((planType) => (
                        <SelectItem key={planType.key} value={planType.key}>
                          {i18n.language.startsWith('zh') ? planType.label_zh : planType.label_en}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
            )}
            <div className="space-y-2">
              <Label>{t('memories.col_title')}</Label>
              <Input
                value={form.title}
                onChange={(e) => handleChange('title', e.target.value)}
                placeholder={t('memories.placeholder_title')}
                required
              />
            </div>
            <div className="space-y-2">
              <Label>{t('memories.summary')}</Label>
              <Textarea
                value={form.summary}
                onChange={(e) => handleChange('summary', e.target.value)}
                placeholder={t('memories.placeholder_summary')}
                className="min-h-[120px]"
                required
              />
            </div>
          </CardContent>
        </Card>
      )}

      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={() => navigate({ to: '/memories' })}>
          {t('common.cancel')}
        </Button>
        <Button onClick={() => handleSubmit()} disabled={createMutation.isPending}>
          <Plus className="w-4 h-4 mr-2" />
          {createMutation.isPending ? t('common.saving') : t('common.create')}
        </Button>
      </div>
    </div>
  )
}
