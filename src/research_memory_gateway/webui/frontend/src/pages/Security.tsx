import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Lock, Check, Eye, EyeOff } from 'lucide-react'
import { useChangePassword } from '@/lib/query'
import { toast } from 'sonner'

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
          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t('security.tab_apikeys')}</CardTitle>
              <CardDescription>API key management will be available in a future update (Phase 2).</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="flex items-center justify-center py-12 text-muted-foreground">
                <p className="text-sm">Coming soon — API key CRUD + usage statistics</p>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="connections" className="mt-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t('security.tab_connections')}</CardTitle>
              <CardDescription>Active client monitoring will be available in a future update (Phase 2).</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="flex items-center justify-center py-12 text-muted-foreground">
                <p className="text-sm">Coming soon — active connection monitoring</p>
              </div>
            </CardContent>
          </Card>
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
          <Lock className="w-4 h-4" />
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
