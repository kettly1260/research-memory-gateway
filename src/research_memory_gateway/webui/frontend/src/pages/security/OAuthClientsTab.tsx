import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Copy, Loader2, Pencil, Plus, RefreshCw, ShieldOff, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ApiError, type OAuthClient } from '@/lib/api'
import { useOAuthClients, useOAuthClientMutation } from '@/lib/query'

type ConfirmAction = { type: 'delete' | 'revoke' | 'disable'; client: OAuthClient }

export function OAuthClientsTab() {
  const { t } = useTranslation()
  const query = useOAuthClients()
  const mutation = useOAuthClientMutation()
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<OAuthClient | null>(null)
  const [name, setName] = useState('')
  const [redirects, setRedirects] = useState('')
  const [method, setMethod] = useState<OAuthClient['token_endpoint_auth_method']>('none')
  const [credential, setCredential] = useState<{ client_id: string; client_secret?: string } | null>(null)
  const [confirm, setConfirm] = useState<ConfirmAction | null>(null)
  const formatDate = (value: number | null) => value ? new Date(value * 1000).toLocaleString() : '-'
  const onError = (error: Error) => toast.error(
    error instanceof ApiError ? String(error.body.error_description || error.message) : error.message,
  )
  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      toast.success(t('oauth.copied'))
    } catch {
      toast.error(t('oauth.copy_failed'))
    }
  }
  const closeEditor = () => { setCreating(false); setEditing(null) }
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (editing) {
      mutation.mutate({ type: 'update', id: editing.client_id, patch: { client_name: name.trim() } }, {
        onSuccess: () => { closeEditor(); toast.success(t('oauth.saved')) },
        onError,
      })
    } else {
      mutation.mutate({ type: 'create', input: {
        client_name: name.trim(),
        redirect_uris: redirects.split(/\r?\n/).map(value => value.trim()).filter(Boolean),
        token_endpoint_auth_method: method,
      } }, {
        onSuccess: result => {
          closeEditor()
          if ('client_id' in result) setCredential(result)
          toast.success(t('oauth.saved'))
        },
        onError,
      })
    }
  }
  const confirmAction = () => {
    if (!confirm) return
    const { type, client } = confirm
    mutation.mutate(type === 'disable'
      ? { type: 'update', id: client.client_id, patch: { status: 'disabled' } }
      : { type, id: client.client_id }, {
      onSuccess: () => { setConfirm(null); toast.success(t('oauth.saved')) },
      onError,
    })
  }

  return (
    <section className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-base font-semibold">{t('oauth.title')}</h2>
        <div className="flex gap-2">
          <Button variant="outline" size="icon" title={t('oauth.refresh')} aria-label={t('oauth.refresh')}
            disabled={query.isFetching} onClick={() => query.refetch()}>
            <RefreshCw className={query.isFetching ? 'animate-spin' : ''} />
          </Button>
          <Button disabled={!query.data?.enabled || mutation.isPending} onClick={() => {
            setName(''); setRedirects(''); setMethod('none'); setCreating(true)
          }}><Plus />{t('oauth.create')}</Button>
        </div>
      </div>
      {query.isLoading && <div role="status" className="flex gap-2 py-8"><Loader2 className="size-4 animate-spin" />{t('common.loading')}</div>}
      {query.isError && <p role="alert" className="text-destructive">{String(query.error)}</p>}
      {query.data && <>
        <dl className="grid gap-x-6 gap-y-2 border-y py-4 text-sm sm:grid-cols-[auto_1fr]">
          <dt className="text-muted-foreground">{t('oauth.status')}</dt>
          <dd>{t(query.data.enabled ? 'oauth.enabled' : 'oauth.disabled')}</dd>
          <dt className="text-muted-foreground">Issuer</dt><dd className="break-all">{query.data.issuer || '-'}</dd>
          <dt className="text-muted-foreground">{t('oauth.resource')}</dt><dd className="break-all">{query.data.resource || '-'}</dd>
          <dt className="text-muted-foreground">{t('oauth.registration')}</dt>
          <dd>{t(query.data.dynamic_registration && query.data.enabled ? 'oauth.enabled' : 'oauth.disabled')}</dd>
        </dl>
        <Table>
          <TableHeader><TableRow>
            <TableHead>{t('oauth.name')}</TableHead><TableHead>{t('oauth.status')}</TableHead>
            <TableHead className="hidden md:table-cell">{t('oauth.grants')}</TableHead>
            <TableHead className="hidden lg:table-cell">{t('security.apikey_last_used')}</TableHead>
            <TableHead className="text-right">{t('oauth.actions')}</TableHead>
          </TableRow></TableHeader>
          <TableBody>
            {query.data.items.length === 0 && <TableRow><TableCell colSpan={5} className="py-10 text-center text-muted-foreground">{t('oauth.empty')}</TableCell></TableRow>}
            {query.data.items.map(client => <TableRow key={client.client_id}>
              <TableCell className="min-w-32 max-w-80 whitespace-normal">
                <div className="font-medium break-words">{client.client_name}</div>
                <div className="flex items-center gap-1">
                  <span className="text-xs text-muted-foreground break-all">{client.client_id}</span>
                  <Button variant="ghost" size="icon-sm" className="shrink-0" title={t('oauth.copy_id')} aria-label={t('oauth.copy_id')} onClick={() => copy(client.client_id)}><Copy /></Button>
                </div>
                <div className="text-xs text-muted-foreground">{client.token_endpoint_auth_method}</div>
              </TableCell>
              <TableCell>
                <label className="inline-flex items-center gap-2">
                  <input type="checkbox" checked={client.status === 'active'} disabled={mutation.isPending}
                    aria-label={`${t('oauth.enabled')}: ${client.client_name}`}
                    onChange={() => {
                      if (client.status === 'active') setConfirm({ type: 'disable', client })
                      else mutation.mutate({ type: 'update', id: client.client_id, patch: { status: 'active' } }, { onError })
                    }} />
                  <span className="hidden sm:inline">{t(client.status === 'active' ? 'oauth.enabled' : 'oauth.disabled')}</span>
                </label>
              </TableCell>
              <TableCell className="hidden md:table-cell">{client.active_grants}</TableCell>
              <TableCell className="hidden lg:table-cell text-xs">{formatDate(client.last_used_at)}</TableCell>
              <TableCell>
                <div className="flex justify-end gap-1">
                  <Button variant="ghost" size="icon-sm" title={t('oauth.rename')} aria-label={t('oauth.rename')} disabled={mutation.isPending}
                    onClick={() => { setName(client.client_name); setEditing(client) }}><Pencil /></Button>
                  <Button variant="ghost" size="icon-sm" title={t('oauth.revoke')} aria-label={t('oauth.revoke')} disabled={mutation.isPending}
                    onClick={() => setConfirm({ type: 'revoke', client })}><ShieldOff /></Button>
                  <Button variant="ghost" size="icon-sm" className="text-destructive" title={t('common.delete')} aria-label={t('common.delete')} disabled={mutation.isPending}
                    onClick={() => setConfirm({ type: 'delete', client })}><Trash2 /></Button>
                </div>
              </TableCell>
            </TableRow>)}
          </TableBody>
        </Table>
      </>}

      <Dialog open={creating || !!editing} onOpenChange={open => { if (!open && !mutation.isPending) closeEditor() }}>
        <DialogContent className="sm:max-w-lg max-h-[90dvh] overflow-y-auto rounded-lg">
          <DialogHeader><DialogTitle>{t(editing ? 'oauth.rename' : 'oauth.create')}</DialogTitle></DialogHeader>
          <form onSubmit={submit} className="space-y-4">
            <div className="space-y-2"><Label htmlFor="oauth-name">{t('oauth.name')}</Label>
              <Input id="oauth-name" value={name} onChange={event => setName(event.target.value)} required maxLength={200} autoFocus /></div>
            {editing && <div className="space-y-2">
              <Label>{t('oauth.redirects')}</Label>
              {editing.redirect_uris.map(uri => <p key={uri} className="break-all text-xs text-muted-foreground">{uri}</p>)}
            </div>}
            {!editing && <>
              <div className="space-y-2"><Label htmlFor="oauth-redirects">{t('oauth.redirects')}</Label>
                <Textarea id="oauth-redirects" value={redirects} onChange={event => setRedirects(event.target.value)} required rows={4} placeholder="http://127.0.0.1:3000/callback" /></div>
              <div className="space-y-2"><Label htmlFor="oauth-method">{t('oauth.method')}</Label>
                <select id="oauth-method" value={method} onChange={event => setMethod(event.target.value as OAuthClient['token_endpoint_auth_method'])}
                  className="h-10 w-full rounded-md border bg-background px-3">
                  <option value="none">{t('oauth.public_client')}</option>
                  <option value="client_secret_post">client_secret_post</option>
                  <option value="client_secret_basic">client_secret_basic</option>
                </select></div>
            </>}
            <div className="flex justify-end gap-2"><Button type="button" variant="outline" disabled={mutation.isPending} onClick={closeEditor}>{t('common.cancel')}</Button>
              <Button type="submit" disabled={mutation.isPending || !name.trim() || (creating && !redirects.trim())}>
                {mutation.isPending && <Loader2 className="animate-spin" />}{t('common.save')}
              </Button></div>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={!!credential} onOpenChange={open => { if (!open) setCredential(null) }}>
        <DialogContent className="sm:max-w-lg rounded-lg">
          <DialogHeader><DialogTitle>{t('oauth.created')}</DialogTitle>
            {credential?.client_secret && <DialogDescription>{t('oauth.secret_once')}</DialogDescription>}
          </DialogHeader>
          {credential && <div className="space-y-4">
            {[['Client ID', credential.client_id], ['Client Secret', credential.client_secret]].map(([label, value]) => value && <div key={label} className="space-y-2">
              <Label>{label}</Label><div className="flex items-center gap-2"><code className="min-w-0 flex-1 break-all text-xs">{value}</code>
                <Button variant="outline" size="icon" title={t('oauth.copy')} aria-label={t('oauth.copy')} onClick={() => copy(value)}><Copy /></Button></div></div>)}
            <Button className="w-full" onClick={() => setCredential(null)}>{t('common.close')}</Button>
          </div>}
        </DialogContent>
      </Dialog>

      <Dialog open={!!confirm} onOpenChange={open => { if (!open && !mutation.isPending) setConfirm(null) }}>
        <DialogContent className="rounded-lg">
          <DialogHeader><DialogTitle>{t(`oauth.${confirm?.type || 'revoke'}`)}</DialogTitle>
            <DialogDescription className="break-all">{t('oauth.confirm', { name: confirm?.client.client_name })}</DialogDescription></DialogHeader>
          <div className="flex justify-end gap-2"><Button variant="outline" disabled={mutation.isPending} onClick={() => setConfirm(null)}>{t('common.cancel')}</Button>
            <Button variant="destructive" disabled={mutation.isPending} onClick={confirmAction}>
              {mutation.isPending && <Loader2 className="animate-spin" />}{t('common.confirm')}
            </Button></div>
        </DialogContent>
      </Dialog>
    </section>
  )
}
