// ─── Memory Types ───

export type MemoryStatus = 'active' | 'archived' | 'deleted'
export type MemoryType = 'literature_review' | 'paper_note' | 'synthesis_route' | 'experiment_plan' | 'mechanism_hypothesis' | 'material_system' | 'presentation_outline' | 'research_decision'
export type VerificationStatus = 'evidence_backed' | 'inferred' | 'unverified' | 'conflicting' | 'superseded' | 'retracted'

export interface Claim {
  claim: string
  verification_status: VerificationStatus
  source?: string
  confidence?: number
}

export interface Evidence {
  source: string
  content: string
  relevance_score?: number
  metadata?: Record<string, unknown>
}

export interface ResearchMemory {
  memory_id: string
  project: string
  topic: string
  memory_type: MemoryType
  title: string
  summary: string
  tags: string[]
  claims: Claim[]
  evidence: Evidence[]
  memory_status: MemoryStatus
  created_at: string
  updated_at: string
  metadata?: Record<string, unknown>
}

// ─── API Response Types ───

export interface MemoriesListResponse {
  items: ResearchMemory[]
}

export interface ProjectsResponse {
  projects: string[]
}

export interface OverlapCheckResponse {
  items: Array<{
    memory_id: string
    title: string
    similarity: number
  }>
}

export interface DiffResponse {
  diff: string
}

// ─── Config Types ───

export interface ConfigValue {
  value: unknown
  source: string
}

export interface EffectiveConfig {
  retrieval: {
    mode: ConfigValue
    [key: string]: ConfigValue
  }
  embedding: {
    enabled: ConfigValue
    base_url: ConfigValue
    model: ConfigValue
    endpoint_path: ConfigValue
    timeout_seconds: ConfigValue
    max_retries: ConfigValue
    api_key: ConfigValue
    [key: string]: ConfigValue
  }
  rerank: {
    enabled: ConfigValue
    base_url: ConfigValue
    model: ConfigValue
    endpoint_path: ConfigValue
    timeout_seconds: ConfigValue
    max_retries: ConfigValue
    api_key: ConfigValue
    [key: string]: ConfigValue
  }
  nocturne: {
    transport: ConfigValue
    url: ConfigValue
    token: ConfigValue
    [key: string]: ConfigValue
  }
}

// ─── Vector Coverage ───

export interface VectorCoverageResponse {
  total: number
  embedded: number
  missing: number
}

// ─── Backfill ───

export interface BackfillJob {
  job_id: string
  status: 'running' | 'completed' | 'failed' | 'cancelled' | 'completed_with_errors'
  total: number
  completed: number
  failed: number
  skipped: number
  started_at: string
  updated_at: string
  last_error: string | null
  cancel_requested: boolean
}

// ─── Import/Export ───

export interface ImportValidationResult {
  valid: number
  invalid: number
  duplicates: number
  errors: Array<{ index: number; error: string }>
}

export interface ImportExecutionResult {
  imported: number
  skipped: number
}

export interface ExportResult {
  count: number
  [key: string]: unknown
}

// ─── Config Test ───

export interface ConnectionTestResult {
  ok: boolean
  status: string
  latency_ms?: number
  error?: string
}

export interface ModelsResponse {
  ok: boolean
  models: string[]
  status?: string
}

// ─── Stats ───

export interface DashboardStats {
  active_count: number
  archived_count: number
  vector_coverage: { total: number; embedded: number; missing: number }
  recent_memories: ResearchMemory[]
  type_distribution: Record<string, number>
  project_distribution: Record<string, number>
}

// ─── Audit ───

export interface AuditEvent {
  event_id: string
  event_type: string
  actor?: string | null
  memory_id?: string
  metadata?: Record<string, unknown>
  created_at: string
}

// ─── Stats API Response ───

export interface StatsResponse {
  total: number
  active: number
  archived: number
  deleted: number
  embedded: number
  vector_coverage: number
  type_distribution: Record<string, number>
  project_distribution: Record<string, number>
}
