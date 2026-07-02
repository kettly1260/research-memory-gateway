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

export type MemoryTypeValue = (typeof MEMORY_TYPES)[number]

export const MEMORY_TYPE_LABELS: Record<string, { label_en: string; label_zh: string }> = {
  literature_review: { label_en: 'Literature Review', label_zh: '文献综述' },
  paper_note: { label_en: 'Paper Note', label_zh: '论文笔记' },
  synthesis_route: { label_en: 'Synthesis Route', label_zh: '合成路线' },
  experiment_plan: { label_en: 'Experiment Plan', label_zh: '实验规划' },
  mechanism_hypothesis: { label_en: 'Mechanism Hypothesis', label_zh: '机制假设' },
  material_system: { label_en: 'Material System', label_zh: '材料体系' },
  presentation_outline: { label_en: 'Presentation Outline', label_zh: '汇报提纲' },
  research_decision: { label_en: 'Research Decision', label_zh: '研究决策' },
  workflow_plan: { label_en: 'Workflow Plan', label_zh: '工作流规划' },
}

export function localizedLabel(
  items: Array<{ key: string; label_en: string; label_zh: string }> | undefined,
  key: string,
  language = 'en',
) {
  const item = items?.find((entry) => entry.key === key)
  const fallback = MEMORY_TYPE_LABELS[key]
  const label = item || fallback
  if (!label) {
    return key
      .split('_')
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' ')
  }
  return language === 'zh-CN' || language.startsWith('zh') ? label.label_zh : label.label_en
}

export function formatMemoryType(
  type: string,
  taxonomy?: Array<{ key: string; label_en: string; label_zh: string }>,
  language = 'en',
) {
  return localizedLabel(taxonomy, type, language)
}
