/**
 * Числовая домена раздела «Числа»: одна реализация на три вида экрана.
 *
 * Таблица, расхождения и пробелы читают один срез корпуса и один тип
 * `FindingListItem`, а значит и арифметика «сходятся ли числа» у них одна:
 * ключ группировки по единице, распознавание предела, сверка значения с
 * пределом, размах полосы и относительная разница. Раньше это жило двумя
 * копиями в двух маршрутах и расходилось в мелочах, поэтому здесь ровно один
 * источник истины.
 *
 * Правила модуля:
 * — никаких импортов Svelte и никакого DOM: функции чистые и проверяемые;
 * — ни одного выдуманного числа: чего нет в данных, того нет и в результате
 *   (`null`, а не ноль и не округление);
 * — относительная разница возвращается вместе со знаменателем, которым она
 *   посчитана: экран обязан назвать знаменатель в той же фразе.
 */

import { num } from './format';
import {
  OPERATOR_WORD,
  PREDICATE_LABELS,
  PROPERTY_LABELS,
  SUBJECT_LABELS,
  knownTerm,
  unitKey,
  unitLabel,
} from './terms';
import type { Evidence, FindingListItem, NumericObservation } from './types';

// ── Значение наблюдения ────────────────────────────────────────────────────

/** Границы значения наблюдения: у диапазона обе, у точки одна. */
export interface Bounds {
  lo: number;
  hi: number;
}

/**
 * Ключ группировки по единице: «mg/L» и «мг/л» — одна шкала, а не две.
 * Неизвестная единица остаётся своей записью: свести её с другой было бы хуже,
 * чем показать две шкалы порознь.
 */
export function unitKeyOf(observation: NumericObservation): string {
  return unitKey(observation.normalized_unit || observation.unit);
}

/** Единица для печати: канон домена, если он есть, иначе запись источника. */
export function unitTextOf(observation: NumericObservation): string {
  return unitLabel(observation.normalized_unit || observation.unit).label;
}

/**
 * Границы значения из данных наблюдения. `null` — сравнивать нечего: ни значения,
 * ни обоих краёв диапазона. Ноль здесь не подставляется.
 *
 * Нормализованные величина и край берутся вперёд сырых: полоса группируется по
 * `unitTextOf`, то есть по нормализованной единице, и положить под неё число в
 * исходной единице значит сравнить процент с граммом на тонну молча. Исходная
 * формулировка живёт в `observationText`, где она и читается как в источнике.
 *
 * Односторонний предел («не меньше 95») остаётся без сравнения намеренно: свести
 * его к точке значило бы объявить расхождением честную пару «не меньше 95» и
 * «96–97». Экран называет причину отказа тем же словарём, что и отсутствующий
 * знаменатель.
 */
export function boundsOf(observation: NumericObservation): Bounds | null {
  const lo = observation.normalized_min ?? observation.min_value;
  const hi = observation.normalized_max ?? observation.max_value;
  if (lo != null && hi != null) return { lo, hi };
  const point = observation.normalized_value ?? observation.value;
  return point == null ? null : { lo: point, hi: point };
}

/**
 * Разрыв между двумя полосами: насколько одна не дотягивается до другой. `null`
 * означает, что общий интервал есть, в том числе одна общая точка ([0,10] и
 * [10,20] не расходятся), и расхождения по определению продукта нет.
 *
 * Это единственное место, где расстояние между непересекающимися значениями
 * считается; экраны его только печатают. Односторонний предел сюда не доходит:
 * `boundsOf` отказывает ему раньше, чтобы «не меньше 95» и «96–97» не оказались
 * посчитаны расхождением из-за схемы данных.
 */
export function gapBetween(a: Bounds, b: Bounds): number | null {
  if (b.lo > a.hi) return b.lo - a.hi;
  if (a.lo > b.hi) return a.lo - b.hi;
  return null;
}

/**
 * Значение словами в одном формате со всеми экранами: «не меньше 95», «95–97».
 * У диапазона печатаются оба края, у одностороннего диапазона — предел: связка
 * «от…до» остаётся за пустым значением, чтобы верхняя граница не терялась.
 */
