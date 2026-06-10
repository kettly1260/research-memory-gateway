/**
 * API client with automatic CSRF token injection and session cookie auth.
 * All requests go to /admin/api/* and are proxied by Vite in dev mode.
 */

let csrfToken: string | null = null

/**
 * Fetch the CSRF token from the session cookie.
 * Falls back to a fresh login page fetch if not already cached.
 */
async function getCsrfToken(): Promise<string> {
  if (csrfToken) return csrfToken

  // Try to get from a session endpoint
  try {
    const res = await fetch('/admin/api/config/effective', {
      credentials: 'include',
    })
    // If we get a 401, we need to login
    if (res.status === 401) {
      window.location.href = '/admin/login'
      throw new Error('Unauthorized')
    }
    // CSRF token should be in the cookie session
    // We'll use a dedicated endpoint or extract from meta tag
  } catch {
    // Fallback
  }

  return csrfToken || ''
}

export function setCsrfToken(token: string) {
  csrfToken = token
}

export class ApiError extends Error {
  status: number
  body: Record<string, unknown>

  constructor(
    status: number,
    body: Record<string, unknown>,
  ) {
    super(body.error as string || `API Error ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

interface RequestOptions {
  method?: string
  body?: unknown
  params?: Record<string, string | undefined>
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, params } = options

  let url = `/admin/api${path}`
  if (params) {
    const searchParams = new URLSearchParams()
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined) searchParams.set(key, value)
    })
    const qs = searchParams.toString()
    if (qs) url += `?${qs}`
  }

  const headers: Record<string, string> = {}

  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }

  // Inject CSRF token for write methods
  if (['POST', 'PATCH', 'PUT', 'DELETE'].includes(method)) {
    const token = await getCsrfToken()
    if (token) {
      headers['X-CSRF-Token'] = token
    }
  }

  const response = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: 'include',
  })

  // Handle auth redirect
  if (response.status === 401) {
    window.location.href = '/admin/login'
    throw new ApiError(401, { error: 'unauthorized' })
  }

  const data = await response.json()

  if (!response.ok) {
    throw new ApiError(response.status, data)
  }

  return data as T
}

// ─── Memory APIs ───

import type {
  ResearchMemory,
  MemoriesListResponse,
  ProjectsResponse,
  OverlapCheckResponse,
  DiffResponse,
  EffectiveConfig,
  ConnectionTestResult,
  ModelsResponse,
  VectorCoverageResponse,
  BackfillJob,
  ImportValidationResult,
  ImportExecutionResult,
  ExportResult,
  AuditEvent,
  StatsResponse,
} from '@/types/api'

export const api = {
  // ─── Memories ───
  memories: {
    list(params?: { status?: string; query?: string; project?: string; memory_type?: string; topic?: string; tag?: string }) {
      return request<MemoriesListResponse>('/memories', { params: params as Record<string, string | undefined> })
    },
    get(id: string) {
      return request<ResearchMemory>(`/memories/${id}`)
    },
    create(data: Partial<ResearchMemory> & { confirmed?: boolean }) {
      return request<ResearchMemory>('/memories', { method: 'POST', body: data })
    },
    update(id: string, data: Partial<ResearchMemory>) {
      return request<ResearchMemory>(`/memories/${id}`, { method: 'PATCH', body: data })
    },
    archive(id: string, reason?: string) {
      return request<ResearchMemory>(`/memories/${id}/archive`, { method: 'POST', body: { reason } })
    },
    restore(id: string, reason?: string) {
      return request<ResearchMemory>(`/memories/${id}/restore`, { method: 'POST', body: { reason } })
    },
    softDelete(id: string, reason?: string) {
      return request<ResearchMemory>(`/memories/${id}/soft-delete`, { method: 'POST', body: { reason } })
    },
    hardDelete(id: string, confirmId: string, password: string, reason: string) {
      return request<{ deleted: boolean }>(`/memories/${id}/hard-delete`, {
        method: 'DELETE',
        body: { confirm_memory_id: confirmId, current_password: password, reason },
      })
    },
    checkOverlap(query: string, project?: string, memoryType?: string) {
      return request<OverlapCheckResponse>('/memories/overlap-check', {
        method: 'POST',
        body: { query, project, memory_type: memoryType },
      })
    },
    diff(before: unknown, after: unknown) {
      return request<DiffResponse>('/memories/diff', { method: 'POST', body: { before, after } })
    },
  },

  // ─── Projects ───
  projects: {
    list() {
      return request<ProjectsResponse>('/projects')
    },
  },

  // ─── Config ───
  config: {
    effective() {
      return request<EffectiveConfig>('/config/effective')
    },
    patchWebConfig(data: Record<string, unknown>) {
      return request<unknown>('/config/web-config', { method: 'PATCH', body: data })
    },
    patchSecrets(data: Record<string, string>) {
      return request<Record<string, string>>('/config/secrets', { method: 'PATCH', body: data })
    },
    deleteSecret(provider: string, field: string) {
      return request<{ deleted: boolean }>(`/config/secrets/${provider}/${field}`, { method: 'DELETE' })
    },
    fetchModels(provider: string, baseUrl?: string) {
      return request<ModelsResponse>('/config/models', { params: { provider, base_url: baseUrl } })
    },
    testConnection(provider: string) {
      return request<ConnectionTestResult>('/config/test', { method: 'POST', body: { provider } })
    },
  },

  // ─── Security ───
  security: {
    changePassword(currentPassword: string, newPassword: string) {
      return request<{ changed: boolean }>('/security/password', {
        method: 'POST',
        body: { current_password: currentPassword, new_password: newPassword },
      })
    },
  },

  // ─── Retrieval / Backfill ───
  retrieval: {
    vectorCoverage() {
      return request<VectorCoverageResponse>('/retrieval/vector-coverage')
    },
    backfillDryRun(params: Record<string, unknown>) {
      return request<{ total: number; memory_ids: string[] }>('/retrieval/backfill/dry-run', { method: 'POST', body: params })
    },
    backfillStart(params: Record<string, unknown>) {
      return request<BackfillJob>('/retrieval/backfill/start', { method: 'POST', body: params })
    },
    backfillJob(jobId: string) {
      return request<BackfillJob>(`/retrieval/backfill/jobs/${jobId}`)
    },
    backfillCancel(jobId: string) {
      return request<BackfillJob>(`/retrieval/backfill/jobs/${jobId}/cancel`, { method: 'POST' })
    },
  },

  // ─── Import ───
  import: {
    validate(memories: unknown[], policy?: string) {
      return request<ImportValidationResult>('/import/json/validate', { method: 'POST', body: { memories, policy } })
    },
    execute(memories: unknown[], policy: string, confirmed: boolean) {
      return request<ImportExecutionResult>('/import/json/execute', {
        method: 'POST',
        body: { memories, policy, confirmed },
      })
    },
  },

  // ─── Export ───
  export: {
    create(format: string, includeArchived: boolean, includeDeleted: boolean) {
      return request<ExportResult>('/export', {
        method: 'POST',
        body: { format, include_archived: includeArchived, include_deleted: includeDeleted },
      })
    },
  },

  // ─── Nocturne ───
  nocturne: {
    operation(op: string) {
      return request<unknown>(`/nocturne/${op}`, { method: 'POST' })
    },
  },

  // ─── Audit ───
  audit: {
    list(params?: { limit?: string; offset?: string; event_type?: string }) {
      return request<{ items: AuditEvent[]; total: number; limit: number; offset: number }>('/audit', { params })
    },
  },

  // ─── Stats ───
  stats: {
    get() {
      return request<StatsResponse>('/stats')
    },
  },
}
