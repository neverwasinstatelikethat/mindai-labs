const HYPOTHESIS_KIND_LABELS: Record<string, string> = {
  numeric_discrepancy: 'Числовая несостыковка',
  cross_source_conflict: 'Расхождение между источниками',
  improvement_opportunity: 'Возможность улучшения',
  bottleneck: 'Возможное узкое место',
  information_gap: 'Пробел в информации',
  contradiction: 'Несостыковка',
  discrepancy: 'Расхождение',
  dependency: 'Зависимость',
  improvement: 'Идея улучшения',
  gap: 'Пробел в данных',
  opportunity: 'Возможность',
  risk: 'Риск',
};

/**
 * Русское имя типа гипотезы. Тип приходит свободным ключом модели: нет ключа в
 * словаре — нет и имени в интерфейсе. Формулировка утверждения говорит за
 * себя, а сырой ключ индекса остаётся служебным.
 */
export function hypothesisKindLabel(kind: string): string | null {
  const normalized = kind.trim().toLowerCase().replaceAll('-', '_');
  return HYPOTHESIS_KIND_LABELS[normalized] ?? null;
}