export function observationText(observation: NumericObservation): string {
  if (observation.operator === 'between') {
    if (observation.min_value != null && observation.max_value != null) {
      return `${num(observation.min_value)}–${num(observation.max_value)}`;
    }
    if (observation.min_value != null) return `${OPERATOR_WORD.gte} ${num(observation.min_value)}`;
    if (observation.max_value != null) return `${OPERATOR_WORD.lte} ${num(observation.max_value)}`;
    return 'нет значения';
  }
  const bounds = boundsOf(observation);
  if (!bounds) return 'нет значения';
  return `${OPERATOR_WORD[observation.operator]} ${num(observation.value ?? bounds.lo)}`;
}

/**
 * Числа из текстового значения ячейки сравнения: «92,5», «≥ 95», «85–92».
 * Сервер отдаёт значение ячейки строкой, поэтому разбор остаётся здесь, а не в
 * двух маршрутах сразу. Пустой массив значит «числа не разобраны», а не ноль.
 */
export function numbersOf(text: string): number[] {
  return [...text.matchAll(/-?\d+(?:[.,]\d+)?/g)].map((match) =>
    Number(match[0].replace(',', '.')),
  );
}

// ── Предел корпуса ─────────────────────────────────────────────────────────

export type LimitKind = 'lte' | 'gte';

/**
 * Откуда взято направление предела: из оператора наблюдения либо из его
 * формулировки. Это распознавание, а не поле корпуса, поэтому происхождение
 * возвращается вместе с пределом и экран называет его открыто.
 */
export type LimitOrigin = 'operator' | 'wording';

export interface Limit {
  kind: LimitKind;
  at: number;
  unit: string;
  from: LimitOrigin;
}

export interface PropertyLimit extends Limit {
  /** Служебное имя показателя: русское имя даёт словарь экрана. */
  property: string;
}

/**
 * Предел из одного наблюдения: направление берётся из оператора, а когда
 * оператора нет — из формулировки («≤», «не выше», «не ниже»). `null` — предела
 * в наблюдении нет, и это не значит «предел равен нулю».
 */
export function limitOf(observation: NumericObservation): PropertyLimit | null {
  const at = observation.normalized_value ?? observation.value;
  if (at == null) return null;
  const property = observation.property_name;
  const unit = observation.normalized_unit || observation.unit || '';
  if (observation.operator === 'lte' || observation.operator === 'lt') {
    return { property, kind: 'lte', at, unit, from: 'operator' };
  }
  if (observation.operator === 'gte' || observation.operator === 'gt') {
    return { property, kind: 'gte', at, unit, from: 'operator' };
  }
  const raw = observation.raw_text;
  if (/[≤<]/.test(raw) || /не (выше|более)/.test(raw)) {
    return { property, kind: 'lte', at, unit, from: 'wording' };
  }
  if (/[≥>]/.test(raw) || /выше|не (ниже|менее)/.test(raw)) {
    return { property, kind: 'gte', at, unit, from: 'wording' };
  }
  return null;
}

/**
 * Пределы по показателям. У показателя бывает несколько распознанных пределов,
 * поэтому сверка идёт по самому строгому: верхней границей берётся меньшее
 * число, нижней — большее. Разнонаправленные пределы одним числом не выразить,
 * поэтому при смене направления остаётся первый распознанный.
 */
export function limitsByProperty(
  observations: Iterable<NumericObservation>,
): Record<string, PropertyLimit> {
  // `Map`, а не объект: имя показателя приходит из разбора документа, и ключ
  // вроде `constructor` выдал бы прототип объекта вместо предела.
  const map = new Map<string, PropertyLimit>();
  for (const observation of observations) {
    const bound = limitOf(observation);
    if (!bound) continue;
    const held = map.get(bound.property);
    if (
      !held ||
      (held.kind === bound.kind &&
        (bound.kind === 'lte' ? bound.at < held.at : bound.at > held.at))
    ) {
      map.set(bound.property, bound);
    }
  }
  return Object.fromEntries(map);
}

/** Пределы всех находок окна: список для панели пределов, отсортирован по имени. */
export function limitsOfFindings(findings: readonly FindingListItem[]): PropertyLimit[] {
  const map = limitsByProperty(findings.flatMap((finding) => finding.observations));
  return Object.values(map).sort((a, b) => a.property.localeCompare(b.property, 'ru'));
}

