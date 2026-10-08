/* Тексты для сравнения чисел. */

import { num } from '../format';
import { OPERATOR_WORD, unitSuffix } from './shared';
import type { JournalNoun } from './journal';

// ── Сравнение числовых наблюдений ─────────────────────────────────────────

// Состояние ячейки — итог сверки числа с распознанным пределом, а не статус
// утверждения: поэтому свои слова, а не STATUS_SHORT. «не сверено» — сверка не
// состоялась (предела нет, единицы не совпали, число не разобралось); зелёного
// вывода о соответствии в этом случае не бывает.
export const COMPARE_CELL_LABELS: Record<
  'novalue' | 'unchecked' | 'outside' | 'within',
  string
> = {
  novalue: 'наблюдения нет',
  unchecked: 'не сверено',
  outside: 'вне предела',
  within: 'в пределе',
};

// Итог строки сравнения: «сверки выдержаны» — только когда прошла хотя бы одна
// сверка; иначе «нечего сверять», а не вывод о соответствии.
export const COMPARE_ROW_LABELS: Record<'passed' | 'outside' | 'nothing', string> = {
  passed: 'сверки выдержаны',
  outside: 'вне предела',
  nothing: 'нечего сверять',
};

// Откуда взят предел: из знака в самом наблюдении или из его слов. Экран
// называет это распознавание, а не поле корпуса, и пишет его открытой строкой
// рядом с меткой предела, а не только подсказкой (на тач-устройстве её нет).
export const COMPARE_LIMIT_SOURCE: Record<'operator' | 'wording', string> = {
  operator: 'взяли из знака в тексте наблюдения',
  wording: 'направление распознали по словам «не выше» и «не ниже»',
};

// Легенда цветовых итогов над таблицей: смысл метки читается там же, где сама
// метка, а не только в справке внизу страницы. Серый цвет нарочно назван
// «сверять нечем», а не хорошим результатом.
export const COMPARE_LEGEND: {
  pill: 'consensus' | 'disputed' | 'hypothesis' | 'off';
  label: string;
  note: string;
}[] = [
  {
    pill: 'consensus',
    label: COMPARE_CELL_LABELS.within,
    note: 'число уложилось в предел из источника',
  },
  { pill: 'disputed', label: COMPARE_CELL_LABELS.outside, note: 'число вышло за предел' },
  {
    pill: 'hypothesis',
    label: COMPARE_CELL_LABELS.unchecked,
    note: 'сверять нечем: предела нет, единицы не совпали или число не разобралось',
  },
  { pill: 'off', label: COMPARE_ROW_LABELS.nothing, note: 'в строке ни одной удачной сверки' },
];

/** Нарушение предела одной человекочитаемой фразой: единица названа один раз,
 *  направление сказано словом, а не математическим знаком.
 *  «отторжение солей: 97 %, а предел не больше 95 %». */
export function outsideLimitText(
  property: string,
  value: number,
  unit: string,
  limitKind: 'lte' | 'gte',
  limitAt: number,
): string {
  const suffix = unitSuffix(unit);
  return `${property}: ${num(value)}${suffix}, а предел ${OPERATOR_WORD[limitKind]} ${num(limitAt)}${suffix}`;
}

/** Предел подписью при самом себе: то же слово направления, что в нарушение,
 *  и без знака, который читается только с догадкой. */
export function limitTagText(property: string, limit: { kind: 'lte' | 'gte'; at: number; unit: string }): string {
  const suffix = unitSuffix(limit.unit);
  return `${property}: ${OPERATOR_WORD[limit.kind]} ${num(limit.at)}${suffix}`;
}
export const FEEDBACK_VERDICTS: { key: 'accept' | 'reject' | 'correct'; label: string }[] = [
  { key: 'accept', label: 'Принять ответ' },
  { key: 'reject', label: 'Отклонить ответ' },
  { key: 'correct', label: 'Нужна правка' },
];
