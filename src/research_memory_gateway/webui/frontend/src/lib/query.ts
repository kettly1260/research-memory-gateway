/**
 * TanStack Query hooks for all API operations.
 * Provides automatic caching, refetching, loading states, and mutations.
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from './api'
import type { ResearchMemory } from '@/types/api'

// ─── Query Keys ───
export const queryKeys = {
  memories: {
    all: ['memories'] as const,
    list: (params?: Record<string, string | undefined>) => ['memories', 'list', params] as const,
    detail: (id: string) => ['memories', 'detail', id] as const,
  },
  projects: ['projects'] as const,
  config: {
    effective: ['config', 'effective'] as const,
    models: (provider: string) => ['config', 'models', provider] as const,
  },
  retrieval: {
    vectorCoverage: ['retrieval', 'vector-coverage'] as const,
    backfillJob: (id: string) => ['retrieval', 'backfill', id] as const,
  },
  audit: ['audit'] as const,
}

// ─── Memory Queries ───

export function useMemories(params?: { status?: string; query?: string; project?: string; memory_type?: string; topic?: string; tag?: string }) {
  return useQuery({
    queryKey: queryKeys.memories.list(params as Record<string, string | undefined>),
    queryFn: () => api.memories.list(params),
    select: (data) => data.items,
  })
}

export function useMemory(id: string) {
  return useQuery({
    queryKey: queryKeys.memories.detail(id),
    queryFn: () => api.memories.get(id),
    enabled: !!id,
  })
}

export function useCreateMemory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (data: Partial<ResearchMemory> & { confirmed?: boolean }) => api.memories.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.memories.all })
    },
  })
}

export function useUpdateMemory(id: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (data: Partial<ResearchMemory>) => api.memories.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.memories.all })
      queryClient.invalidateQueries({ queryKey: queryKeys.memories.detail(id) })
    },
  })
}

export function useArchiveMemory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason?: string }) => api.memories.archive(id, reason),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.memories.all })
    },
  })
}

export function useRestoreMemory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason?: string }) => api.memories.restore(id, reason),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.memories.all })
    },
  })
}

export function useSoftDeleteMemory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason?: string }) => api.memories.softDelete(id, reason),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.memories.all })
    },
  })
}

export function useHardDeleteMemory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, confirmId, password, reason }: { id: string; confirmId: string; password: string; reason: string }) =>
      api.memories.hardDelete(id, confirmId, password, reason),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.memories.all })
    },
  })
}

// ─── Projects ───

export function useProjects() {
  return useQuery({
    queryKey: queryKeys.projects,
    queryFn: () => api.projects.list(),
    select: (data) => data.projects,
  })
}

// ─── Config ───

export function useEffectiveConfig() {
  return useQuery({
    queryKey: queryKeys.config.effective,
    queryFn: () => api.config.effective(),
  })
}

export function usePatchWebConfig() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (data: Record<string, unknown>) => api.config.patchWebConfig(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.config.effective })
    },
  })
}

export function usePatchSecrets() {
  return useMutation({
    mutationFn: (data: Record<string, string>) => api.config.patchSecrets(data),
  })
}

export function useTestConnection() {
  return useMutation({
    mutationFn: (provider: string) => api.config.testConnection(provider),
  })
}

export function useFetchModels(provider: string) {
  return useMutation({
    mutationFn: (baseUrl?: string) => api.config.fetchModels(provider, baseUrl),
  })
}

// ─── Security ───

export function useChangePassword() {
  return useMutation({
    mutationFn: ({ currentPassword, newPassword }: { currentPassword: string; newPassword: string }) =>
      api.security.changePassword(currentPassword, newPassword),
  })
}

// ─── Retrieval ───

export function useVectorCoverage() {
  return useQuery({
    queryKey: queryKeys.retrieval.vectorCoverage,
    queryFn: () => api.retrieval.vectorCoverage(),
  })
}

export function useBackfillJob(jobId: string | null) {
  return useQuery({
    queryKey: queryKeys.retrieval.backfillJob(jobId || ''),
    queryFn: () => api.retrieval.backfillJob(jobId!),
    enabled: !!jobId,
    refetchInterval: (query) => {
      const job = query.state.data
      return job && job.status === 'running' ? 2000 : false
    },
  })
}

export function useBackfillStart() {
  return useMutation({
    mutationFn: (params: Record<string, unknown>) => api.retrieval.backfillStart(params),
  })
}

// ─── Import ───

export function useImportValidate() {
  return useMutation({
    mutationFn: ({ memories, policy }: { memories: unknown[]; policy?: string }) =>
      api.import.validate(memories, policy),
  })
}

export function useImportExecute() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ memories, policy, confirmed }: { memories: unknown[]; policy: string; confirmed: boolean }) =>
      api.import.execute(memories, policy, confirmed),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.memories.all })
    },
  })
}

// ─── Export ───

export function useExport() {
  return useMutation({
    mutationFn: ({ format, includeArchived, includeDeleted }: { format: string; includeArchived: boolean; includeDeleted: boolean }) =>
      api.export.create(format, includeArchived, includeDeleted),
  })
}

// ─── Audit ───

export function useAuditEvents(params?: { limit?: string; offset?: string; event_type?: string }) {
  return useQuery({
    queryKey: [...queryKeys.audit, params] as const,
    queryFn: () => api.audit.list(params),
  })
}

// ─── Stats ───

export function useStats() {
  return useQuery({
    queryKey: ['stats'] as const,
    queryFn: () => api.stats.get(),
    refetchInterval: 30000, // refresh every 30s
  })
}
