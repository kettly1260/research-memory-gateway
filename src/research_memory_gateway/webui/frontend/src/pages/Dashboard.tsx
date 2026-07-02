import { useTranslation } from "react-i18next"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Database, Activity, Clock, BrainCircuit, Plus, ArrowRight } from "lucide-react"
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis, PieChart, Pie, Cell, BarChart, Bar } from "recharts"
import { useMemories, useVectorCoverage, useStats, useTaxonomy } from "@/lib/query"
import { Link } from "@tanstack/react-router"
import type { ResearchMemory } from "@/types/api"
import { formatMemoryType } from "@/constants/memoryTypes"

const CHART_COLORS = [
  'var(--chart-1)',
  'var(--chart-2)',
  'var(--chart-3)',
  'var(--chart-4)',
  'var(--chart-5)',
]

function StatCard({ title, value, icon: Icon, description }: {
  title: string
  value: string | number
  icon: React.ElementType
  description?: string
}) {
  return (
    <Card className="hover:shadow-md transition-all duration-200 hover:-translate-y-0.5 group">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {title}
        </CardTitle>
        <div className="h-8 w-8 rounded-lg bg-primary/10 flex items-center justify-center group-hover:bg-primary/15 transition-colors">
          <Icon className="h-4 w-4 text-primary" />
        </div>
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold tracking-tight">{value}</div>
        {description && (
          <p className="text-xs text-muted-foreground mt-1">{description}</p>
        )}
      </CardContent>
    </Card>
  )
}

function computeDistribution(memories: ResearchMemory[], key: 'memory_type' | 'project') {
  const counts: Record<string, number> = {}
  memories.forEach((m) => {
    const val = m[key]
    counts[val] = (counts[val] || 0) + 1
  })
  return Object.entries(counts)
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value)
}

function computeMonthlyTrend(memories: ResearchMemory[]) {
  const months: Record<string, number> = {}
  memories.forEach((m) => {
    const d = new Date(m.created_at)
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
    months[key] = (months[key] || 0) + 1
  })
  return Object.entries(months)
    .sort(([a], [b]) => a.localeCompare(b))
    .slice(-12)
    .map(([month, count]) => ({ month, count }))
}