export type LimitCheck = 'outside' | 'within' | 'none';

export interface LimitVerdict {
  check: LimitCheck;
  /** Значение, которое вышло за предел: нужно фразе нарушения. `null` при `check !== 'outside'`. */
  offending: number | null;
}

/**
 * Сверка значения с пределом: `outside` — значение за пределом того же
 * показателя и единицы, `within` — предел есть и выдержан, `none` — сверять
 * нечем (значения нет, предела нет, единицы не совпали или число не
 * разобрано). `none` никогда не выдаётся за «в пределе».
 */
export function checkAgainstLimit(
  value: string | null | undefined,
  unit: string | null | undefined,
  limit: Limit | undefined,
): LimitVerdict {
  if (!value || !limit || unitKey(unit) !== unitKey(limit.unit)) {
    return { check: 'none', offending: null };
  }
  const numbers = numbersOf(value);
  if (numbers.length === 0) return { check: 'none', offending: null };
  if (limit.kind === 'lte') {
    const worst = Math.max(...numbers);
    return worst > limit.at ? { check: 'outside', offending: worst } : { check: 'within', offending: null };
  }
  const worst = Math.min(...numbers);
  return worst < limit.at ? { check: 'outside', offending: worst } : { check: 'within', offending: null };
}

// ── Место в источнике ──────────────────────────────────────────────────────

export interface Locator {
  kind: string;
  value: string;
  /** Значение числовое: экран печатает его таблярными разрядами. */
  numeric: boolean;
}

/**
 * Место в источнике: страница, лист, диапазон ячеек. Символьные смещения сюда
 * не входят — они служебные и показываются только в блоке служебных данных.
 */
export function locatorsOf(evidence: Evidence | null | undefined): Locator[] {
  const rows: Locator[] = [];
  if (!evidence) return rows;
  if (evidence.page != null) rows.push({ kind: 'стр.', value: String(evidence.page), numeric: true });
  if (evidence.sheet) rows.push({ kind: 'лист', value: evidence.sheet, numeric: false });
  if (evidence.cell_range) {
    rows.push({ kind: 'ячейки', value: evidence.cell_range, numeric: false });
  }
  return rows;
}

/** Символьный диапазон фрагмента: только для блока служебных данных. */
export function charRangeOf(evidence: Evidence | null | undefined): string | null {
  if (!evidence) return null;
  return evidence.char_start != null && evidence.char_end != null
    ? `${evidence.char_start}–${evidence.char_end}`
    : null;
}

/** Название первоисточника: пустая строка значит «названия нет», а не «нет источника». */
export function sourceTitleOf(finding: FindingListItem): string {
  return finding.evidence.find((row) => row.source_title)?.source_title ?? '';
}

// ── Относительная разница ──────────────────────────────────────────────────

/**
 * Относительная разница вместе со знаменателем, которым она посчитана.
 * `ratio` — доля, а не проценты: 0.18 печатается как 18 %. Знаменатель
 * возвращается всегда, чтобы экран назвал его в той же фразе.
 */
export interface RelativeDelta {
  ratio: number;
  denominator: number;
}

/**
 * Размах полосы относительно наименьшего значения: знаменатель — `lo`.
 * `null` — честного знаменателя нет (наименьшее значение не положительно,
 * значения не конечны или размах отрицательный). Нуль знаменателем не
 * подставляется и другим значением не заменяется.
 */
export function spreadRatio(lo: number, hi: number): RelativeDelta | null {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
  const span = hi - lo;
  if (span < 0 || lo <= 0) return null;
  return { ratio: span / lo, denominator: lo };
}

export type DeltaMiss = 'absent' | 'zero' | 'negative';

/**
 * Исход сравнения двух величин: либо разница со своим знаменателем, либо
 * названная причина, по которой разницы нет. Причина возвращается вместо
 * `null`, чтобы экран сказал «знаменатель равен нулю», а не подставил другой.
 */
export type DeltaOutcome = { ok: true; delta: RelativeDelta } | { ok: false; miss: DeltaMiss };

