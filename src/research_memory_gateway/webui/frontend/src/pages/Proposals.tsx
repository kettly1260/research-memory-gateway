import * as React from 'react'
import { useTranslation } from 'react-i18next'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Check, Clock, FileClock, PencilLine, XCircle } from 'lucide-react'
import { useProposal, useProposals, useSaveProposal, useTaxonomy, useUpdateProposalStatus } from '@/lib/query'
import { formatMemoryType, localizedLabel } from '@/constants/memoryTypes'
import type { ProposalStatus, SaveProposal } from '@/types/api'
import { toast } from 'sonner'

const saveableProposalStatuses = new Set<ProposalStatus>(['pending', 'approved'])
const terminalProposalStatuses = new Set<ProposalStatus>(['saved', 'rejected', 'expired'])

const statusVariants: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  pending: 'outline',
  approved: 'secondary',
  rejected: 'destructive',
  needs_edit: 'secondary',
  saved: 'default',
  expired: 'outline',
}

export function Proposals() {
  const { t, i18n } = useTranslation()
  const [statusFilter, setStatusFilter] = React.useState('pending')
  const [selectedId, setSelectedId] = React.useState<string | null>(null)
  const { data: taxonomy } = useTaxonomy()
  const { data: proposals = [], isLoading } = useProposals({
    status: statusFilter === 'all' ? undefined : statusFilter,
    limit: '100',
  })
  const selectedProposalId = React.useMemo(() => {
    if (proposals.length === 0) return null
    if (selectedId && proposals.some((item) => item.proposal_id === selectedId)) {
      return selectedId
    }
    return proposals[0].proposal_id
  }, [proposals, selectedId])
  const { data: selected } = useProposal(selectedProposalId)
  const selectedProposal = selectedProposalId ? selected : undefined
  const saveMutation = useSaveProposal()
  const statusMutation = useUpdateProposalStatus()

  const formatProposalStatus = (status: string) =>
    localizedLabel(taxonomy?.proposal_statuses, status, i18n.language)
  const selectedProposalStatus = selectedProposal?.proposal_status
  const canSaveSelectedProposal = selectedProposalStatus ? saveableProposalStatuses.has(selectedProposalStatus) : false
  const canChangeSelectedProposalStatus = selectedProposalStatus ? !terminalProposalStatuses.has(selectedProposalStatus) : false

  const saveSelected = () => {
    if (!selectedProposalId || !canSaveSelectedProposal) return
    saveMutation.mutate(
      { id: selectedProposalId, text: 'WebUI proposal save' },
      {
        onSuccess: (memory) => toast.success(t('proposals.saved_toast', { title: memory.title })),
        onError: (err) => toast.error(String(err)),
      },
    )
  }

  const changeStatus = (proposalStatus: string) => {
    if (!selectedProposalId || !canChangeSelectedProposalStatus || selectedProposalStatus === proposalStatus) return
    statusMutation.mutate(
      { id: selectedProposalId, proposalStatus, reason: `WebUI ${proposalStatus}` },
      {
        onSuccess: () => toast.success(t('proposals.status_updated')),
        onError: (err) => toast.error(String(err)),
      },
    )
  }

  return (
    <div className="p-6 md:p-8 max-w-7xl mx-auto space-y-6 animate-fade-in">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold tracking-tight">{t('proposals.title')}</h1>
        <Select value={statusFilter} onValueChange={(value) => { if (value !== null) setStatusFilter(value) }}>
          <SelectTrigger className="w-[180px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="pending">{formatProposalStatus('pending')}</SelectItem>
            <SelectItem value="needs_edit">{formatProposalStatus('needs_edit')}</SelectItem>
            <SelectItem value="approved">{formatProposalStatus('approved')}</SelectItem>
            <SelectItem value="saved">{formatProposalStatus('saved')}</SelectItem>
            <SelectItem value="rejected">{formatProposalStatus('rejected')}</SelectItem>
            <SelectItem value="expired">{formatProposalStatus('expired')}</SelectItem>
            <SelectItem value="all">{t('common.all')}</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(420px,0.9fr)]">
        <div className="rounded-lg border bg-card overflow-hidden">
          <Table>
            <TableHeader>
              <TableRow className="bg-muted/30 hover:bg-muted/30">
                <TableHead>{t('proposals.memory')}</TableHead>
                <TableHead>{t('proposals.reason')}</TableHead>
                <TableHead>{t('common.status')}</TableHead>
                <TableHead>{t('memories.col_updated')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading ? (
                Array.from({ length: 4 }).map((_, index) => (
                  <TableRow key={index}>
                    <TableCell colSpan={4}>
                      <div className="skeleton h-4 w-full" />
                    </TableCell>
                  </TableRow>
                ))
              ) : proposals.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={4} className="h-32 text-center text-muted-foreground">
                    {t('common.no_results')}
                  </TableCell>
                </TableRow>
              ) : (
                proposals.map((proposal) => (
                  <ProposalRow
                    key={proposal.proposal_id}
                    proposal={proposal}
                    selected={proposal.proposal_id === selectedProposalId}
                    statusLabel={formatProposalStatus(proposal.proposal_status)}
                    typeLabel={formatMemoryType(proposal.suggested_memory.memory_type, taxonomy?.memory_types, i18n.language)}
                    onSelect={() => setSelectedId(proposal.proposal_id)}
                  />
                ))
              )}
            </TableBody>
          </Table>
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2">
                <FileClock className="w-4 h-4" />
                {t('proposals.detail')}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {selectedProposal ? (
                <>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={statusVariants[selectedProposal.proposal_status] || 'outline'}>
                      {formatProposalStatus(selectedProposal.proposal_status)}
                    </Badge>
                    <Badge variant="outline">
                      {formatMemoryType(selectedProposal.suggested_memory.memory_type, taxonomy?.memory_types, i18n.language)}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      v{selectedProposal.current_version}
                    </span>
                  </div>
                  <div>
                    <p className="text-sm font-medium">{selectedProposal.suggested_memory.title}</p>
                    <p className="text-xs text-muted-foreground mt-1">{selectedProposal.reason}</p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button size="sm" onClick={saveSelected} disabled={saveMutation.isPending || !canSaveSelectedProposal}>
                      <Check className="w-4 h-4 mr-2" />
                      {t('proposals.save')}
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => changeStatus('needs_edit')} disabled={statusMutation.isPending || !canChangeSelectedProposalStatus || selectedProposalStatus === 'needs_edit'}>
                      <PencilLine className="w-4 h-4 mr-2" />
                      {formatProposalStatus('needs_edit')}
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => changeStatus('expired')} disabled={statusMutation.isPending || !canChangeSelectedProposalStatus || selectedProposalStatus === 'expired'}>
                      <Clock className="w-4 h-4 mr-2" />
                      {formatProposalStatus('expired')}
                    </Button>
                    <Button size="sm" variant="destructive" onClick={() => changeStatus('rejected')} disabled={statusMutation.isPending || !canChangeSelectedProposalStatus || selectedProposalStatus === 'rejected'}>
                      <XCircle className="w-4 h-4 mr-2" />
                      {formatProposalStatus('rejected')}
                    </Button>
                  </div>
                  <pre className="text-xs overflow-auto max-h-[360px] p-4 rounded-lg bg-muted/30 font-mono whitespace-pre-wrap">
                    {JSON.stringify(selectedProposal.suggested_memory, null, 2)}
                  </pre>
                </>
              ) : (
                <div className="py-12 text-center text-muted-foreground">{t('common.no_results')}</div>
              )}
            </CardContent>
          </Card>

          {selectedProposal && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">{t('proposals.versions')}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {selectedProposal.versions.length === 0 ? (
                  <p className="text-sm text-muted-foreground">{t('common.no_results')}</p>
                ) : (
                  selectedProposal.versions.map((version) => (
                    <div key={version.version} className="rounded-lg border p-3 text-sm space-y-1">
                      <div className="flex items-center justify-between">
                        <span className="font-medium">v{version.version}</span>
                        <span className="text-xs text-muted-foreground">{new Date(version.created_at).toLocaleString()}</span>
                      </div>
                      <p className="text-xs text-muted-foreground">{version.author} · {version.change_reason}</p>
                      {version.confirmation && (
                        <pre className="text-[11px] overflow-auto rounded bg-muted/30 p-2">
                          {JSON.stringify(version.confirmation, null, 2)}
                        </pre>
                      )}
                    </div>
                  ))
                )}
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}

function ProposalRow({
  proposal,
  selected,
  statusLabel,
  typeLabel,
  onSelect,
}: {
  proposal: SaveProposal
  selected: boolean
  statusLabel: string
  typeLabel: string
  onSelect: () => void
}) {
  return (
    <TableRow
      className={selected ? 'bg-muted/50 cursor-pointer' : 'cursor-pointer'}
      onClick={onSelect}
    >
      <TableCell>
        <div className="space-y-1">
          <p className="font-medium line-clamp-1">{proposal.suggested_memory.title}</p>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="text-[10px]">{typeLabel}</Badge>
            <span className="text-xs text-muted-foreground">{proposal.suggested_memory.project}</span>
          </div>
        </div>
      </TableCell>
      <TableCell className="max-w-[240px]">
        <span className="text-sm text-muted-foreground line-clamp-2">{proposal.reason}</span>
      </TableCell>
      <TableCell>
        <Badge variant={statusVariants[proposal.proposal_status] || 'outline'} className="text-[11px]">
          {statusLabel}
        </Badge>
      </TableCell>
      <TableCell className="text-sm text-muted-foreground">
        {new Date(proposal.updated_at).toLocaleString()}
      </TableCell>
    </TableRow>
  )
}