export function Dashboard() {
  const { t, i18n } = useTranslation()
  const { data: activeMemories, isLoading: loadingActive } = useMemories({ status: 'active' })
  const { data: allMemories } = useMemories({ status: 'all' })
  const { data: vectorCoverage } = useVectorCoverage()
  const { data: stats } = useStats()
  const { data: taxonomy } = useTaxonomy()

  const activeCount = stats?.active ?? activeMemories?.length ?? 0
  const archivedCount = stats?.archived ?? 0
  const coveragePercent = stats
    ? `${stats.vector_coverage}%`
    : vectorCoverage
      ? vectorCoverage.total > 0
        ? ((vectorCoverage.embedded / vectorCoverage.total) * 100).toFixed(1) + '%'
        : '0%'
      : '—'

  // Use stats API distribution if available, otherwise compute client-side
  const typeDistribution = stats?.type_distribution
    ? Object.entries(stats.type_distribution).map(([name, value]) => ({ name, value })).sort((a, b) => b.value - a.value)
    : allMemories ? computeDistribution(allMemories, 'memory_type') : []

  const projectDistribution = stats?.project_distribution
    ? Object.entries(stats.project_distribution).map(([name, value]) => ({ name, value })).sort((a, b) => b.value - a.value).slice(0, 8)
    : allMemories ? computeDistribution(allMemories, 'project').slice(0, 8) : []

  const monthlyTrend = allMemories ? computeMonthlyTrend(allMemories) : []

  const recentMemories = activeMemories
    ? [...activeMemories].sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()).slice(0, 5)
    : []

  return (
    <div className="flex-1 space-y-6 p-6 md:p-8 pt-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between animate-fade-in">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">{t('dashboard.title')}</h1>
          <p className="text-muted-foreground mt-1">{t('dashboard.subtitle')}</p>
        </div>
        <Link to="/memories/new" className="hidden sm:block">
          <Button>
            <Plus className="mr-2 h-4 w-4" /> {t('memories.new_memory')}
          </Button>
        </Link>
      </div>

      {/* Stats Grid */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <StatCard
          title={t('dashboard.stat_active_memories')}
          value={loadingActive ? '...' : activeCount.toLocaleString()}
          icon={Database}
        />
        <StatCard
          title={t('dashboard.stat_archived_memories')}
          value={archivedCount.toLocaleString()}
          icon={BrainCircuit}
        />
        <StatCard
          title={t('dashboard.stat_vector_coverage')}
          value={coveragePercent}
          icon={Activity}
          description={vectorCoverage ? `${vectorCoverage.embedded}/${vectorCoverage.total}` : undefined}
        />
        <StatCard
          title={t('dashboard.stat_last_updated')}
          value={recentMemories[0] ? new Date(recentMemories[0].updated_at).toLocaleDateString() : '—'}
          icon={Clock}
        />
      </div>

      {/* Charts Row */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-7">
        {/* Trend Chart */}
        <Card className="col-span-full lg:col-span-4">
          <CardHeader>
            <CardTitle className="text-base">{t('dashboard.chart_trends')}</CardTitle>
            <CardDescription>{t('dashboard.subtitle')}</CardDescription>
          </CardHeader>
          <CardContent className="pl-2">
            <div className="h-[280px] w-full">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={monthlyTrend} margin={{ top: 10, right: 30, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="colorCount" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="var(--chart-1)" stopOpacity={0.3}/>
                      <stop offset="95%" stopColor="var(--chart-1)" stopOpacity={0}/>
                    </linearGradient>
                  </defs>
                  <XAxis dataKey="month" stroke="var(--muted-foreground)" fontSize={12} tickLine={false} axisLine={false} />
                  <YAxis stroke="var(--muted-foreground)" fontSize={12} tickLine={false} axisLine={false} />
                  <Tooltip
                    contentStyle={{
                      borderRadius: '8px',
                      border: '1px solid var(--border)',
                      backgroundColor: 'var(--background)',
                      color: 'var(--foreground)',
                      fontSize: '12px',
                    }}
                  />
                  <Area type="monotone" dataKey="count" stroke="var(--chart-1)" fillOpacity={1} fill="url(#colorCount)" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>

        {/* Type Distribution Pie */}
        <Card className="col-span-full lg:col-span-3">
          <CardHeader>
            <CardTitle className="text-base">{t('dashboard.chart_types')}</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="h-[220px] w-full">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={typeDistribution}
                    cx="50%"
                    cy="50%"
                    innerRadius={50}
                    outerRadius={80}
                    paddingAngle={2}
                    dataKey="value"
                  >
                    {typeDistribution.map((_, index) => (
                      <Cell key={`cell-${index}`} fill={CHART_COLORS[index % CHART_COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip
                    contentStyle={{
                      borderRadius: '8px',
                      border: '1px solid var(--border)',
                      backgroundColor: 'var(--background)',
                      color: 'var(--foreground)',
                      fontSize: '12px',
                    }}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>
            <div className="flex flex-wrap gap-2 mt-2 justify-center">
              {typeDistribution.map((item, index) => (
                <div key={item.name} className="flex items-center gap-1.5 text-xs">
                  <div
                    className="w-2.5 h-2.5 rounded-full"
                    style={{ background: CHART_COLORS[index % CHART_COLORS.length] }}
                  />
                  <span className="text-muted-foreground">{formatMemoryType(item.name, taxonomy?.memory_types, i18n.language)}</span>
                  <span className="font-medium">{item.value}</span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Bottom Row: Project Distribution + Recent Activity */}
      <div className="grid gap-4 md:grid-cols-2">
        {/* Project Bar Chart */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">{t('dashboard.chart_projects')}</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="h-[240px] w-full">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={projectDistribution} layout="vertical" margin={{ left: 80 }}>
                  <XAxis type="number" fontSize={12} tickLine={false} axisLine={false} stroke="var(--muted-foreground)" />
                  <YAxis type="category" dataKey="name" fontSize={11} tickLine={false} axisLine={false} stroke="var(--muted-foreground)" width={75} />
                  <Tooltip
                    contentStyle={{
                      borderRadius: '8px',
                      border: '1px solid var(--border)',
                      backgroundColor: 'var(--background)',
                      color: 'var(--foreground)',
                      fontSize: '12px',
                    }}
                  />
                  <Bar dataKey="value" fill="var(--chart-1)" radius={[0, 4, 4, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>

        {/* Recent Activity */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle className="text-base">{t('dashboard.recent_edits')}</CardTitle>
              <CardDescription>{t('dashboard.quick_actions')}</CardDescription>
            </div>
            <Link to="/memories">
              <Button variant="ghost" size="sm">
                <ArrowRight className="h-4 w-4" />
              </Button>
            </Link>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {recentMemories.length === 0 && (
                <p className="text-sm text-muted-foreground text-center py-8">{t('common.loading')}</p>
              )}
              {recentMemories.map((memory, i) => (
                <Link
                  key={memory.memory_id}
                  to={`/memories/${memory.memory_id}` as string}
                  className="flex items-center gap-3 p-2 -mx-2 rounded-lg hover:bg-muted/50 transition-colors stagger-item group"
                  style={{ animationDelay: `${i * 0.04}s` }}
                >
                  <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center shrink-0 group-hover:bg-primary/15 transition-colors">
                    <Database className="w-3.5 h-3.5 text-primary" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium leading-none truncate">{memory.title}</p>
                    <p className="text-xs text-muted-foreground mt-1 truncate">
                      {memory.project} · {memory.topic}
                    </p>
                  </div>
                  <Badge variant="outline" className="shrink-0 text-[10px]">
                    {formatMemoryType(memory.memory_type, taxonomy?.memory_types, i18n.language)}
                  </Badge>
                </Link>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