/**
 * Доля от выбранного основания: знаменатель — величина самого основания.
 * Отношение знаковое: экран говорит «больше» или «меньше» и печатает модуль.
 */
export function deltaFromBase(
  base: number | null | undefined,
  other: number | null | undefined,
): DeltaOutcome {
  if (base == null || other == null || !Number.isFinite(base) || !Number.isFinite(other)) {
    return { ok: false, miss: 'absent' };
  }
  if (base === 0) return { ok: false, miss: 'zero' };
  if (base < 0) return { ok: false, miss: 'negative' };
  return { ok: true, delta: { ratio: (other - base) / base, denominator: base } };
}

// ── Полоса значений одного показателя ──────────────────────────────────────

/** Одна точка полосы: значение находки по показателю в одной единице. */
export interface ValuePoint {
  findingId: string;
  property: string;
  unit: string;
  lo: number;
  hi: number;
  text: string;
  source: string;
  locators: Locator[];
}

/** Показатель в одной единице, названный несколькими находками. */
export interface Comparison {
  key: string;
  property: string;
  unit: string;
  points: ValuePoint[];
  lo: number;
  hi: number;
  /** Абсолютный размах: `hi - lo`. */
  spread: number;
  /** Относительный размах от наименьшего значения; `null` — знаменателя нет. */
  relative: RelativeDelta | null;
  /** Общая часть диапазонов; `null` — общей части нет. */
  overlap: [number, number] | null;
  /** Диапазоны не пересекаются: это расхождение по определению продукта. */
  disjoint: boolean;
}

/**
 * Числовые сравнения списка находок: на одну полосу попадают только
 * одноимённые показатели в одинаковых единицах и из разных находок.
 * Несопоставимые величины рядом не ставятся — иначе полоса врёт.
 * Порядок — от самого широкого размаха.
 */
export function comparisonsOf(findings: readonly FindingListItem[]): Comparison[] {
  const byKey = new Map<string, ValuePoint[]>();
  for (const finding of findings) {
    // Одна находка даёт полосе одну точку: при нескольких наблюдениях того же
    // показателя берётся самое широкое, иначе полоса рисует одну находку дважды.
    const perFinding = new Map<string, ValuePoint>();
    for (const observation of finding.observations) {
      const bounds = boundsOf(observation);
      if (!bounds) continue;
      const unit = unitTextOf(observation);
      const key = `${observation.property_name}|${unit}`;
      const point: ValuePoint = {
        findingId: finding.id,
        property: observation.property_name,
        unit,
        lo: bounds.lo,
        hi: bounds.hi,
        text: observationText(observation),
        source: sourceTitleOf(finding),
        locators: locatorsOf(finding.evidence[0]),
      };
      const held = perFinding.get(key);
      if (!held || held.lo > bounds.lo || held.hi < bounds.hi) perFinding.set(key, point);
    }
    for (const [key, point] of perFinding) {
      const bucket = byKey.get(key);
      if (bucket) bucket.push(point);
      else byKey.set(key, [point]);
    }
  }

  const out: Comparison[] = [];
  for (const [key, points] of byKey) {
    if (new Set(points.map((point) => point.findingId)).size < 2) continue;
    const ordered = [...points].sort((a, b) => a.lo - b.lo || a.hi - b.hi);
    const lo = ordered[0].lo;
    const hi = ordered[ordered.length - 1].hi;
    const maxLo = Math.max(...ordered.map((point) => point.lo));
    const minHi = Math.min(...ordered.map((point) => point.hi));
    let disjoint = true;
    for (let index = 1; index < ordered.length; index += 1) {
      if (ordered[index].lo <= ordered[index - 1].hi) disjoint = false;
    }
    out.push({
      key,
      property: ordered[0].property,
      unit: ordered[0].unit,
      points: ordered,
      lo,
      hi,
      spread: hi - lo,
      relative: spreadRatio(lo, hi),
      overlap: minHi >= maxLo ? [maxLo, minHi] : null,
      disjoint,
    });
  }
  return out.sort((a, b) => b.spread - a.spread);
}

