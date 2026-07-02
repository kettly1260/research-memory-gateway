export const MEMORY_TYPES = [
  'literature_review',
  'paper_note',
  'synthesis_route',
  'experiment_plan',
  'mechanism_hypothesis',
  'material_system',
  'presentation_outline',
  'research_decision',
  'workflow_plan',
] as const

export const MEMORY_STATUSES = ['active', 'archived', 'deleted'] as const
export const PROPOSAL_STATUSES = ['pending', 'approved', 'rejected', 'needs_edit', 'saved', 'expired'] as const
export const PLAN_STATUSES = ['draft', 'accepted', 'active', 'superseded'] as const

export type MemoryTypeValue = (typeof MEMORY_TYPES)[number]
export type MemoryStatusValue = (typeof MEMORY_STATUSES)[number]
export type ProposalStatusValue = (typeof PROPOSAL_STATUSES)[number]
export type PlanStatusValue = (typeof PLAN_STATUSES)[number]

export interface LocalizedTaxonomyItem {
  key: string
  label_en: string
  label_zh: string
}

function humanizeKey(key: string) {
  return key
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

export function localizedLabel(
  items: LocalizedTaxonomyItem[] | undefined,
  key: string,
  language = 'en',
) {
  const item = items?.find((entry) => entry.key === key)
  if (!item) {
    return humanizeKey(key)
  }
  return language === 'zh-CN' || language.startsWith('zh') ? item.label_zh : item.label_en
}

export function formatMemoryType(
  type: string,
  taxonomy?: LocalizedTaxonomyItem[],
  language = 'en',
) {
  return localizedLabel(taxonomy, type, language)
}
