const HYPOTHESIS_KIND_LABELS: Record<string, string> = {
  numeric_discrepancy: 'Числовая несостыковка',
  cross_source_conflict: 'Расхождение между источниками',
  improvement_opportunity: 'Возможность улучшения',
  bottleneck: 'Возможное узкое место',
  information_gap: 'Пробел в информации',
  contradiction: 'Несостыковка',
  improvement: 'Идея улучшения',
  gap: 'Пробел в данных',
  opportunity: 'Возможность',
  risk: 'Риск',
};

export function hypothesisKindLabel(kind: string): string {
  const normalized = kind.trim().toLowerCase().replaceAll('-', '_');
  return HYPOTHESIS_KIND_LABELS[normalized] ?? kind.replaceAll('_', ' ');
}
