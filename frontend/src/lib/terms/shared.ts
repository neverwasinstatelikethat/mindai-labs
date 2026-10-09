/* Зона словаря: shared. Вырезана из terms.ts механически, состав не менялся. */

import { countOf, num, plural } from '../format';
import type {
  AgentEvent,
  ConflictCandidateStatus,
  DataClass,
  FindingApiStatus,
  IntentClassification,
  NumericObservation,
} from '../types';

/**
 * Единый словарь отображения. Сервер отдаёт значения корпуса ключами онтологии,
 * интерфейс показывает слова; служебное имя остаётся подписью при человекочитаемом
 * названии, а не заголовком.
 *
 * Словарь намеренно неполный: `knownTerm` возвращает null для неизвестного ключа,
 * чтобы экран не ставил сырой ключ в позицию заголовка. Пустой подписи не бывает —
 * `termOf` всегда отдаёт хотя бы ключ.
 */


// ── Статус утверждения ────────────────────────────────────────────────────

export const STATUS_SHORT: Record<FindingApiStatus, string> = {
  consensus: 'согласуется',
  disputed: 'оспаривается',
  hypothesis: 'не сверено',
};

// Там, где статус читают как вывод, а не как метку в строке таблицы.
export const STATUS_PHRASE: Record<FindingApiStatus, string> = {
  consensus: 'источники согласуются',
  disputed: 'источники расходятся',
  hypothesis: 'не подтверждено источниками',
};

// Заменённая версия приходит отдельным полем версии, а не статусом находки.
export const STATUS_SUPERSEDED = 'заменено';

// Подписи статусов и классов данных используются в рабочих разделах.

// ── Числовые условия ──────────────────────────────────────────────────────

export const OPERATOR_SYMBOL: Record<NumericObservation['operator'], string> = {
  eq: '=',
  lt: '<',
  lte: '≤',
  gt: '>',
  gte: '≥',
  // Прочерк зарезервирован за пустым значением, поэтому у диапазона своё знакосочетание.
  between: 'от…до',
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
  fact_search: 'поиск конкретного показателя',
  literature_review: 'обзор литературы',
  technology_comparison: 'сравнение технологий',
  contradiction_analysis: 'разбор противоречия',
  gap_analysis: 'анализ пробела',
  expert_discovery: 'поиск эксперта',
  graph_edit: 'правка графа',
  report_generation: 'подготовка отчёта',
};

// Узлы агентного прохода описаны одним словарём — `RUN_STAGES` в зоне запроса:
// стадия читается как «что сделали», а имена узлов контура остаются ключами
// потока и на экран не попадают.

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
  scripted: 'ответ собран по готовым правилам, без модели',
  unavailable: 'модель не настроена, ответ собран по найденным утверждениям',
};

// ── Ключи корпуса: субъекты, свойства, условия применения ─────────────────

