import { createRootRoute, createRoute, createRouter, Outlet } from '@tanstack/react-router'
import { AppShell } from './components/layout/AppShell'
import { Dashboard } from './pages/Dashboard'
import { Memories } from './pages/Memories'
import { MemoryDetail } from './pages/MemoryDetail'
import { MemoryNew } from './pages/MemoryNew'
import { Login } from './pages/Login'
import { Config } from './pages/Config'
import { Security } from './pages/Security'
import { ImportPage } from './pages/Import'
import { ExportsPage } from './pages/Export'
import { Audit } from './pages/Audit'

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
  component: Dashboard,
})

const memoriesRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/memories',
  component: Memories,
})

const memoryNewRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/memories/new',
  component: MemoryNew,
})

const memoryDetailRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/memories/$id',
  component: MemoryDetail,
})

const configRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/config',
  component: Config,
})

const securityRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/security',
  component: Security,
})

const importRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/import',
  component: ImportPage,
})

const exportsRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/exports',
  component: ExportsPage,
})

const auditRoute = createRoute({
  getParentRoute: () => authRoute,
  path: '/audit',
  component: Audit,
})

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  component: Login,
})

const routeTree = rootRoute.addChildren([
  authRoute.addChildren([
    indexRoute,
    memoryNewRoute,
    memoryDetailRoute,
    memoriesRoute,
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
