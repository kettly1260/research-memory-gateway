export const MEMORY_TYPES = [
  'literature_review',
  'paper_note',
  'synthesis_route',
  'experiment_plan',
  'mechanism_hypothesis',
  'material_system',
  'presentation_outline',
  'research_decision',
] as const

export type MemoryTypeValue = (typeof MEMORY_TYPES)[number]

export function formatMemoryType(type: string) {
  return type
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}
