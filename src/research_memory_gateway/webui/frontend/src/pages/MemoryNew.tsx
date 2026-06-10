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
import { useCreateMemory } from '@/lib/query'
import { ApiError } from '@/lib/api'
import { toast } from 'sonner'

const memoryTypes = [
  'research_finding',
  'methodology',
  'tool_usage',
  'domain_knowledge',
  'experimental_result',
  'literature_note',
]

export function MemoryNew() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const createMutation = useCreateMemory()

  const [form, setForm] = useState({
    project: '',
    topic: '',
    memory_type: 'research_finding',
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
        toast.error('Invalid JSON')
        return
      }
    } else {
      data = {
        ...form,
        tags: form.tags.split(',').map((t) => t.trim()).filter(Boolean),
      }
    }

    if (confirmed) data.confirmed = true

    createMutation.mutate(data as Parameters<typeof createMutation.mutate>[0], {
      onSuccess: (memory) => {
        toast.success('Memory created')
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
          Form
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
              <span className="text-sm font-medium">Potential overlaps detected</span>
            </div>
            {overlaps.map((o) => (
              <div key={o.memory_id} className="flex items-center justify-between text-sm">
                <span className="truncate">{o.title}</span>
                <Badge variant="outline" className="text-[10px]">{(o.similarity * 100).toFixed(0)}%</Badge>
              </div>
            ))}
            <Button size="sm" onClick={() => handleSubmit(true)} disabled={createMutation.isPending}>
              Confirm & Save Anyway
            </Button>
          </CardContent>
        </Card>
      )}

      {jsonMode ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">JSON Editor</CardTitle>
          </CardHeader>
          <CardContent>
            <Textarea
              value={jsonContent}
              onChange={(e) => setJsonContent(e.target.value)}
              className="font-mono text-xs min-h-[400px]"
              placeholder='{"project":"demo","topic":"...","memory_type":"research_finding","title":"...","summary":"..."}'
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
                  placeholder="e.g. GenomicsAI"
                  required
                />
              </div>
              <div className="space-y-2">
                <Label>{t('memories.col_topic')}</Label>
                <Input
                  value={form.topic}
                  onChange={(e) => handleChange('topic', e.target.value)}
                  placeholder="e.g. Variant Calling"
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
                    {memoryTypes.map((type) => (
                      <SelectItem key={type} value={type}>{type}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label>Tags (comma-separated)</Label>
                <Input
                  value={form.tags}
                  onChange={(e) => handleChange('tags', e.target.value)}
                  placeholder="e.g. AI, genomics, CRISPR"
                />
              </div>
            </div>
            <div className="space-y-2">
              <Label>{t('memories.col_title')}</Label>
              <Input
                value={form.title}
                onChange={(e) => handleChange('title', e.target.value)}
                placeholder="Memory title"
                required
              />
            </div>
            <div className="space-y-2">
              <Label>Summary</Label>
              <Textarea
                value={form.summary}
                onChange={(e) => handleChange('summary', e.target.value)}
                placeholder="Describe this memory..."
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
