import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Label } from '@/components/ui/label'
import { Download, Loader2, Check } from 'lucide-react'
import { useExport } from '@/lib/query'
import { toast } from 'sonner'

export function ExportsPage() {
  const { t } = useTranslation()
  const [format, setFormat] = useState('both')
  const [includeArchived, setIncludeArchived] = useState(false)
  const [includeDeleted, setIncludeDeleted] = useState(false)
  const [result, setResult] = useState<Record<string, unknown> | null>(null)

  const exportMutation = useExport()

  const handleExport = () => {
    exportMutation.mutate(
      { format, includeArchived, includeDeleted },
      {
        onSuccess: (data) => {
          setResult(data)
          toast.success(t('export.exported_toast', { count: data.count }))
        },
        onError: (err) => toast.error(String(err)),
      },
    )
  }

  return (
    <div className="p-6 md:p-8 max-w-5xl mx-auto space-y-6 animate-fade-in">
      <h1 className="text-2xl font-bold tracking-tight">{t('nav.export')}</h1>

      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Download className="w-4 h-4" />
            {t('export.title')}
          </CardTitle>
          <CardDescription>
            {t('export.desc')}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-2">
              <Label>{t('export.format')}</Label>
              <Select value={format} onValueChange={(v) => { if (v !== null) setFormat(v) }}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="both">{t('export.both')}</SelectItem>
                  <SelectItem value="json">JSON</SelectItem>
                  <SelectItem value="markdown">Markdown</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>{t('export.include_archived')}</Label>
              <Select value={String(includeArchived)} onValueChange={(v) => setIncludeArchived(v === 'true')}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="false">{t('common.no')}</SelectItem>
                  <SelectItem value="true">{t('common.yes')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>{t('export.include_deleted')}</Label>
              <Select value={String(includeDeleted)} onValueChange={(v) => setIncludeDeleted(v === 'true')}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="false">{t('common.no')}</SelectItem>
                  <SelectItem value="true">{t('common.yes')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <Button onClick={handleExport} disabled={exportMutation.isPending}>
            {exportMutation.isPending ? (
              <Loader2 className="w-4 h-4 animate-spin mr-2" />
            ) : (
              <Download className="w-4 h-4 mr-2" />
            )}
            {t('nav.export')}
          </Button>
        </CardContent>
      </Card>

      {/* Results */}
      {result && (
        <Card className="animate-fade-in">
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <Check className="w-4 h-4 text-emerald-500" />
              {t('export.complete')}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="text-xs overflow-auto max-h-[400px] p-4 rounded-lg bg-muted/30 font-mono whitespace-pre-wrap">
              {JSON.stringify(result, null, 2)}
            </pre>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
