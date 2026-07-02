import { useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Textarea } from '@/components/ui/textarea'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Upload, FileJson, Check, AlertCircle, Loader2 } from 'lucide-react'
import { useImportValidate, useImportExecute } from '@/lib/query'
import { ApiError } from '@/lib/api'
import { toast } from 'sonner'

const importPolicies = ['skip_existing', 'overwrite_existing', 'import_as_new'] as const
type ImportPolicy = (typeof importPolicies)[number]

export function ImportPage() {
  const { t } = useTranslation()
  const [jsonContent, setJsonContent] = useState('')
  const [policy, setPolicy] = useState<ImportPolicy>('skip_existing')
  const [validationResult, setValidationResult] = useState<{
    valid: number; invalid: number; duplicates: number; errors: Array<{ index: number; error: string }>
  } | null>(null)
  const [confirmationDiffs, setConfirmationDiffs] = useState<Record<string, string> | null>(null)
  const [isDragOver, setIsDragOver] = useState(false)

  const validateMutation = useImportValidate()
  const executeMutation = useImportExecute()

  const parseMemories = useCallback(() => {
    try {
      const parsed = JSON.parse(jsonContent)
      return Array.isArray(parsed) ? parsed : [parsed]
    } catch {
      toast.error(t('common.invalid_json'))
      return null
    }
  }, [jsonContent, t])

  const handleValidate = () => {
    const memories = parseMemories()
    if (!memories) return
    setConfirmationDiffs(null)
    validateMutation.mutate({ memories, policy }, {
      onSuccess: (data) => {
        setValidationResult(data)
        toast.success(t('import.validated_toast', { valid: data.valid, invalid: data.invalid }))
      },
    })
  }

  const handleExecute = (confirmed: boolean = false) => {
    const memories = parseMemories()
    if (!memories) return
    if (!confirmed) {
      setConfirmationDiffs(null)
    }
    executeMutation.mutate({ memories, policy, confirmed }, {
      onSuccess: (data) => {
        toast.success(t('import.imported_toast', { imported: data.imported, skipped: data.skipped }))
        setJsonContent('')
        setValidationResult(null)
        setConfirmationDiffs(null)
      },
      onError: (err) => {
        if (err instanceof ApiError && err.body.error === 'confirmation_required') {
          const diffs = err.body.diffs
          if (diffs && typeof diffs === 'object' && !Array.isArray(diffs)) {
            setConfirmationDiffs(diffs as Record<string, string>)
          }
          toast.error(t('import.confirmation_required'))
          return
        }
        toast.error(err instanceof Error ? err.message : String(err))
      },
    })
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setIsDragOver(false)
    const file = e.dataTransfer.files[0]
    if (file && file.type === 'application/json') {
      const reader = new FileReader()
      reader.onload = (ev) => {
        setJsonContent(ev.target?.result as string || '')
      }
      reader.readAsText(file)
    }
  }

  return (
    <div className="p-6 md:p-8 max-w-5xl mx-auto space-y-6 animate-fade-in">
      <h1 className="text-2xl font-bold tracking-tight">{t('nav.import')}</h1>

      {/* Drop zone */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Upload className="w-4 h-4" />
            {t('import.json_import')}
          </CardTitle>
          <CardDescription>
            {t('import.json_import_desc')}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div
            className={`border-2 border-dashed rounded-lg p-8 text-center transition-colors cursor-pointer ${
              isDragOver ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/50'
            }`}
            onDragOver={(e) => { e.preventDefault(); setIsDragOver(true) }}
            onDragLeave={() => setIsDragOver(false)}
            onDrop={handleDrop}
            onClick={() => {
              const input = document.createElement('input')
              input.type = 'file'
              input.accept = '.json'
              input.onchange = (e) => {
                const file = (e.target as HTMLInputElement).files?.[0]
                if (file) {
                  const reader = new FileReader()
                  reader.onload = (ev) => setJsonContent(ev.target?.result as string || '')
                  reader.readAsText(file)
                }
              }
              input.click()
            }}
          >
            <FileJson className="w-8 h-8 mx-auto text-muted-foreground mb-2" />
            <p className="text-sm text-muted-foreground">{t('import.drop_hint')}</p>
          </div>

          <Textarea
            value={jsonContent}
            onChange={(e) => setJsonContent(e.target.value)}
            className="font-mono text-xs min-h-[200px]"
            placeholder='[{"project":"demo","topic":"...","memory_type":"paper_note","title":"...","summary":"..."}]'
          />

          <div className="flex items-center gap-4">
            <div className="space-y-1">
              <label className="text-xs text-muted-foreground font-medium">{t('import.policy')}</label>
              <Select value={policy} onValueChange={(v) => {
                if (importPolicies.includes(v as ImportPolicy)) setPolicy(v as ImportPolicy)
              }}>
                <SelectTrigger className="w-[200px]"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="skip_existing">{t('import.skip_existing')}</SelectItem>
                  <SelectItem value="overwrite_existing">{t('import.overwrite_existing')}</SelectItem>
                  <SelectItem value="import_as_new">{t('import.import_as_new')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="flex gap-2">
            <Button variant="outline" onClick={handleValidate} disabled={!jsonContent || validateMutation.isPending}>
              {validateMutation.isPending ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : <Check className="w-4 h-4 mr-2" />}
              {t('import.validate')}
            </Button>
            <Button onClick={() => handleExecute(false)} disabled={!jsonContent || executeMutation.isPending}>
              {executeMutation.isPending ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : <Upload className="w-4 h-4 mr-2" />}
              {t('import.execute')}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Validation Results */}
      {validationResult && (
        <Card className="animate-fade-in">
          <CardHeader>
            <CardTitle className="text-base">{t('import.validation_results')}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex gap-4">
              <div className="flex items-center gap-2">
                  <Badge variant="default" className="text-xs">{validationResult.valid} {t('import.valid')}</Badge>
              </div>
              {validationResult.invalid > 0 && (
                <div className="flex items-center gap-2">
                  <Badge variant="destructive" className="text-xs">{validationResult.invalid} {t('import.invalid')}</Badge>
                </div>
              )}
              {validationResult.duplicates > 0 && (
                <div className="flex items-center gap-2">
                  <Badge variant="secondary" className="text-xs">{validationResult.duplicates} {t('import.duplicates')}</Badge>
                </div>
              )}
            </div>
            {validationResult.errors.length > 0 && (
              <div className="space-y-1">
                {validationResult.errors.map((err, i) => (
                  <div key={i} className="flex items-center gap-2 text-sm text-destructive">
                    <AlertCircle className="w-3 h-3" />
                    <span>{t('common.item')} {err.index}: {err.error}</span>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {confirmationDiffs && (
        <Card className="animate-fade-in border-amber-200 dark:border-amber-900/60">
          <CardHeader>
            <CardTitle className="text-base">{t('import.overwrite_diffs')}</CardTitle>
            <CardDescription>{t('import.confirmation_required')}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="max-h-80 overflow-auto rounded-lg border bg-muted/30 p-3">
              {Object.entries(confirmationDiffs).map(([memoryId, diff]) => (
                <div key={memoryId} className="space-y-2">
                  <div className="font-mono text-xs font-semibold">{memoryId}</div>
                  <pre className="whitespace-pre-wrap text-xs leading-relaxed">{diff}</pre>
                </div>
              ))}
            </div>
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setConfirmationDiffs(null)}>
                {t('common.cancel')}
              </Button>
              <Button onClick={() => handleExecute(true)} disabled={executeMutation.isPending}>
                {executeMutation.isPending ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : null}
                {t('import.confirm_overwrite')}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
