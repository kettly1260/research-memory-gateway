import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Lock, Check, Eye, EyeOff, Plus, Trash2, Copy, ShieldCheck, RefreshCw, KeyRound, Loader2 } from 'lucide-react'
import { useChangePassword, useApiKeys, useCreateApiKey, useDeleteApiKey, useConnections } from '@/lib/query'
import { toast } from 'sonner'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'

export function Security() {
  const { t } = useTranslation()

  return (
    <div className="p-6 md:p-8 max-w-5xl mx-auto space-y-6 animate-fade-in">
      <h1 className="text-2xl font-bold tracking-tight">{t('security.title')}</h1>

      <Tabs defaultValue="password">
        <TabsList>
          <TabsTrigger value="password">{t('security.tab_password')}</TabsTrigger>
          <TabsTrigger value="apikeys">{t('security.tab_apikeys')}</TabsTrigger>
          <TabsTrigger value="connections">{t('security.tab_connections')}</TabsTrigger>
        </TabsList>

        <TabsContent value="password" className="mt-4">
          <PasswordTab />
        </TabsContent>

        <TabsContent value="apikeys" className="mt-4">
          <ApiKeysTab />
        </TabsContent>

        <TabsContent value="connections" className="mt-4">
          <ConnectionsTab />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function PasswordTab() {
  const { t } = useTranslation()
  const changePassword = useChangePassword()
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [showCurrent, setShowCurrent] = useState(false)
  const [showNew, setShowNew] = useState(false)

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!currentPassword || !newPassword) return

    changePassword.mutate(
      { currentPassword, newPassword },
      {
        onSuccess: () => {
          toast.success('Password changed. Please log in again.')
          setCurrentPassword('')
          setNewPassword('')
          // Clear JWT tokens
          localStorage.removeItem('access_token')
          localStorage.removeItem('refresh_token')
          // Redirect to login after brief delay
          setTimeout(() => { window.location.href = '/admin/login' }, 1500)
        },
        onError: (err) => toast.error(String(err)),
      },
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Lock className="w-4 h-4 text-primary" />
          {t('security.tab_password')}
        </CardTitle>
        <CardDescription>Change the administrator password. You will be logged out after changing.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-4 max-w-md">
          <div className="space-y-2">
            <Label>Current Password</Label>
            <div className="relative">
              <Input
                type={showCurrent ? 'text' : 'password'}
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                required
              />
              <button
                type="button"
                className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
                onClick={() => setShowCurrent(!showCurrent)}
              >
                {showCurrent ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>
          <div className="space-y-2">
            <Label>New Password</Label>
            <div className="relative">
              <Input
                type={showNew ? 'text' : 'password'}
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                required
                minLength={6}
              />
              <button
                type="button"
                className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
                onClick={() => setShowNew(!showNew)}
              >
                {showNew ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>
          <Button type="submit" disabled={changePassword.isPending || !currentPassword || !newPassword}>
            {changePassword.isPending ? t('common.saving') : (
              <>
                <Check className="w-4 h-4 mr-2" />
                {t('common.save')}
              </>
            )}
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}

function ApiKeysTab() {
  const { data: keysRes, isLoading, refetch } = useApiKeys()
  const createMutation = useCreateApiKey()
  const deleteMutation = useDeleteApiKey()
  
  const [newKeyName, setNewKeyName] = useState('')
  const [customKey, setCustomKey] = useState('')
  const [createdKeyData, setCreatedKeyData] = useState<{ name: string; api_key: string } | null>(null)
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [copied, setCopied] = useState(false)

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault()
    if (!newKeyName) return
    createMutation.mutate(
      { name: newKeyName, customKey: customKey || undefined },
      {
        onSuccess: (data) => {
          setCreatedKeyData(data)
          setNewKeyName('')
          setCustomKey('')
          refetch()
        },
        onError: (err: any) => toast.error(err?.message || String(err))
      }
    )
  }

  const handleDelete = (id: string) => {
    if (!confirm('Are you sure you want to revoke this API Key? Any clients using it will lose access immediately.')) return
    deleteMutation.mutate(id, {
      onSuccess: () => {
        toast.success('API Key revoked')
        refetch()
      },
      onError: (err: any) => toast.error(err?.message || String(err))
    })
  }

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text)
    setCopied(true)
    toast.success('API Key copied to clipboard')
    setTimeout(() => setCopied(false), 2000)
  }

  const keys = keysRes?.items || []

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-4">
        <div>
          <CardTitle className="text-base flex items-center gap-2">
            <KeyRound className="w-4 h-4 text-primary" />
            API Key Management
          </CardTitle>
          <CardDescription>
            Generate credentials for external clients (like Cline or Antigravity) to access the gateway.
          </CardDescription>
        </div>
        <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
          <Button onClick={() => { setCreatedKeyData(null); setCreateDialogOpen(true) }} size="sm">
            <Plus className="w-4 h-4 mr-2" />
            New API Key
          </Button>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Create API Key</DialogTitle>
              <DialogDescription>
                Provide a name and an optional custom API key. If left blank, a secure random key will be generated.
              </DialogDescription>
            </DialogHeader>
            {!createdKeyData ? (
              <form onSubmit={handleCreate} className="space-y-4 pt-2">
                <div className="space-y-2">
                  <Label>Key Name</Label>
                  <Input 
                    value={newKeyName} 
                    onChange={(e) => setNewKeyName(e.target.value)} 
                    placeholder="e.g. Claude Desktop Client" 
                    required 
                  />
                </div>
                <div className="space-y-2">
                  <Label>Custom API Key (Optional)</Label>
                  <Input 
                    value={customKey} 
                    onChange={(e) => setCustomKey(e.target.value)} 
                    placeholder="Leave blank to auto-generate" 
                  />
                </div>
                <Button type="submit" className="w-full" disabled={createMutation.isPending}>
                  {createMutation.isPending ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : null}
                  Generate Key
                </Button>
              </form>
            ) : (
              <div className="space-y-4 pt-2">
                <div className="p-3 bg-emerald-50 dark:bg-emerald-950/20 text-emerald-800 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800 rounded-lg text-sm flex gap-2">
                  <ShieldCheck className="w-5 h-5 shrink-0 text-emerald-500" />
                  <div>
                    <span className="font-semibold">Key created successfully!</span>
                    <p className="text-xs mt-1 text-emerald-700/80 dark:text-emerald-400/80">
                      Copy the key now. For your security, this key will not be shown again.
                    </p>
                  </div>
                </div>
                <div className="space-y-2">
                  <Label>Key Name</Label>
                  <Input value={createdKeyData.name} readOnly className="bg-muted" />
                </div>
                <div className="space-y-2">
                  <Label>API Key Secret</Label>
                  <div className="flex gap-2">
                    <Input value={createdKeyData.api_key} readOnly className="font-mono text-sm" />
                    <Button onClick={() => copyToClipboard(createdKeyData.api_key)} variant="outline">
                      {copied ? 'Copied' : <Copy className="w-4 h-4" />}
                    </Button>
                  </div>
                </div>
                <Button onClick={() => setCreateDialogOpen(false)} className="w-full">
                  Close
                </Button>
              </div>
            )}
          </DialogContent>
        </Dialog>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="space-y-2 py-4">
            <div className="skeleton h-8 w-full" />
            <div className="skeleton h-8 w-full" />
            <div className="skeleton h-8 w-full" />
          </div>
        ) : keys.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
            <KeyRound className="w-8 h-8 text-muted-foreground/30 mb-2 animate-pulse" />
            <p className="text-sm">No API Keys created yet</p>
          </div>
        ) : (
          <div className="border rounded-lg overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Key ID</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Last Used</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="w-[100px]"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {keys.map((key) => (
                  <TableRow key={key.key_id}>
                    <TableCell className="font-medium">{key.name}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">{key.key_id}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {new Date(key.created_at).toLocaleString()}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {key.last_used_at ? new Date(key.last_used_at).toLocaleString() : 'Never'}
                    </TableCell>
                    <TableCell>
                      <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-50 text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-400 border border-emerald-200/50 dark:border-emerald-800/30">
                        {key.status}
                      </span>
                    </TableCell>
                    <TableCell>
                      <Button 
                        variant="ghost" 
                        size="sm" 
                        className="text-destructive hover:bg-destructive/10 hover:text-destructive h-8 w-8 p-0"
                        onClick={() => handleDelete(key.key_id)}
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function ConnectionsTab() {
  const { data: connRes, isLoading } = useConnections()
  const conns = connRes?.items || []

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <RefreshCw className="w-4 h-4 text-primary" />
          Active Connection Monitoring
        </CardTitle>
        <CardDescription>
          Real-time view of external clients accessing the Gateway API (auto-refreshing every 5 seconds).
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="space-y-2 py-4">
            <div className="skeleton h-8 w-full" />
            <div className="skeleton h-8 w-full" />
          </div>
        ) : conns.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
            <RefreshCw className="w-8 h-8 text-muted-foreground/30 mb-2 animate-pulse" />
            <p className="text-sm">No active client connections detected</p>
          </div>
        ) : (
          <div className="border rounded-lg overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Client IP</TableHead>
                  <TableHead>API Key Name</TableHead>
                  <TableHead>Client UA Info</TableHead>
                  <TableHead>Request Count</TableHead>
                  <TableHead>Last Active</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {conns.map((conn, idx) => (
                  <TableRow key={idx}>
                    <TableCell className="font-mono text-sm font-semibold">{conn.client_ip}</TableCell>
                    <TableCell>
                      <span className="inline-flex items-center px-2 py-0.5 rounded font-medium text-xs bg-primary/10 text-primary border border-primary/20">
                        {conn.key_name || 'Legacy/Default'}
                      </span>
                    </TableCell>
                    <TableCell className="max-w-[240px] truncate text-xs text-muted-foreground" title={conn.client_info || 'N/A'}>
                      {conn.client_info || 'N/A'}
                    </TableCell>
                    <TableCell className="text-sm font-semibold">{conn.request_count}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {new Date(conn.last_request_at).toLocaleString()}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