/**
 * Расхождение чисел: один показатель, одна единица, два источника и разные
 * величины. Совпавшие значения расхождением не считаются — там источники
 * расходятся в формулировке, а не в числе.
 */
export function diverges(comparison: Comparison): boolean {
  return comparison.points.length >= 2 && comparison.spread > 0;
}

/** Есть ли в списке сравнений хоть одно расхождение чисел. */
export function anyDivergence(comparisons: readonly Comparison[]): boolean {
  return comparisons.some(diverges);
}

/**
 * Доля полосы от наименьшего значения: та, у которой относительная разница
 * наибольшая. Знаменатель при ней, поэтому экран может назвать его в фразе.
 */
export function headlineOf(comparisons: readonly Comparison[]): Comparison | null {
  let best: Comparison | null = null;
  for (const comparison of comparisons) {
    if (best === null) {
      best = comparison;
      continue;
    }
    const current = comparison.relative?.ratio ?? -1;
    const held = best.relative?.ratio ?? -1;
    if (current > held) best = comparison;
  }
  return best;
}

/**
 * Положение точки на полосе в процентах её ширины. `null` — реальной шкалы нет
 * (края совпали), и полосы не рисуется: ширина всегда вычислена из значений.
 */
export function bandOf(
  point: ValuePoint,
  comparison: Comparison,
): { left: number; width: number } | null {
  const span = comparison.hi - comparison.lo;
  if (!(span > 0)) return null;
  return {
    left: ((point.lo - comparison.lo) / span) * 100,
    // Точечное значение иначе не видно на широкой полосе: минимум толщины —
    // приём рисования, а не второе число.
    width: Math.max(((point.hi - point.lo) / span) * 100, 1.5),
  };
}

// ── Темы расхождений ───────────────────────────────────────────────────────

export interface Topic {
  /** Русское имя связки либо формулировка утверждения, когда имени нет. */
  title: string;
  /** Имя взято из словаря: сырой ключ заголовком не бывает. */
  named: boolean;
  /** Служебные ключи темы — только для блока служебных данных. */
  keys: string[];
}

/** Тема находки: пара «субъект + предикат». */
export function topicOf(subject: string, predicate: string, findings: readonly FindingListItem[]): Topic {
  const keys = [subject, predicate].filter((key) => key.length > 0);
  const subjectName = knownTerm(SUBJECT_LABELS, subject);
  const predicateName = knownTerm(PREDICATE_LABELS, predicate);
  if (subjectName && predicateName) {
    return { title: `${subjectName}: ${predicateName}`, named: true, keys };
  }
  const statement = findings.find((finding) => finding.statement.trim().length > 0)?.statement.trim();
  return { title: statement ?? 'тема без формулировки', named: false, keys };
}

/** Ключ бакета темы: JSON пары ключей, разделитель не может столкнуться с данными. */
export function topicKey(finding: FindingListItem): string {
  return JSON.stringify([finding.subject ?? '', finding.predicate ?? '']);
}

export interface TopicGroup {
  /** Ключ темы, а не позиция: отбор и порядок не переключают раскрытые темы. */
  id: string;
  topic: Topic;
  findings: FindingListItem[];
  comparisons: Comparison[];
  /** Сравнение, которое задаёт число темы: наибольшая относительная разница. */
  headline: Comparison | null;
  /** Относительная разница темы; `null` — честного знаменателя нет. */
  divergence: RelativeDelta | null;
  sourceCount: number;
}

/** Темы списка находок: бакеты по паре «субъект + предикат». */
export function topicGroups(findings: readonly FindingListItem[]): TopicGroup[] {
  const buckets = new Map<string, FindingListItem[]>();
  for (const finding of findings) {
    const key = topicKey(finding);
    const bucket = buckets.get(key);
    if (bucket) bucket.push(finding);
    else buckets.set(key, [finding]);
  }
  return [...buckets.entries()].map(([key, list]) => {
    const parsed: unknown = JSON.parse(key);
    const [subject = '', predicate = ''] = Array.isArray(parsed) ? (parsed as string[]) : [];
    const comparisons = comparisonsOf(list);
    const sources = new Set<string>();
    for (const finding of list) {
      for (const evidence of finding.evidence) sources.add(evidence.document_id);
    }
    const headline = headlineOf(comparisons);
    return {
      id: key,
      topic: topicOf(subject, predicate, list),
      findings: list,
      comparisons,
      headline,
      divergence: headline?.relative ?? null,
      sourceCount: sources.size,
    };
  });
}

