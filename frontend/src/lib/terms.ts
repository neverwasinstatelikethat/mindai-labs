/**
 * Единый словарь отображения. Сервер отдаёт значения корпуса ключами онтологии,
 * интерфейс показывает слова; служебное имя остаётся подписью при человекочитаемом
 * названии, а не заголовком.
 *
 * Словарь намеренно неполный: `knownTerm` возвращает null для неизвестного ключа,
 * чтобы экран не ставил сырой ключ в позицию заголовка. Пустой подписи не бывает —
 * `termOf` всегда отдаёт хотя бы ключ.
 */

import type {
  AgentEvent,
  FindingApiStatus,
  IntentClassification,
  NumericObservation,
} from './types';

// ── Статус утверждения ────────────────────────────────────────────────────

export const STATUS_SHORT: Record<FindingApiStatus, string> = {
  consensus: 'согласуется',
  disputed: 'оспаривается',
  hypothesis: 'гипотеза',
};

// Там, где статус читают как вывод, а не как метку в строке таблицы.
export const STATUS_PHRASE: Record<FindingApiStatus, string> = {
  consensus: 'источники согласуются',
  disputed: 'источники расходятся',
  hypothesis: 'не подтверждено источниками',
};

// Заменённая версия приходит отдельным полем версии, а не статусом находки.
export const STATUS_SUPERSEDED = 'заменено';

// Классы доступа и имена прав живут в `types.ts` (CAPABILITY_LABELS,
// DATA_CLASS_LABELS): они зеркалят серверный контракт и не дублируются здесь.

// ── Числовые условия ──────────────────────────────────────────────────────

export const OPERATOR_SYMBOL: Record<NumericObservation['operator'], string> = {
  eq: '=',
  lt: '<',
  lte: '≤',
  gt: '>',
  gte: '≥',
  between: '—',
};

export const OPERATOR_WORD: Record<NumericObservation['operator'], string> = {
  eq: 'равно',
  lt: 'меньше',
  lte: 'не больше',
  gt: 'больше',
  gte: 'не меньше',
  between: 'в диапазоне',
};

// ── Намерение и узлы прохода ──────────────────────────────────────────────

export const INTENT_LABELS: Record<IntentClassification['primary'], string> = {
  fact_search: 'поиск факта',
  literature_review: 'обзор литературы',
  technology_comparison: 'сравнение технологий',
  contradiction_analysis: 'разбор противоречия',
  gap_analysis: 'анализ пробела',
  expert_discovery: 'поиск эксперта',
  graph_edit: 'правка графа',
  report_generation: 'подготовка отчёта',
};

export const AGENT_LABELS: Record<string, string> = {
  planning_agent: 'Постановка задачи',
  intent_router: 'Постановка задачи',
  action_planner: 'План запроса',
  tool_executor: 'Обход графа',
  controller: 'Проверка полноты',
  reasoner: 'Сборка тезисов',
  critic: 'Проверка ответа',
  improver: 'Ревизия ответа',
  synthesizer: 'Итоговый ответ',
  finalize: 'Итоговый ответ',
};

// Статус шага прохода: `started` сервер пока не выдаёт, но контракт его допускает.
export const STEP_STATE_LABELS: Record<AgentEvent['status'], string> = {
  started: 'выполняется',
  completed: 'готово',
  revised: 'с ревизией',
  failed: 'сбой',
};

// ── Доступ ────────────────────────────────────────────────────────────────

export const MODEL_MODE_LABELS: Record<'gigachat' | 'scripted' | 'unavailable', string> = {
  gigachat: 'ответ собран моделью',
  scripted: 'ответ собран без модели — включён служебный режим',
  unavailable: 'модель не настроена, ответ собран по найденным утверждениям',
};

// ── Ключи корпуса: субъекты, свойства, условия применения ─────────────────

