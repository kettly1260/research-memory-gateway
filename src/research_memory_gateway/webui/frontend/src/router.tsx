import { lazy, Suspense, type ComponentType } from 'react'
import { createRootRoute, createRoute, createRouter, Outlet } from '@tanstack/react-router'
import { AppShell } from './components/layout/AppShell'
import { useTranslation } from 'react-i18next'

const Dashboard = lazy(() => import('./pages/Dashboard').then((module) => ({ default: module.Dashboard })))
const Memories = lazy(() => import('./pages/Memories').then((module) => ({ default: module.Memories })))
const MemoryDetail = lazy(() => import('./pages/MemoryDetail').then((module) => ({ default: module.MemoryDetail })))
const MemoryNew = lazy(() => import('./pages/MemoryNew').then((module) => ({ default: module.MemoryNew })))
const Proposals = lazy(() => import('./pages/Proposals').then((module) => ({ default: module.Proposals })))
const Login = lazy(() => import('./pages/Login').then((module) => ({ default: module.Login })))
const Config = lazy(() => import('./pages/Config').then((module) => ({ default: module.Config })))
const Security = lazy(() => import('./pages/Security').then((module) => ({ default: module.Security })))
const ImportPage = lazy(() => import('./pages/Import').then((module) => ({ default: module.ImportPage })))
const ExportsPage = lazy(() => import('./pages/Export').then((module) => ({ default: module.ExportsPage })))
const Audit = lazy(() => import('./pages/Audit').then((module) => ({ default: module.Audit })))

function lazyRoute(Component: ComponentType) {
  return function LazyRouteComponent() {
    const { t } = useTranslation()
    return (
      <Suspense fallback={<div className="p-6 md:p-8 animate-fade-in text-sm text-muted-foreground">{t('common.loading')}</div>}>
        <Component />
      </Suspense>
    )
  }
}

const rootRoute = createRootRoute({
  component: () => <Outlet />,
})

const authRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: 'authenticated',
  component: AppShell,
})

const indexRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/',
  component: lazyRoute(Dashboard),
})

const memoriesRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/memories',
  component: lazyRoute(Memories),
})

const memoryNewRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/memories/new',
  component: lazyRoute(MemoryNew),
})

const memoryDetailRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/memories/$id',
  component: lazyRoute(MemoryDetail),
})

const proposalsRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/proposals',
  component: lazyRoute(Proposals),
})

const configRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/config',
  component: lazyRoute(Config),
})

const securityRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/security',
  component: lazyRoute(Security),
})

const importRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/import',
  component: lazyRoute(ImportPage),
})

const exportsRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/exports',
  component: lazyRoute(ExportsPage),
})

const auditRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/audit',
  component: lazyRoute(Audit),
})

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  component: lazyRoute(Login),
})

const routeTree = rootRoute.addChildren([
  authRoute.addChildren([
    indexRoute,
    memoryNewRoute,
    memoryDetailRoute,
    memoriesRoute,
    proposalsRoute,
    configRoute,
    securityRoute,
    importRoute,
    exportsRoute,
    auditRoute,
  ]),
  loginRoute,
])

export const router = createRouter({ 
  routeTree,
  basepath: '/admin',
})

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}
