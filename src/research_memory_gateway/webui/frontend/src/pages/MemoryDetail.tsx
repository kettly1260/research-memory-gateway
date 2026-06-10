import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useParams, useNavigate } from '@tanstack/react-router'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Separator } from '@/components/ui/separator'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  ArrowLeft,
  Clock,
  FileText,
  Tag,
  Archive,
  Undo2,
  Trash2,
  AlertTriangle,
  Copy,
} from 'lucide-react'
import { useMemory, useArchiveMemory, useRestoreMemory, useSoftDeleteMemory, useHardDeleteMemory, useUpdateMemory } from '@/lib/query'
import type { ResearchMemory } from '@/types/api'
import { toast } from 'sonner'

const statusVariants: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  active: 'default',
  archived: 'secondary',
  deleted: 'destructive',
}

export function MemoryDetail() {
  const { t } = useTranslation()
  const params = useParams({ strict: false })
  const navigate = useNavigate()
  const memoryId = (params as Record<string, string>).id || ''

  const { data: memory, isLoading, error } = useMemory(memoryId)
  const archiveMutation = useArchiveMemory()
  const restoreMutation = useRestoreMemory()
  const softDeleteMutation = useSoftDeleteMemory()
  const hardDeleteMutation = useHardDeleteMemory()
  const updateMutation = useUpdateMemory(memoryId)

  const [editMode, setEditMode] = useState(false)
  const [editJson, setEditJson] = useState('')
  const [hardDeleteOpen, setHardDeleteOpen] = useState(false)
  const [hardDeleteForm, setHardDeleteForm] = useState({ confirmId: '', password: '', reason: '' })

  if (isLoading) {
    return (
      <div className="p-6 md:p-8 max-w-5xl mx-auto space-y-4 animate-fade-in">
        <div className="skeleton h-8 w-48" />
        <div className="skeleton h-4 w-full" />
        <div className="skeleton h-64 w-full" />
      </div>
    )
  }

  if (error || !memory) {
    return (
      <div className="p-6 md:p-8 max-w-5xl mx-auto">
        <Card>
          <CardContent className="py-12 text-center text-muted-foreground">
            Memory not found
          </CardContent>
        </Card>
      </div>
    )
  }

  const handleEdit = () => {
    setEditJson(JSON.stringify(memory, null, 2))
    setEditMode(true)
  }

  const handleSaveEdit = () => {
    try {
      const parsed = JSON.parse(editJson) as Partial<ResearchMemory>
      updateMutation.mutate(parsed, {
        onSuccess: () => {
          toast.success('Memory updated')
          setEditMode(false)
        },
        onError: (err) => toast.error(String(err)),
      })
    } catch {
      toast.error('Invalid JSON')
    }
  }

  const handleHardDelete = () => {
    hardDeleteMutation.mutate(
      { id: memoryId, ...hardDeleteForm },
      {
        onSuccess: () => {
          toast.success('Memory permanently deleted')
          setHardDeleteOpen(false)
          navigate({ to: '/memories' })
        },
        onError: (err) => toast.error(String(err)),
      },
    )
  }

  return (
    <div className="p-6 md:p-8 max-w-5xl mx-auto space-y-6 animate-fade-in">
      {/* Back navigation */}
      <Link to="/memories" className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors">
        <ArrowLeft className="w-4 h-4" />
        {t('common.back')}
      </Link>

      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-2">
          <h1 className="text-2xl font-bold tracking-tight">{memory.title}</h1>
          <div className="flex items-center gap-2 flex-wrap">
            <Badge variant={statusVariants[memory.memory_status] || 'outline'} className="capitalize">
              {memory.memory_status}
            </Badge>
            <Badge variant="outline">{memory.memory_type}</Badge>
            <button
              onClick={() => { navigator.clipboard.writeText(memory.memory_id); toast.success('Copied!') }}
              className="text-xs text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1"
            >
              <Copy className="w-3 h-3" /> {memory.memory_id.slice(0, 12)}...
            </button>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <Button variant="outline" size="sm" onClick={handleEdit}>
            {t('common.edit')}
          </Button>
        </div>
      </div>

      {/* Main content */}
      <div className="grid gap-6 md:grid-cols-3">
        {/* Left: Details */}
        <div className="md:col-span-2 space-y-6">
          {/* Summary */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2">
                <FileText className="w-4 h-4" />
                Summary
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm leading-relaxed">{memory.summary}</p>
            </CardContent>
          </Card>

          {/* Claims */}
          {memory.claims.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">{t('memories.claims')}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {memory.claims.map((claim, i) => (
                  <div key={i} className="flex items-start gap-3 p-3 rounded-lg bg-muted/30 stagger-item">
                    <div className="flex-1">
                      <p className="text-sm">{claim.claim}</p>
                    </div>
                    <Badge variant="outline" className="text-[10px] shrink-0 capitalize">
                      {claim.verification_status}
                    </Badge>
                  </div>
                ))}
              </CardContent>
            </Card>
          )}

          {/* Evidence */}
          {memory.evidence.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">{t('memories.evidence')}</CardTitle>
              </CardHeader>
              <CardContent>
                <pre className="text-xs overflow-auto max-h-[400px] p-4 rounded-lg bg-muted/30 font-mono whitespace-pre-wrap">
                  {JSON.stringify(memory.evidence, null, 2)}
                </pre>
              </CardContent>
            </Card>
          )}
        </div>

        {/* Right: Metadata + Actions */}
        <div className="space-y-6">
          {/* Metadata */}
          <Card>
            <CardContent className="pt-6 space-y-4">
              <div>
                <Label className="text-xs text-muted-foreground">{t('memories.col_project')}</Label>
                <p className="text-sm font-medium">{memory.project}</p>
              </div>
              <Separator />
              <div>
                <Label className="text-xs text-muted-foreground">{t('memories.col_topic')}</Label>
                <p className="text-sm font-medium">{memory.topic}</p>
              </div>
              <Separator />
              <div className="flex items-center gap-2">
                <Clock className="w-3.5 h-3.5 text-muted-foreground" />
                <div>
                  <Label className="text-xs text-muted-foreground">Created</Label>
                  <p className="text-xs">{new Date(memory.created_at).toLocaleString()}</p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Clock className="w-3.5 h-3.5 text-muted-foreground" />
                <div>
                  <Label className="text-xs text-muted-foreground">Updated</Label>
                  <p className="text-xs">{new Date(memory.updated_at).toLocaleString()}</p>
                </div>
              </div>
              {memory.tags.length > 0 && (
                <>
                  <Separator />
                  <div>
                    <Label className="text-xs text-muted-foreground flex items-center gap-1">
                      <Tag className="w-3 h-3" /> Tags
                    </Label>
                    <div className="flex flex-wrap gap-1 mt-1.5">
                      {memory.tags.map((tag) => (
                        <Badge key={tag} variant="secondary" className="text-[10px]">{tag}</Badge>
                      ))}
                    </div>
                  </div>
                </>
              )}
            </CardContent>
          </Card>

          {/* Actions */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">{t('common.actions')}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {memory.memory_status === 'active' && (
                <Button
                  variant="outline"
                  size="sm"
                  className="w-full justify-start"
                  onClick={() => archiveMutation.mutate({ id: memoryId }, { onSuccess: () => toast.success('Archived') })}
                >
                  <Archive className="w-4 h-4 mr-2" /> Archive
                </Button>
              )}
              {memory.memory_status === 'archived' && (
                <Button
                  variant="outline"
                  size="sm"
                  className="w-full justify-start"
                  onClick={() => restoreMutation.mutate({ id: memoryId }, { onSuccess: () => toast.success('Restored') })}
                >
                  <Undo2 className="w-4 h-4 mr-2" /> Restore
                </Button>
              )}
              {memory.memory_status !== 'deleted' && (
                <Button
                  variant="outline"
                  size="sm"
                  className="w-full justify-start text-destructive hover:bg-destructive/10"
                  onClick={() => softDeleteMutation.mutate({ id: memoryId }, { onSuccess: () => toast.success('Soft deleted') })}
                >
                  <Trash2 className="w-4 h-4 mr-2" /> Soft Delete
                </Button>
              )}
              {memory.memory_status === 'deleted' && (
                <Button
                  variant="destructive"
                  size="sm"
                  className="w-full justify-start"
                  onClick={() => setHardDeleteOpen(true)}
                >
                  <AlertTriangle className="w-4 h-4 mr-2" /> Hard Delete
                </Button>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      {/* Edit Modal */}
      <Dialog open={editMode} onOpenChange={setEditMode}>
        <DialogContent className="max-w-3xl max-h-[80vh] overflow-auto">
          <DialogHeader>
            <DialogTitle>{t('common.edit')} Memory</DialogTitle>
            <DialogDescription>Edit the raw JSON representation of this memory.</DialogDescription>
          </DialogHeader>
          <Textarea
            value={editJson}
            onChange={(e) => setEditJson(e.target.value)}
            className="font-mono text-xs min-h-[400px]"
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditMode(false)}>{t('common.cancel')}</Button>
            <Button onClick={handleSaveEdit} disabled={updateMutation.isPending}>
              {updateMutation.isPending ? t('common.saving') : t('common.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Hard Delete Confirmation */}
      <Dialog open={hardDeleteOpen} onOpenChange={setHardDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="text-destructive flex items-center gap-2">
              <AlertTriangle className="w-5 h-5" /> Permanent Delete
            </DialogTitle>
            <DialogDescription>
              This action is irreversible. Enter the memory ID, your password, and a reason.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label className="text-xs">Memory ID: <code className="text-[10px]">{memory.memory_id}</code></Label>
              <Input
                placeholder="Confirm memory ID"
                value={hardDeleteForm.confirmId}
                onChange={(e) => setHardDeleteForm((f) => ({ ...f, confirmId: e.target.value }))}
              />
            </div>
            <div>
              <Label className="text-xs">Password</Label>
              <Input
                type="password"
                value={hardDeleteForm.password}
                onChange={(e) => setHardDeleteForm((f) => ({ ...f, password: e.target.value }))}
              />
            </div>
            <div>
              <Label className="text-xs">Reason</Label>
              <Input
                value={hardDeleteForm.reason}
                onChange={(e) => setHardDeleteForm((f) => ({ ...f, reason: e.target.value }))}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setHardDeleteOpen(false)}>{t('common.cancel')}</Button>
            <Button variant="destructive" onClick={handleHardDelete} disabled={hardDeleteMutation.isPending}>
              Permanently Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