export const SUBJECT_LABELS: Record<string, string> = {
  'reverse-osmosis': 'Обратный осмос',
  'ion-exchange': 'Ионный обмен',
  evaporation: 'Выпаривание',
  'electrodialysis': 'Электродиализ',
};

export const PREDICATE_LABELS: Record<string, string> = {
  HAS_SALT_REJECTION: 'Отторжение растворённых солей',
  HAS_REMOVAL_EFFICIENCY: 'Эффективность удаления',
  HAS_ENERGY_RATIO: 'Удельный расход энергии',
  REQUIRES_MIN_TEMPERATURE: 'Минимальная рабочая температура',
};

export const PROPERTY_LABELS: Record<string, string> = {
  salt_rejection: 'отторжение солей',
  removal_efficiency: 'эффективность удаления',
  energy_ratio: 'удельный расход энергии',
  min_temperature: 'минимальная температура',
  dry_residue: 'сухой остаток',
};

export const SCOPE_KEY_LABELS: Record<string, string> = {
  water_type: 'тип воды',
  climate: 'климат',
  pretreatment: 'предварительная очистка',
  origin: 'происхождение записи',
};

export const SCOPE_VALUE_LABELS: Record<string, string> = {
  mine_water: 'шахтная вода',
  cold: 'холодный климат',
  temperate: 'умеренный климат',
  demo: 'учебный пример',
};

// Типы связей графа. Схема витрины показывает русские имена, техническое имя
// связи остаётся доступным в подписи узла на карте связей.
export const RELATION_LABELS: Record<string, string> = {
  TREATED_BY: 'обрабатывается',
  PRODUCES: 'даёт',
  EXPERT_IN: 'экспертиза по',
  SUPERSEDES: 'заменяет',
  HAS_SALT_REJECTION: 'отторжение солей',
  HAS_REMOVAL_EFFICIENCY: 'эффективность удаления',
  HAS_ENERGY_RATIO: 'удельный расход энергии',
  REQUIRES_MIN_TEMPERATURE: 'требует температуры не ниже',
  CONTAINS: 'содержит',
  REQUIRES: 'требует',
  ASSERTS: 'утверждает о',
  SUPPORTED_BY: 'подтверждён документом',
  MENTIONS: 'упоминает',
  DERIVED_FROM: 'получен из',
  MEASURED_BY: 'измерено',
  APPLIES_TO: 'применимо к',
  LOCATED_IN: 'находится в',
  RUN_BY: 'выполнено',
};

// ── Доступ к термину ──────────────────────────────────────────────────────

/** Русское имя ключа или сам ключ, если словаря нет. */
export function termOf(map: Record<string, string>, key: string | null | undefined): string {
  if (!key) return '—';
  return map[key] ?? key;
}

/** Русское имя ключа или null: экран использует его, чтобы не ставить сырой
 *  ключ в заголовок, когда перевода нет. */
export function knownTerm(map: Record<string, string>, key: string | null | undefined): string | null {
  if (!key) return null;
  return map[key] ?? null;
}

/** Условия применения одной строкой: «тип воды: шахтная вода». */
export function describeScope(scope: Record<string, string> | undefined | null): string[] {
  if (!scope) return [];
  return Object.entries(scope).map(
    ([key, value]) => `${termOf(SCOPE_KEY_LABELS, key)}: ${termOf(SCOPE_VALUE_LABELS, value)}`,
  );
}

/** Значение + единица + оператор в одном формате для всех таблиц и находок. */
export function describeValue(observation: NumericObservation): string {
  const unit = observation.normalized_unit || observation.unit;
  const suffix = unit ? ` ${unit}` : '';
  const min = observation.normalized_min ?? observation.min_value;
  const max = observation.normalized_max ?? observation.max_value;
  if (observation.operator === 'between' && min != null && max != null) {
    return `${min}–${max}${suffix}`;
  }
  const at = observation.normalized_value ?? observation.value;
  if (at == null) return '—';
  return `${at}${suffix} (${OPERATOR_SYMBOL[observation.operator]})`;
}