/**
 * Пара темы, которую подобрал корпус: стороны берутся из самого широкого
 * числового расхождения, чтобы рядом встали спорящие величины, а не две
 * случайные карточки. Аналитик вправе выбрать главный источник сам.
 */
export function widestPair(group: TopicGroup): {
  base: FindingListItem | null;
  other: FindingListItem | null;
} {
  const list = group.findings;
  let base: FindingListItem | null = list[0] ?? null;
  let other: FindingListItem | null = list[1] ?? null;
  const headline = group.comparisons[0];
  if (headline && headline.points.length >= 2) {
    const lowId = headline.points[0].findingId;
    const highId = headline.points[headline.points.length - 1].findingId;
    const low = list.find((finding) => finding.id === lowId) ?? null;
    const high = list.find((finding) => finding.id === highId) ?? null;
    if (low) base = low;
    if (high) other = high;
  }
  if (base && (!other || other.id === base.id)) {
    other = list.find((finding) => finding.id !== base.id) ?? null;
  }
  return { base, other };
}

// ── Пробелы: чего не хватает, чтобы числа сравнивались ─────────────────────

export type GapKind = 'pair' | 'unit' | 'value' | 'locator';

export interface GapNote {
  id: string;
  kind: GapKind;
  topic: Topic;
  /** Сколько утверждений в теме: нужно формулировке пробела. */
  findings: number;
  source: string;
  locators: Locator[];
  /** Единицы темы для пробела «числа в разных единицах». */
  units: string[];
}

/**
 * Пробелы окна находок: отдельного эндпоинта у сервиса нет, поэтому это ровно
 * те дыры, которые видны в загруженном списке. Четыре вида — по тому, чего не
 * хватает для сравнения чисел: второго источника, общей единицы, самого числа
 * или места в первоисточнике.
 */
export function gapNotesOf(findings: readonly FindingListItem[]): GapNote[] {
  const notes: GapNote[] = [];
  for (const group of topicGroups(findings)) {
    const first = group.findings[0];
    if (group.findings.length < 2) {
      notes.push({
        id: `${group.id}#pair`,
        kind: 'pair',
        topic: group.topic,
        findings: group.findings.length,
        locators: locatorsOf(first?.evidence[0]),
        source: first ? sourceTitleOf(first) : '',
        units: [],
      });
    }
    if (group.findings.length >= 2 && group.comparisons.length === 0) {
      // Единицы названы в данных: пробел «разные единицы» обязан показать,
      // какие именно записи не сошлись, а не сказать «шкалы нет» вообще.
      const units = [
        ...new Set(group.findings.flatMap((finding) => finding.observations.map(unitTextOf))),
      ].filter((unit) => unit.length > 0);
      notes.push({
        id: `${group.id}#unit`,
        kind: 'unit',
        topic: group.topic,
        findings: group.findings.length,
        locators: locatorsOf(first?.evidence[0]),
        source: first ? sourceTitleOf(first) : '',
        units,
      });
    }
    for (const finding of group.findings) {
      if (finding.observations.length === 0) {
        notes.push({
          id: `${finding.id}#value`,
          kind: 'value',
          topic: group.topic,
          findings: group.findings.length,
          locators: locatorsOf(finding.evidence[0]),
          source: sourceTitleOf(finding),
          units: [],
        });
      }
      if (finding.evidence.length === 0) {
        notes.push({
          id: `${finding.id}#locator`,
          kind: 'locator',
          topic: group.topic,
          findings: group.findings.length,
          locators: [],
          source: '',
          units: [],
        });
      }
    }
  }
  return notes;
}

// ── Имена показателей ──────────────────────────────────────────────────────

/** Русское имя показателя и признак, что оно взято из словаря. */
export function propertyName(property: string): { name: string; named: boolean } {
  const known = knownTerm(PROPERTY_LABELS, property);
  return known ? { name: known, named: true } : { name: property || '—', named: false };
}