export const SUBJECT_LABELS: Record<string, string> = {
  'шаблон обработки запроса': 'Шаблон обработки запросов',
  'обработка запросов': 'Обработка запросов',
  'проверка заполнения записей': 'Проверка заполнения записей',
  'очередь задач': 'Очередь задач',
  'дополнительная проверка заявки': 'Дополнительная проверка заявки',
  'reverse-osmosis': 'Обратный осмос',
  'ion-exchange': 'Ионный обмен',
  evaporation: 'Выпаривание',
  'electrodialysis': 'Электродиализ',
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
  mine_water: 'вода',
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

/** Условия применения одной строкой по локализованным названиям. */
export function describeScope(scope: Record<string, string> | undefined | null): string[] {
  if (!scope) return [];
  return Object.entries(scope).map(
    ([key, value]) => `${termOf(SCOPE_KEY_LABELS, key)}: ${termOf(SCOPE_VALUE_LABELS, value)}`,
  );
}

/** Значение + единица + оператор в одном формате для всех таблиц и находок:
 *  «не меньше 95 %», «от 95 до 97 %». Числа идут через общий `num`, чтобы
 *  десятичная запятая не расходилась с остальными экранами. Диапазон читается
 *  словами: прочерк между числами неотличим от знака пустого значения. */
export function describeValue(observation: NumericObservation): string {
  const suffix = unitSuffix(observation.normalized_unit || observation.unit);
  const min = observation.normalized_min ?? observation.min_value;
  const max = observation.normalized_max ?? observation.max_value;
  if (observation.operator === 'between' && min != null && max != null) {
    return `от ${num(min)} до ${num(max)}${suffix}`;
  }
  const at = observation.normalized_value ?? observation.value;
  if (at == null) return '—';
  return `${OPERATOR_WORD[observation.operator]} ${num(at)}${suffix}`;
}

/* ── Единицы измерения ────────────────────────────────────────────────────
   Сервер приводит единицу к канону своим словарём (`agents/workflow.py`,
   `_UNIT_CANONICAL`), но исходная запись остаётся в поле `unit`, а из
   разбора приходит и «mg/L», и «ratio», и «70 процентов». Экран печатает
   русское написание для известных единиц. Неизвестная единица остаётся как
   в источнике, чтобы не менять смысл значения. */

/** Каноническое написание по строчному ключу: регистр и алфавит — не другое измерение. */
const UNIT_LABELS: Record<string, string> = {
  '%': '%',
  percent: '%',
  '°c': '°C',
  '°с': '°C',
  ratio: 'раз',
  раз: 'раз',
  // Давление и прочность.
  па: 'Па',
  кпа: 'кПа',
  мпа: 'МПа',
  гпа: 'ГПа',
  pa: 'Па',
  kpa: 'кПа',
  mpa: 'МПа',
  gpa: 'ГПа',
  // Концентрация: масса на объём раствора.
  'г/л': 'г/л',
  'мг/л': 'мг/л',
  'мкг/л': 'мкг/л',
  'кг/л': 'кг/л',
  'g/l': 'г/л',
  'mg/l': 'мг/л',
  'ug/l': 'мкг/л',
  'µg/l': 'мкг/л',
  'mcg/l': 'мкг/л',
  'kg/l': 'кг/л',
  // Концентрация: масса на объём среды.
  'г/м³': 'г/м³',
  'мг/м³': 'мг/м³',
  'мкг/м³': 'мкг/м³',
  'кг/м³': 'кг/м³',
  'т/м³': 'т/м³',
  'g/m3': 'г/м³',
  'mg/m3': 'мг/м³',
  'ug/m3': 'мкг/м³',
  'kg/m3': 'кг/м³',
  't/m3': 'т/м³',
  // Корпус пишет кубатуру и обычной тройкой: «мг/м3» — та же единица, что и
  // «мг/м³», и разводить их двумя шкалами нельзя.
  'г/м3': 'г/м³',
  'мг/м3': 'мг/м³',
  'мкг/м3': 'мкг/м³',
  'кг/м3': 'кг/м³',
  'т/м3': 'т/м³',
  // Содержание в породе: масса на массу.
  'г/т': 'г/т',
  'мг/т': 'мг/т',
  'кг/т': 'кг/т',
  'g/t': 'г/т',
  'mg/t': 'мг/т',
  'kg/t': 'кг/т',
  // Длина.
  мм: 'мм',
  см: 'см',
  м: 'м',
  км: 'км',
  mm: 'мм',
  cm: 'см',
  m: 'м',
  km: 'км',
  // Масса и объём.
  г: 'г',
  мг: 'мг',
  мкг: 'мкг',
  кг: 'кг',
  т: 'т',
  л: 'л',
  мл: 'мл',
  g: 'г',
  mg: 'мг',
  ug: 'мкг',
  kg: 'кг',
  t: 'т',
  l: 'л',
  ml: 'мл',
  // Удельный расход энергии: единица корпуса написана с средней чертой и с
  // дефисом, и то и другое — одно измерение. Кубатуру корпус пишет и «м3».
  'квт·ч/м³': 'кВт·ч/м³',
  'квт·ч/м3': 'кВт·ч/м³',
  'квт-ч/м³': 'кВт·ч/м³',
  'квт-ч/м3': 'кВт·ч/м³',
  'квт·ч/т': 'кВт·ч/т',
  'квт-ч/т': 'кВт·ч/т',
  'квт·ч': 'кВт·ч',
  'квт-ч': 'кВт·ч',
  'kwh/m3': 'кВт·ч/м³',
  'kwh/t': 'кВт·ч/т',
  'kwh': 'кВт·ч',
};

/** Единица словом и в падеже: окончаний слишком много, чтобы сверять их
 *  посимвольно, поэтому берётся основа слова. Порядок — от длинной основы к
 *  короткой, иначе «килограмм» свернётся в «грамм». */
const UNIT_STEMS: [string, string][] = [
  ['мегапаскал', 'МПа'],
  ['килопаскал', 'кПа'],
  ['миллиграмм', 'мг'],
  ['миллилитр', 'мл'],
  ['миллиметр', 'мм'],
  ['сантиметр', 'см'],
  ['килограмм', 'кг'],
  ['процент', '%'],
  ['паскаль', 'Па'],
  ['тонна', 'т'],
  ['литр', 'л'],
  ['метр', 'м'],
  ['грамм', 'г'],
];

export type UnitLabel = { label: string; known: boolean };

/** Единица для печати: канон, если она есть в словаре домена, и исходная
 *  запись с признанием `known: false`, если нет. */
export function unitLabel(raw: string | null | undefined): UnitLabel {
  const unit = (raw ?? '').trim();
  if (!unit) return { label: '', known: true };
  // Единица приходит из разбора документа, и ключом словаря может стать что
  // угодно: «constructor» в поле unit не должен выдать прототип объекта вместо
  // написания, поэтому берётся только собственное свойство.
  const own = (key: string): string | undefined =>
    Object.prototype.hasOwnProperty.call(UNIT_LABELS, key) ? UNIT_LABELS[key] : undefined;
  const exact = own(unit);
  if (exact) return { label: exact, known: true };
  const lowered = unit.toLowerCase();
  const byCase = own(lowered);
  if (byCase) return { label: byCase, known: true };
  if (lowered.length >= 4 && /[а-яё]/.test(lowered)) {
    for (const [stem, canon] of UNIT_STEMS) {
      if (lowered.startsWith(stem)) return { label: canon, known: true };
    }
  }
  return { label: unit, known: false };
}

/** Ключ группировки: наблюдения «mg/L» и «мг/л» — одна шкала, а не две. Для
 *  неизвестной единицы ключом остаётся исходная запись: свести её с другой
 *  записью того же измерения было бы хуже, чем показать их порознь. */
export function unitKey(raw: string | null | undefined): string {
  const { label } = unitLabel(raw);
  return label.toLowerCase();
}

/** Что сказать, когда единица не свернута: шкала по такой единице строится по
 *  исходной записи, и сравнивать её с другой записью того же измерения нельзя. */
export const UNIT_AS_IN_SOURCE = 'единица напечатана как в источнике, сервис её не привёл';
/** Суффикс единицы для чисел: с каноническим написанием, без церемоний. */
export function unitSuffix(raw: string | null | undefined): string {
  const { label } = unitLabel(raw);
  return label ? ` ${label}` : '';
}


/* ── Зональные словари ────────────────────────────────────────────────────
   Единый словарь отображения остаётся этим файлом. Новые метки каждой зоны
   живут в своём помеченном блоке ниже: блок — единственное место, где этой
   зоне разрешено править файл, поэтому параллельные правки не затирают друг
   друга. Общий порядок: метка — сюда, а не в страницу. */
