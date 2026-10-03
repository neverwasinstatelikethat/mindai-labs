<script lang="ts">
  // Расхождение — тема, где два первоисточника дают разные числа об одном
  // показателе. Тема держит обе формулировки рядом, их величины лежат на одной
  // полосе, аналитик выбирает главный источник (от него считается сравнение) и
  // фиксирует решение в «Отзывах». Сбой чтения, пустой корпус и корпус без
  // расхождений — три разных состояния, а не одно «пусто».
  // Ниже очередь на проверку: пары чисел, которые сервис нашёл, но решение по
  // ним ещё не записано. У очереди свой запрос, свои состояния и своё действие,
  // потому что она существует и тогда, когда подтверждённых расхождений нет.
  // Право подтверждать или отклонять противоречие выдаёт администратор сервиса:
  // без него пары видны, кнопок нет.
  // Шапку, skip-link и <main id="main"> рендерит +layout.svelte.
  import { api, ApiError } from '$lib/api';
  import { countOf, dateTime, num, pct } from '$lib/format';
  import { navLabel } from '$lib/nav';
  import { session } from '$lib/sessionStore.svelte';
  import {
    CONFLICTS_ACTION,
    CONFLICTS_FAILURE,
    CONFLICTS_PAGE,
    CONFLICT_FROM_MAIN,
    CONFLICT_QUEUE_ACTION,
    CONFLICT_QUEUE_CLASS_HINT,
    CONFLICT_QUEUE_FAILURE,
    CONFLICT_QUEUE_HEAD,
    CONFLICT_QUEUE_HINTS,
    CONFLICT_QUEUE_STATUS_LABELS,
    CONFLICT_QUEUE_STATUS_TONE,
    CONFLICT_QUEUE_SVC,
    CONFLICT_QUEUE_WORDS,
    CONFLICT_SCALE_WORDS,
    CONFLICT_SIDE,
    CONFLICT_TOPIC_SVC,
    CONFLICT_TOPIC_WORDS,
    GAP_KIND_LABELS,
    GAP_KIND_TITLES,
    OPERATOR_WORD,
    PREDICATE_LABELS,
    PROPERTY_LABELS,
    STATUS_SHORT,
    SUBJECT_LABELS,
    conflictsListUnloaded,
    conflictsNoDivergencePending,
    conflictsRecordNote,
    conflictsSectionLink,
    conflictsShownOf,
    describeScope,
    gapBody,
    gapsShown,
    knownTerm,
    othersShown,
    queueCounter,
    queueOutcome,
    queueWaiting,
    quotesShown,
    termOf,
    topicsShown,
    type ConflictGapKind,
  } from '$lib/terms';
  import {
    DATA_CLASS_LABELS,
    type ConflictCandidate,
    type ConflictSide,
    type DataClass,
    type Evidence,
    type FindingListItem,
    type NumericObservation,
  } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Chip from '$lib/ui/Chip.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Field from '$lib/ui/Field.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import Select from '$lib/ui/Select.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';

  const canRead = $derived(session.can('knowledge:read'));

  // UUID находки — не имя источника: когда заголовка доказательства нет,
  // экран говорит об этом прямо, а не подставляет идентификатор.
  const NO_SOURCE = 'источник не указан';

  // Экран живёт на реальном корпусе: пятьсот оспоренных находок не должны
  // превращаться в одну простыню. Темы, цитаты, значения полосы, остальные
  // утверждения темы и пробелы имеют потолок и раскрываются действием.
  const GROUP_PAGE_SIZE = 6;
  const GAP_PAGE_SIZE = 8;
  const EVIDENCE_PREVIEW = 2;
  const OTHERS_PREVIEW = 3;
  const BAND_PREVIEW = 4;
  // Сколько тем открыто по умолчанию: остальное раскрывает сам читатель.
  const FIRST_OPEN = 1;

  let items = $state<FindingListItem[]>([]);
  let status = $state<'loading' | 'ready' | 'error'>('loading');
  let failure = $state<{ title: string; text: string; denied: boolean } | null>(null);
  let loadedAt = $state<Date | null>(null);
  // Сколько утверждений в корпусе всего. Без этого «расхождений нет» неотличимо
  // от пустого корпуса; null означает, что показаний нет, и экран обязан
  // сказать это отдельно от нуля.
  let corpusClaims = $state<number | null>(null);
  // Сервер режет список тем окном: полное число оспоренных утверждений несёт
  // заголовок ответа, а не длина массива. null означает, что показаний нет, и
  // экран не вправе называть показанное ни всеми, ни неполным. Оговорка о
  // неполном чтении приходит с сервиса уже человеческим текстом.
  let conflictsTotal = $state<number | null>(null);
  let conflictsNote = $state<string | null>(null);

  // ── Отбор и раскрытие: каждый контроль реально меняет список ────────────
  let search = $state('');
  let onlyNumeric = $state(false);
  // pressed у чипа доступа значит «доступ выбран и показан» — тот же смысл,
  // что у фасетных чипов в находках. Пустой выбор = показаны все.
  let pickedClasses = $state<DataClass[]>([]);
  let sortKey = $state<string>('divergence');
  // Форма отбора под сворачиванием: смотреть темы нужно чаще, чем править
  // отбор, поэтому применённые условия остаются строкой над списком.
  let filtersOpen = $state(false);
  let collapsed = $state<Set<string>>(new Set());
  let expanded = $state<Set<string>>(new Set());
  let openQuotes = $state<Set<string>>(new Set());
  let openOthers = $state<Set<string>>(new Set());
  let openMain = $state<Set<string>>(new Set());
  let openBand = $state<Set<string>>(new Set());
  // Ключ темы → пара идентификаторов [главный источник, сверяемый]. Пока
  // записи нет, работает подобранная корпусом пара.
  let pairPicks = $state<Record<string, [string, string]>>({});
  let shownGroups = $state(GROUP_PAGE_SIZE);
  let shownGaps = $state(GAP_PAGE_SIZE);

  const CLASS_ORDER: DataClass[] = ['public', 'internal', 'restricted'];

  const SORT_OPTIONS = [
    { value: 'divergence', label: 'по величине расхождения' },
    { value: 'sources', label: 'по числу источников' },
    { value: 'subject', label: 'по названию темы' },
  ];

  interface Locator {
    kind: string;
    value: string;
    numeric: boolean;
  }

  interface Point {
    findingId: string;
    property: string;
    unit: string;
    lo: number;
    hi: number;
    text: string;
    source: string;
    locs: Locator[];
  }

  interface Comparison {
    key: string;
    property: string;
    unit: string;
    points: Point[];
    lo: number;
    hi: number;
    spread: number;
    /** Относительный размах полосы: служит порядком тем, а не выводом о базе. */
    relative: number | null;
    overlap: [number, number] | null;
    disjoint: boolean;
  }

  interface Topic {
    /** Заголовок темы: русское имя связки либо формулировка, когда имени нет. */
    title: string;
    named: boolean;
    /** Служебные ключи темы: только под раскрытием «Служебные данные». */
    keys: string[];
  }

  /**
   * Пара для сравнения: base — главный источник, который выбрал аналитик,
   * other — сверяемый. manual — пару выбрал человек, а не экран.
   */
  interface Pair {
    base: FindingListItem | null;
    other: FindingListItem | null;
    manual: boolean;
  }

  interface Group {
    id: string;
    domId: string;
    topic: Topic;
    findings: FindingListItem[];
    comparisons: Comparison[];
    /** null — относительного размаха нет: все величины не положительны. */
    divergence: number | null;
    sourceCount: number;
    headline: Comparison | null;
    /** Пара, которую подобрал корпус: самая широкая разница величин. */
    auto: Pair;
  }

  interface GapNote {
    id: string;
    kind: ConflictGapKind;
    topic: Topic;
    /** Сколько в теме утверждений: нужно формулировке пробела без общей шкалы. */
    findings: number;
    locs: Locator[];
    source: string;
  }

  /** Место в источнике: страница, лист, диапазон ячеек. Символовые оффсеты
   *  остались в служебных данных: на виду они читателю не помогают. */
  function locatorsOf(evidence: Evidence): Locator[] {
    const rows: Locator[] = [];
    if (evidence.page != null) rows.push({ kind: 'стр.', value: String(evidence.page), numeric: true });
    if (evidence.sheet) rows.push({ kind: 'лист', value: evidence.sheet, numeric: false });
    if (evidence.cell_range) rows.push({ kind: 'ячейки', value: evidence.cell_range, numeric: false });
    return rows;
  }

  /** Символовый оффсет фрагмента: появляется только под «Служебными данными». */
  function charRangeOf(evidence: Evidence): string | null {
    return evidence.char_start != null && evidence.char_end != null
      ? `${evidence.char_start}–${evidence.char_end}`
      : null;
  }

  function sourceOf(finding: FindingListItem): string {
    return finding.evidence.find((row) => row.source_title)?.source_title ?? '';
  }

  /** Имя свойства: русское, если оно есть в словаре, и технический ключ подписью. */
  function nameOf(map: Record<string, string>, key: string): { name: string; named: boolean } {
    const known = knownTerm(map, key);
    return known ? { name: known, named: true } : { name: key || '—', named: false };
  }

  /** Служебное имя показателя остаётся подсказкой: русское название важнее ключа. */
  function propHint(propertyName: string, named: boolean): string | undefined {
    return named ? `служебное имя показателя: ${propertyName}` : undefined;
  }

  function bounds(obs: NumericObservation): { lo: number; hi: number } | null {
    if (obs.min_value != null && obs.max_value != null) return { lo: obs.min_value, hi: obs.max_value };
    if (obs.value != null) return { lo: obs.value, hi: obs.value };
    if (obs.min_value != null) return { lo: obs.min_value, hi: obs.min_value };
    if (obs.max_value != null) return { lo: obs.max_value, hi: obs.max_value };
    return null;
  }

  /**
   * Число вместе с пределом в одном формате с остальными экранами:
   * «не меньше 95», «95–97». У диапазона печатаются оба края, поэтому связка
   * «от…до» остаётся за пустым значением и пределы не теряют верхнюю границу.
   * Когда из диапазона известен только один край, это предел, а не диапазон.
   */
  function obsText(obs: NumericObservation): string {
    if (obs.operator === 'between') {
      if (obs.min_value != null && obs.max_value != null) {
        return `${num(obs.min_value)}–${num(obs.max_value)}`;
      }
      if (obs.min_value != null) return `${OPERATOR_WORD.gte} ${num(obs.min_value)}`;
      if (obs.max_value != null) return `${OPERATOR_WORD.lte} ${num(obs.max_value)}`;
      return 'нет значения';
    }
    const b = bounds(obs);
    if (!b) return 'нет значения';
    return `${OPERATOR_WORD[obs.operator]} ${num(obs.value ?? b.lo)}`;
  }

  function unitOf(obs: NumericObservation): string {
    return obs.normalized_unit || obs.unit || '';
  }

  function matches(finding: FindingListItem, q: string): boolean {
    return (
      finding.statement.toLowerCase().includes(q) ||
      (finding.subject ?? '').toLowerCase().includes(q) ||
      (finding.predicate ?? '').toLowerCase().includes(q) ||
      termOf(SUBJECT_LABELS, finding.subject ?? '').toLowerCase().includes(q) ||
      termOf(PREDICATE_LABELS, finding.predicate ?? '').toLowerCase().includes(q) ||
      finding.observations.some(
        (o) => o.property_name.toLowerCase().includes(q) || o.raw_text.toLowerCase().includes(q),
      ) ||
      finding.evidence.some(
        (e) => e.source_title.toLowerCase().includes(q) || e.quote.toLowerCase().includes(q),
      )
    );
  }

  /**
   * Числовое расхождение: на одну полосу попадают только одноимённые показатели
   * в одинаковых единицах и от разных находок. Несравнимые величины рядом не
   * ставятся — иначе полоса врёт. Края полосы — min и max самих данных.
   */
  function comparisonsFor(list: FindingListItem[]): Comparison[] {
    const byProp = new Map<string, Point[]>();
    for (const finding of list) {
      const perFinding = new Map<string, Point>();
      for (const obs of finding.observations) {
        const b = bounds(obs);
        if (!b) continue;
        const unit = unitOf(obs);
        const key = `${obs.property_name}|${unit}`;
        const point: Point = {
          findingId: finding.id,
          property: obs.property_name,
          unit,
          lo: b.lo,
          hi: b.hi,
          text: obsText(obs),
          source: sourceOf(finding),
          locs: finding.evidence[0] ? locatorsOf(finding.evidence[0]) : [],
        };
        const wide = perFinding.get(key);
        if (!wide || wide.lo > b.lo || wide.hi < b.hi) perFinding.set(key, point);
      }
      for (const [key, point] of perFinding) {
        const bucket = byProp.get(key);
        if (bucket) bucket.push(point);
        else byProp.set(key, [point]);
      }
    }

    const out: Comparison[] = [];
    for (const [key, points] of byProp) {
      if (new Set(points.map((p) => p.findingId)).size < 2) continue;
      const ordered = [...points].sort((a, b) => a.lo - b.lo || a.hi - b.hi);
      const lo = ordered[0].lo;
      const hi = ordered[ordered.length - 1].hi;
      const maxLo = Math.max(...ordered.map((p) => p.lo));
      const minHi = Math.min(...ordered.map((p) => p.hi));
      const spread = hi - lo;
      let disjoint = true;
      for (let i = 1; i < ordered.length; i += 1) {
        if (ordered[i].lo <= ordered[i - 1].hi) disjoint = false;
      }
      out.push({
        key,
        property: ordered[0].property,
        unit: ordered[0].unit,
        points: ordered,
        lo,
        hi,
        spread,
        // Относительный размах полосы нужен только порядком тем: он считается от
        // наименьшего значения и не выдаётся за сравнение от главного источника.
        relative: lo > 0 && spread > 0 ? spread / lo : null,
        overlap: minHi >= maxLo ? [maxLo, minHi] : null,
        disjoint,
      });
    }
    return out.sort((a, b) => b.spread - a.spread);
  }

  /**
   * Величина расхождения темы: абсолютный размах несравним между показателями
   * (95 % и 95 кВт·ч/т нельзя ставить в один порядок), поэтому считается только
   * отношение к меньшему значению. Когда такого отношения нет, темы без числа
   * идут после измеримых, а не получают выдуманное значение.
   */
  function divergenceOf(comparisons: Comparison[]): number | null {
    let max: number | null = null;
    for (const cmp of comparisons) {
      if (cmp.relative === null) {
        if (cmp.spread === 0 && max === null) max = 0;
        continue;
      }
      if (max === null || cmp.relative > max) max = cmp.relative;
    }
    return max;
  }

  /** Полосы без реального диапазона не рисуется: совпали границы — нет и шкалы. */
  function bandStyle(point: Point, cmp: Comparison): string | null {
    const span = cmp.hi - cmp.lo;
    if (span <= 0) return null;
    const left = ((point.lo - cmp.lo) / span) * 100;
    const width = Math.max(((point.hi - point.lo) / span) * 100, 1.5);
    return `left: ${left}%; width: ${width}%`;
  }

  /**
   * Заголовок темы. Когда словарь знает связку, она называется по-русски, а
   * служебные ключи остаются под «Служебными данными». Когда не знает,
   * заголовком становится формулировка утверждения: сырой ключ заголовком
   * не бывает.
   */
  function topicFor(subject: string, predicate: string, list: FindingListItem[]): Topic {
    const keys = [subject, predicate].filter((key) => key.length > 0);
    const subjectName = knownTerm(SUBJECT_LABELS, subject);
    const predicateName = knownTerm(PREDICATE_LABELS, predicate);
    if (subjectName && predicateName) {
      return { title: `${subjectName}: ${predicateName}`, named: true, keys };
    }
    const statement = list.find((finding) => finding.statement.trim().length > 0)?.statement.trim();
    return { title: statement ?? 'тема без формулировки', named: false, keys };
  }

  /**
   * Авторская пара темы: стороны берутся из самого широкого числового
   * расхождения, чтобы рядом встали именно спорящие величины, а не случайные две
   * карточки. Аналитик вправе выбрать главный источник сам: см. pairOf.
   */
  function pairFor(
    list: FindingListItem[],
    comparisons: Comparison[],
  ): { base: FindingListItem | null; other: FindingListItem | null } {
    const headline = comparisons[0];
    let base: FindingListItem | null = list[0] ?? null;
    let other: FindingListItem | null = list[1] ?? null;
    if (headline && headline.points.length >= 2) {
      const lowId = headline.points[0].findingId;
      const highId = headline.points[headline.points.length - 1].findingId;
      const low = list.find((f) => f.id === lowId) ?? null;
      const high = list.find((f) => f.id === highId) ?? null;
      if (low) base = low;
      if (high) other = high;
    }
    const anchor = base;
    if (anchor && (!other || other.id === anchor.id)) {
      other = list.find((f) => f.id !== anchor.id) ?? null;
    }
    return { base, other };
  }

  /** Ключ темы — JSON пары ключей: разделитель не может столкнуться с данными. */
  function bucketKey(finding: FindingListItem): string {
    return JSON.stringify([finding.subject ?? '', finding.predicate ?? '']);
  }

  /** Сборка темы из бакета находок: одна и для отбора, и для полного списка. */
  function buildGroup(key: string, list: FindingListItem[]): Group {
    const parsed: unknown = JSON.parse(key);
    const [subject = '', predicate = ''] = Array.isArray(parsed) ? (parsed as string[]) : [];
    const comparisons = comparisonsFor(list);
    const sources = new Set<string>();
    for (const finding of list) {
      for (const evidence of finding.evidence) sources.add(evidence.document_id);
    }
    return {
      // ключ темы, а не позиция: сортировка и отбор не переключают свёрнутые группы
      id: key,
      // стабильный id DOM-узла группы: позиция в списке меняется отбором
      domId: `grp-${list[0]?.id ?? key}`,
      topic: topicFor(subject, predicate, list),
      findings: list,
      comparisons,
      divergence: divergenceOf(comparisons),
      sourceCount: sources.size,
      headline: comparisons[0] ?? null,
      auto: { ...pairFor(list, comparisons), manual: false },
    };
  }

  /**
   * Пара темы на экране: выбор аналитика важнее подобранной корпусом пары.
   * Если записанная находка пропала из списка, тема возвращается к паре
   * корпуса, а не остаётся с половиной.
   */
  function pairOf(group: Group): Pair {
    const pick = pairPicks[group.id];
    if (!pick) return group.auto;
    const base = group.findings.find((f) => f.id === pick[0]) ?? null;
    if (!base) return group.auto;
    const other =
      group.findings.find((f) => f.id === pick[1]) ??
      group.findings.find((f) => f.id !== base.id) ??
      null;
    return { base, other, manual: true };
  }

  /**
   * Выбор главного источника: прежний источник отходит на вторую сторону, чтобы
   * выбор одного источника не терял молча то, с чем сверяли. От выбранной
   * стороны считается сравнение: см. fromMainLabel.
   */
  function pickMain(group: Group, findingId: string): void {
    const current = pairOf(group);
    if (!current.base || current.base.id === findingId) return;
    const other =
      current.other && current.other.id !== findingId ? current.other.id : current.base.id;
    pairPicks = { ...pairPicks, [group.id]: [findingId, other] };
  }

  /** Сброс выбора: возвращается пара, которую подобрал корпус. */
  function resetMain(group: Group): void {
    if (!(group.id in pairPicks)) return;
    const next = { ...pairPicks };
    delete next[group.id];
    pairPicks = next;
  }

  function othersFor(group: Group, pair: Pair): FindingListItem[] {
    return group.findings.filter((f) => f.id !== pair.base?.id && f.id !== pair.other?.id);
  }

  /**
   * Список выбора главного источника с потолком: тема на триста находок не
   * даёт стену кнопок. Выбранные стороны остаются видимы, иначе выбор,
   * спрятанный в хвосте списка, нельзя было бы ни снять, ни проверить.
   */
  function mainOptions(group: Group, pair: Pair): FindingListItem[] {
    const preview = group.findings.slice(0, OTHERS_PREVIEW);
    if (openMain.has(group.id)) return group.findings;
    const extra = [pair.base, pair.other].filter(
      (finding): finding is FindingListItem =>
        finding != null && !preview.some((row) => row.id === finding.id),
    );
    return [...preview, ...extra];
  }

  /** Значения полосы с потолком: остальные раскрывает одна кнопка. */
  function bandPoints(cmp: Comparison): { rows: Point[]; hidden: number } {
    if (openBand.has(cmp.key)) return { rows: cmp.points, hidden: 0 };
    return {
      rows: cmp.points.slice(0, BAND_PREVIEW),
      hidden: Math.max(cmp.points.length - BAND_PREVIEW, 0),
    };
  }

  /**
   * Сравнение от главного источника. Доля считается от выбранной стороны, а не
   * от меньшего числа: выбор источника обязан менять видимые проценты. Когда
   * выбранная сторона показатель не называет либо её значение не положительное,
   * сравнения нет и это сказано прямо, а не подменено другим знаменателем.
   * `named` значит, что у фразы есть subject: его подписывают «Источник Б».
   */
  function fromMainLabel(
    cmp: Comparison,
    pair: Pair,
  ): { lead: string; value: string | null; named: boolean } {
    const basePoint = pair.base ? cmp.points.find((p) => p.findingId === pair.base?.id) : undefined;
    const otherPoint = pair.other
      ? cmp.points.find((p) => p.findingId === pair.other?.id)
      : undefined;
    if (!basePoint || !otherPoint) {
      return { lead: CONFLICT_FROM_MAIN.noPair, value: null, named: false };
    }
    if (basePoint.lo === 0) {
      return { lead: CONFLICT_FROM_MAIN.zeroBase, value: null, named: false };
    }
    if (basePoint.lo < 0) {
      return { lead: CONFLICT_FROM_MAIN.negBase, value: null, named: false };
    }
    const diff = (otherPoint.lo - basePoint.lo) / basePoint.lo;
    if (diff > 0) return { lead: CONFLICT_FROM_MAIN.more, value: pct(diff), named: true };
    if (diff < 0) return { lead: CONFLICT_FROM_MAIN.less, value: pct(-diff), named: true };
    return { lead: CONFLICT_FROM_MAIN.equal, value: null, named: true };
  }

  /** Список выбора главного источника: без версии два утверждения одного документа не различить. */
  function baseOptionLabel(finding: FindingListItem): string {
    return `${sourceOf(finding) || NO_SOURCE}, версия ${finding.version}`;
  }

  /**
   * Решение по расхождению уходит в «Отзывы» с предвыбранным утверждением:
   * главный источник аналитик уже назвал, поэтому в отзыв идёт сверяемая сторона.
   */
  function decisionHref(pair: Pair): string {
    const claim = pair.other ?? pair.base;
    return claim ? `/feedback?claim=${encodeURIComponent(claim.id)}` : '/feedback';
  }

  // Полный список без отбора: по нему считают пробелы, отбор фильтрует только
  // отображаемый список тем.
  const allGroups = $derived.by<Group[]>(() => {
    const buckets = new Map<string, FindingListItem[]>();
    for (const finding of items) {
      const key = bucketKey(finding);
      const bucket = buckets.get(key);
      if (bucket) bucket.push(finding);
      else buckets.set(key, [finding]);
    }
    return [...buckets.entries()].map(([key, list]) => buildGroup(key, list));
  });

  const groups = $derived.by<Group[]>(() => {
    const q = search.trim().toLowerCase();
    const buckets = new Map<string, FindingListItem[]>();
    for (const finding of items) {
      if (pickedClasses.length > 0 && !pickedClasses.includes(finding.data_class)) continue;
      if (q && !matches(finding, q)) continue;
      const key = bucketKey(finding);
      const bucket = buckets.get(key);
      if (bucket) bucket.push(finding);
      else buckets.set(key, [finding]);
    }

    const rows = [...buckets.entries()].map(([key, list]) => buildGroup(key, list));

    const visible = onlyNumeric ? rows.filter((g) => g.comparisons.length > 0) : rows;
    return visible.sort((a, b) => {
      if (sortKey === 'sources') {
        return b.sourceCount - a.sourceCount || a.topic.title.localeCompare(b.topic.title, 'ru');
      }
      if (sortKey === 'subject') return a.topic.title.localeCompare(b.topic.title, 'ru');
      // Темы без измеренного расхождения не подменяют его выдуманным значением —
      // они идут после измеримых.
      return (
        (b.divergence ?? -1) - (a.divergence ?? -1) ||
        b.comparisons.length - a.comparisons.length ||
        b.sourceCount - a.sourceCount ||
        a.topic.title.localeCompare(b.topic.title, 'ru')
      );
    });
  });

  const shownGroupRows = $derived(groups.slice(0, shownGroups));

  function groupOpen(index: number, group: Group): boolean {
    return expanded.has(group.id) || (index < FIRST_OPEN && !collapsed.has(group.id));
  }

  const allOpen = $derived(
    shownGroupRows.length > 0 && shownGroupRows.every((g, i) => groupOpen(i, g)),
  );

  /**
   * Пробелы: отдельного эндпоинта у сервера нет, поэтому это ровно те дыры,
   * которые видны в списке оспоренных утверждений, и названы они по данным.
   * Считаются по полному списку (allGroups): отбор фильтрует темы,
   * а не пробелы.
   */
  const gaps = $derived.by<GapNote[]>(() => {
    const notes: GapNote[] = [];
    for (const group of allGroups) {
      if (group.findings.length < 2) {
        const only = group.findings[0];
        notes.push({
          id: `${group.id}#pair`,
          kind: 'pair',
          topic: group.topic,
          findings: group.findings.length,
          locs: only?.evidence[0] ? locatorsOf(only.evidence[0]) : [],
          source: only ? sourceOf(only) : '',
        });
      }
      if (group.findings.length >= 2 && group.comparisons.length === 0) {
        notes.push({
          id: `${group.id}#scale`,
          kind: 'scale',
          topic: group.topic,
          findings: group.findings.length,
          locs: group.findings[0]?.evidence[0] ? locatorsOf(group.findings[0].evidence[0]) : [],
          source: group.findings[0] ? sourceOf(group.findings[0]) : '',
        });
      }
      for (const finding of group.findings) {
        if (finding.observations.length === 0) {
          notes.push({
            id: `${finding.id}#value`,
            kind: 'value',
            topic: group.topic,
            findings: group.findings.length,
            locs: finding.evidence[0] ? locatorsOf(finding.evidence[0]) : [],
            source: sourceOf(finding),
          });
        }
        if (finding.evidence.length === 0) {
          notes.push({
            id: `${finding.id}#locator`,
            kind: 'locator',
            topic: group.topic,
            findings: group.findings.length,
            locs: [],
            source: '',
          });
        }
      }
    }
    return notes;
  });

  const shownGapsRows = $derived(gaps.slice(0, shownGaps));

  const filtersActive = $derived(
    search.trim() !== '' || onlyNumeric || pickedClasses.length > 0 || sortKey !== 'divergence',
  );
  const sortLabel = $derived(
    SORT_OPTIONS.find((option) => option.value === sortKey)?.label ?? sortKey,
  );
  const loadedAtText = $derived(
    loadedAt ? loadedAt.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'medium' }) : '',
  );
  const classOptions = $derived.by<DataClass[]>(() => {
    const present = new Set<string>(items.map((f) => f.data_class));
    return CLASS_ORDER.filter((code) => present.has(code));
  });

  function toggleClass(code: DataClass): void {
    pickedClasses = pickedClasses.includes(code)
      ? pickedClasses.filter((row) => row !== code)
      : [...pickedClasses, code];
  }

  function showAllClasses(): void {
    pickedClasses = [];
  }

  function toggleFilters(): void {
    filtersOpen = !filtersOpen;
  }

  function openFilters(): void {
    filtersOpen = true;
  }

  function toggleGroup(index: number, group: Group): void {
    const open = groupOpen(index, group);
    const nextCollapsed = new Set(collapsed);
    const nextExpanded = new Set(expanded);
    if (open) {
      nextExpanded.delete(group.id);
      nextCollapsed.add(group.id);
    } else {
      nextCollapsed.delete(group.id);
      nextExpanded.add(group.id);
    }
    collapsed = nextCollapsed;
    expanded = nextExpanded;
  }

  function toggleAllGroups(): void {
    const ids = shownGroupRows.map((g) => g.id);
    if (allOpen) {
      collapsed = new Set([...collapsed, ...ids]);
      expanded = new Set();
    } else {
      expanded = new Set([...expanded, ...ids]);
      collapsed = new Set();
    }
  }

  function toggleQuotes(key: string): void {
    const next = new Set(openQuotes);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    openQuotes = next;
  }

  function toggleOthers(key: string): void {
    const next = new Set(openOthers);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    openOthers = next;
  }

  function toggleMain(key: string): void {
    const next = new Set(openMain);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    openMain = next;
  }

  function toggleBand(key: string): void {
    const next = new Set(openBand);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    openBand = next;
  }

  function resetFilters(): void {
    search = '';
    onlyNumeric = false;
    pickedClasses = [];
    sortKey = 'divergence';
  }

  // Смена отбора или порядка возвращает список к первой порции: под собой
  // пользователь не должен находить бесконечную простыню.
  const filterSignature = $derived(
    `${search.trim().toLowerCase()}|${onlyNumeric}|${pickedClasses.join(',')}|${sortKey}`,
  );
  $effect(() => {
    void filterSignature;
    shownGroups = GROUP_PAGE_SIZE;
    shownGaps = GAP_PAGE_SIZE;
  });

  // Строка-ориентир над списком: раздел на три части и живые числа каждой из
  // них. Знаменатель очереди остаётся пустым, пока ответ не пришёл, чтобы
  // нули не выглядели прочитанными данными.
  const marks = $derived.by(() => {
    const ready = status === 'ready' && items.length > 0;
    return {
      topics: ready ? groups.length : null,
      queue: queueStatus === 'ready' ? queueWaitingCount : null,
      gaps: ready ? gaps.length : null,
    };
  });

  /**
   * Пустой список при неподтверждённом нуле. Слово «нет» здесь было бы выводом
   * о согласных источниках, которого экран не вправе делать: полное число либо
   * обещает расхождения, либо не сообщено вовсе.
   */
  const listUnloadedNotice = $derived(conflictsListUnloaded(conflictsTotal));

  /**
   * Три разных факта, а не один «сбой»: доступ закрыт, данные не пришли,
   * список пуст. Объяснение говорит последствие и одно действие, без кодов
   * ответов и путей.
   */
  function describe(reason: unknown): { title: string; text: string; denied: boolean } {
    if (reason instanceof ApiError && reason.status === 403) {
      return {
        title: CONFLICTS_FAILURE.deniedTitle,
        text: CONFLICTS_FAILURE.deniedBody,
        denied: true,
      };
    }
    if (reason instanceof ApiError && (reason.status === 502 || reason.status === 503)) {
      return {
        title: CONFLICTS_FAILURE.failedTitle,
        text: CONFLICTS_FAILURE.failedBody,
        denied: false,
      };
    }
    return {
      title: CONFLICTS_FAILURE.loadTitle,
      text: CONFLICTS_FAILURE.loadBody,
      denied: false,
    };
  }

  let requestSeq = 0;
  async function load(): Promise<void> {
    const call = ++requestSeq;
    status = 'loading';
    failure = null;
    // Показания окна относятся к тому списку, который он опишет: при повторе и
    // при сбое чтения их нет, поэтому они сбрасываются, а не доедают старый
    // знаменатель. Тот же порядок, что у очереди на проверку.
    conflictsTotal = null;
    conflictsNote = null;
    // Объём корпуса читаем параллельно и отдельно: без него нечем отличить
    // пустой корпус от корпуса без расхождений, а его сбой не имеет права
    // превращать пустой список в вывод о согласных источниках.
    const claimsPromise = api
      .corpusStats()
      .then((stats) => stats.claims)
      .catch(() => null);
    try {
      const page = await api.conflicts();
      const claims = await claimsPromise;
      if (call !== requestSeq) return;
      items = page.items;
      conflictsTotal = page.total;
      // Пустая строка у замечания значит, что его не было: состояние остаётся
      // null, чтобы экран не выводил пустую полосу вместо текста сервиса.
      conflictsNote = page.windowNote === '' ? null : page.windowNote;
      corpusClaims = claims;
      collapsed = new Set();
      expanded = new Set();
      openQuotes = new Set();
      openOthers = new Set();
      openMain = new Set();
      openBand = new Set();
      pairPicks = {};
      shownGroups = GROUP_PAGE_SIZE;
      shownGaps = GAP_PAGE_SIZE;
      loadedAt = new Date();
      status = 'ready';
    } catch (reason) {
      if (call !== requestSeq) return;
      // Показанное остаётся на экране: сбой следующего чтения не вычёркивает
      // темы, которые человек уже видел, и не превращает список в пустой.
      failure = describe(reason);
      status = 'error';
    }
  }

  // ── Очередь на проверку: что ждёт решения эксперта ───────────────────────
  // Свой запрос, своё состояние и своё действие: очередь обязана открываться и
  // тогда, когда подтверждённых расхождений в корпусе ещё нет.
  const QUEUE_WINDOW = 20;
  const QUEUE_PREVIEW = 4;

  let candidates = $state<ConflictCandidate[]>([]);
  let queueStatus = $state<'loading' | 'ready' | 'error'>('loading');
  let queueFailure = $state<{ title: string; text: string; denied: boolean } | null>(null);
  // Полное число пар приходит заголовком ответа. null означает, что показаний
  // нет: это «неизвестно», а не «ноль пар».
  let queueTotal = $state<number | null>(null);
  let queueLoadedAt = $state<Date | null>(null);
  let queueBusy = $state(false);
  let shownPairs = $state(QUEUE_PREVIEW);
  // Решение по паре пишется на месте: активна только одна запись за раз.
  let pendingId = $state('');
  let pendingChoice = $state(false);
  // Итог записанного решения показан в подвале той же карточки, где он и
  // произошёл: сообщение вверху очереди терялось при длинном списке.
  let cardMessage = $state<{ id: string; tone: 'ok' | 'warn' | 'error' | 'info'; text: string } | null>(
    null,
  );
  // Сбой дозагрузки очереди относится к списку целиком, поэтому он у списка.
  let queueMessage = $state<{ tone: 'error'; text: string } | null>(null);

  // Право на решение серверное: признак приходит с /api/v1/auth/me вместе с
  // остальными правами, того же достаточно здесь — без клиентского зеркала ролей.
  const canDecide = $derived(session.can('proposal:review'));

  const shownQueueRows = $derived(candidates.slice(0, shownPairs));
  const queueWaitingCount = $derived(
    candidates.filter((candidate) => candidate.status === 'candidate').length,
  );
  const queueLeft = $derived(
    queueTotal === null ? 0 : Math.max(queueTotal - candidates.length, 0),
  );
  const queueHasMore = $derived(candidates.length > 0 && queueLeft > 0);
  // Одна фраза о неполноте очереди: часть пар не показана, и это видно всегда.
  const queueNotAll = $derived(queueLeft > 0 || shownQueueRows.length < candidates.length);
  const queueLoadedAtText = $derived(
    queueLoadedAt
      ? queueLoadedAt.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'medium' })
      : '',
  );

  function pairTopic(candidate: ConflictCandidate): Topic {
    const keys = [candidate.subject, candidate.property_name].filter((key) => key.length > 0);
    const subjectName = knownTerm(SUBJECT_LABELS, candidate.subject);
    const propertyName = knownTerm(PROPERTY_LABELS, candidate.property_name);
    if (subjectName && propertyName) {
      return { title: `${subjectName}: ${propertyName}`, named: true, keys };
    }
    const known = subjectName ?? propertyName;
    if (known) return { title: known, named: true, keys };
    // Служебный ключ заголовком не бывает: без словаря пару называют по
    // формулировке утверждения.
    const statement = [candidate.left.statement, candidate.right.statement]
      .find((text) => text.trim().length > 0)
      ?.trim();
    return { title: statement ?? CONFLICT_QUEUE_WORDS.noTopic, named: false, keys };
  }

  /** Первоисточник пары: тот же список «Находок» по документу, что и в ответе. */
  function documentHref(documentId: string): string {
    const params = new URLSearchParams();
    params.set('document', documentId);
    return `/findings?${params.toString()}`;
  }

  function docHint(documentId: string): string {
    return CONFLICT_QUEUE_WORDS.docCode(documentId);
  }

  /**
   * Отказ по-человечески: последствие, одно действие и тон. Повторная запись
   * уже принятого решения не ошибка, а состояние пары: она отдаётся тоном info,
   * и очередь перечитывается, чтобы записанное решение стало видно.
   */
  function decisionFailure(
    reason: unknown,
  ): { tone: 'warn' | 'error' | 'info'; text: string; refresh: boolean } {
    if (reason instanceof ApiError && reason.status === 403) {
      return { tone: 'warn', text: CONFLICT_QUEUE_FAILURE.denied, refresh: false };
    }
    if (reason instanceof ApiError && reason.status === 404) {
      return { tone: 'warn', text: CONFLICT_QUEUE_FAILURE.gone, refresh: true };
    }
    if (reason instanceof ApiError && reason.status === 409) {
      return { tone: 'info', text: CONFLICT_QUEUE_FAILURE.duplicate, refresh: true };
    }
    return { tone: 'error', text: CONFLICT_QUEUE_FAILURE.transport, refresh: false };
  }

  function describeQueue(reason: unknown): { title: string; text: string; denied: boolean } {
    if (reason instanceof ApiError && reason.status === 403) {
      return {
        title: CONFLICT_QUEUE_WORDS.queueDeniedTitle,
        text: CONFLICT_QUEUE_FAILURE.queueDenied,
        denied: true,
      };
    }
    return {
      title: CONFLICT_QUEUE_WORDS.queueFailedTitle,
      text: CONFLICT_QUEUE_FAILURE.queueFailed,
      denied: false,
    };
  }

  let queueSeq = 0;
  /** `append` дозагружает следующую часть, не стирая загруженное при отказе. */
  async function loadQueue(append = false): Promise<void> {
    const call = ++queueSeq;
    queueMessage = null;
    if (append) queueBusy = true;
    else {
      queueStatus = 'loading';
      queueFailure = null;
    }
    try {
      const page = await api.conflictCandidates(QUEUE_WINDOW, append ? candidates.length : 0);
      if (call !== queueSeq) return;
      candidates = append ? [...candidates, ...page.items] : page.items;
      queueTotal = page.total;
      queueLoadedAt = new Date();
      queueStatus = 'ready';
      // Обновление не сворачивает список обратно к первой четвёрке: раскрытое
      // человек уже видел, и после записи решения оно обязано остаться на месте.
      if (!append) shownPairs = Math.max(QUEUE_PREVIEW, shownPairs);
    } catch (reason) {
      if (call !== queueSeq) return;
      if (append) {
        queueMessage = { tone: 'error', text: CONFLICT_QUEUE_FAILURE.more };
      } else {
        queueFailure = describeQueue(reason);
        queueStatus = 'error';
      }
    } finally {
      if (call === queueSeq) queueBusy = false;
    }
  }

  /**
   * Решение эксперта: ответ сервера обновляет строку очереди на месте, а итог
   * показывается в подвале той же карточки. Подтверждение переводит обе находки
   * в оспоренные, поэтому темы выше перечитываются сразу: решение видно, не
   * после отдельной кнопки.
   */
  async function decide(candidate: ConflictCandidate, confirmed: boolean): Promise<void> {
    if (!canDecide) {
      cardMessage = { id: candidate.id, tone: 'warn', text: CONFLICT_QUEUE_FAILURE.denied };
      return;
    }
    pendingId = candidate.id;
    pendingChoice = confirmed;
    cardMessage = null;
    try {
      const review = await api.conflictReview(candidate.id, confirmed);
      candidates = candidates.map((item) =>
        item.id === review.candidate_id
          ? {
              ...item,
              status: review.status,
              decided_by: review.actor_id,
              decided_at: review.created_at,
            }
          : item,
      );
      cardMessage = { id: candidate.id, tone: 'ok', text: queueOutcome(review.status) };
      if (review.status === 'confirmed') void load();
    } catch (reason) {
      const refused = decisionFailure(reason);
      cardMessage = { id: candidate.id, tone: refused.tone, text: refused.text };
      if (refused.refresh) void loadQueue();
    } finally {
      pendingId = '';
      pendingChoice = false;
    }
  }

  // Доступ к находкам считает сервер по подтверждённой сессии, поэтому список
  // перечитывается, когда права появились. Очередь запрашивается тем же входом,
  // но отдельно: её сбой не имеет права прятать темы расхождений, и наоборот.
  let started = false;
  let queueStarted = false;
  $effect(() => {
    if (session.state === 'unknown' || !canRead) return;
    if (!started) {
      started = true;
      void load();
    }
    if (!queueStarted) {
      queueStarted = true;
      void loadQueue();
    }
  });
</script>

<svelte:head>
  <title>Расхождения: Научный Клубок</title>
  <meta
    name="description"
    content="Два источника называют разные числа об одном показателе: здесь видно обе формулировки, места в документах и полосу величин. Ниже очередь на проверку: пары, которые ждут решения эксперта."
  />
</svelte:head>

<div class="page conf">
  <div class="wrap">
    <SectionHead
      level="1"
      eyebrow={CONFLICTS_PAGE.eyebrow}
      title={CONFLICTS_PAGE.title}
      lead={CONFLICTS_PAGE.lead}>
      <div class="row conf__aside">
        {#if status === 'ready' && loadedAt}
          <p class="micro muted">
            {CONFLICTS_PAGE.loadedAt} <time datetime={loadedAt.toISOString()}>{loadedAtText}</time>
          </p>
        {/if}
        {#if status === 'ready' && items.length > 0}
          <!-- Полное число приходит заголовком ответа, поэтому строка о показанном
               стоит рядом со временем загрузки: «показаны 200 из 431 расхождения». -->
          <p class="micro muted">{conflictsShownOf(items.length, conflictsTotal)}</p>
        {/if}
        {#if status === 'ready' && conflictsNote}
          <!-- Замечание о неполном чтении формулирует сервис: экран повторяет его
               отдельной строкой как есть и не переписывает своими словами. -->
          <p class="micro muted conf__window-note">{conflictsNote}</p>
        {/if}
        {#if canRead && status === 'ready' && items.length > 0}
          <Button size="sm" variant="quiet" expanded={filtersOpen} onclick={toggleFilters}>
            {filtersOpen ? CONFLICTS_ACTION.filterClose : CONFLICTS_ACTION.filterOpen}
          </Button>
        {/if}
        {#if canRead}
          <Button
            variant="quiet"
            size="sm"
            icon="refresh"
            busy={status === 'loading'}
            onclick={() => void load()}>
            {CONFLICTS_ACTION.readAgain}
          </Button>
        {/if}
      </div>
    </SectionHead>

    {#if !canRead}
      <Notice tone="warn" title={CONFLICTS_FAILURE.deniedTitle}>
        {CONFLICTS_FAILURE.deniedBody}
        <div class="row conf__aside">
          <Button href="/" variant="quiet" size="sm">На витрину</Button>
        </div>
      </Notice>
    {:else if status === 'loading' && items.length === 0}
      <div class="conf__skeleton" role="status" aria-label={CONFLICTS_PAGE.loadingAria}>
        <span class="skeleton conf__sk-title"></span>
        <span class="skeleton conf__sk-panel"></span>
        <span class="skeleton conf__sk-panel"></span>
        <p class="micro muted">{CONFLICTS_PAGE.loading}</p>
      </div>
    {:else if status === 'error'}
      <Notice tone={failure?.denied ? 'warn' : 'error'} title={failure?.title ?? CONFLICTS_FAILURE.loadTitle}>
        {failure?.text ?? CONFLICTS_FAILURE.loadBody}
        <div class="row conf__aside">
          <Button variant="quiet" size="sm" icon="refresh" onclick={() => void load()}>Повторить запрос</Button>
          <Button href="/findings" variant="ghost" size="sm">Раздел «{navLabel('/findings')}»</Button>
        </div>
      </Notice>
    {:else if items.length === 0}
      {#if conflictsTotal !== null && conflictsTotal > 0}
        <!-- Сервис называет полное число больше нуля, а список пуст: это сбой
             чтения окна, а не «расхождений нет». -->
        <Notice tone="error" title={listUnloadedNotice.title}>
          {listUnloadedNotice.body}
          <div class="row conf__aside">
            <Button variant="quiet" size="sm" icon="refresh" onclick={() => void load()}>
              {CONFLICTS_ACTION.readAgain}
            </Button>
            <Button href="/findings" variant="ghost" size="sm">Раздел «{navLabel('/findings')}»</Button>
          </div>
        </Notice>
      {:else if corpusClaims === 0}
        <!-- Пустой корпус: сравнивать числа здесь ещё не между чем, и выдавать
             это за «источники согласуются» нельзя. -->
        <Empty
          icon="layers"
          title={CONFLICTS_PAGE.emptyCorpusTitle}
          body={CONFLICTS_PAGE.emptyCorpusBody}>
          {#snippet action()}
            <div class="row">
              <Button href="/findings" variant="action" size="sm">
                Открыть «{navLabel('/findings')}»
              </Button>
            </div>
          {/snippet}
        </Empty>
      {:else if conflictsTotal === null}
        <!-- Подтверждённого нуля нет: экран не пишет ни «расхождений нет», ни
             «корпус пуст», потому что полного числа он не знает. -->
        <Notice tone="warn" title={listUnloadedNotice.title}>
          {listUnloadedNotice.body}
          <div class="row conf__aside">
            <Button variant="quiet" size="sm" icon="refresh" onclick={() => void load()}>
              {CONFLICTS_ACTION.readAgain}
            </Button>
            <Button href="/findings" variant="ghost" size="sm">Раздел «{navLabel('/findings')}»</Button>
          </div>
        </Notice>
      {:else if corpusClaims === null}
        <!-- Расхождений нет, а объём корпуса не загружен: экран не вправе
             объявлять ни пустой корпус, ни согласие источников. -->
        <Empty
          icon="alert"
          title={CONFLICTS_PAGE.statsFailedTitle}
          body={CONFLICTS_PAGE.statsFailedBody}>
          {#snippet action()}
            <div class="row">
              <Button variant="action" size="sm" icon="refresh" onclick={() => void load()}>
                {CONFLICTS_ACTION.readAgain}
              </Button>
              <Button href="/dashboard" variant="ghost" size="sm">Раздел «{navLabel('/dashboard')}»</Button>
            </div>
          {/snippet}
        </Empty>
      {:else}
        <Empty
          icon="checkCircle"
          title={queueWaitingCount > 0 ? CONFLICTS_PAGE.noDivergencePendingTitle : CONFLICTS_PAGE.noDivergenceTitle}
          body={queueWaitingCount > 0 ? conflictsNoDivergencePending(queueWaitingCount) : CONFLICTS_PAGE.noDivergenceBody}>
          {#snippet action()}
            {#if corpusClaims !== null}
              <p class="micro muted conf__corpus-size">
                В корпусе {countOf(corpusClaims, 'утверждение', 'утверждения', 'утверждений')}
              </p>
            {/if}
            <div class="row">
              <Button href="/findings" variant="quiet" size="sm">Раздел «{navLabel('/findings')}»</Button>
              <Button href="/research" variant="ghost" size="sm">
                Спросить в разделе «{navLabel('/research')}»
              </Button>
            </div>
          {/snippet}
        </Empty>
      {/if}
    {:else}
      <!-- Применённый отбор виден строкой над темами, только когда он есть:
           без отбора полоса не съедает первый экран. Форма под сворачиванием. -->
      {#if filtersActive}
        <div class="conf__applied">
          <p class="micro conf__applied-title">
            <Icon name="filter" size={14} /> {CONFLICTS_PAGE.filterTitle}
          </p>
          <div class="row conf__applied-chips">
            {#if search.trim()}
              <Chip pressed onclick={() => (search = '')}>
                {CONFLICTS_PAGE.chipSearch}: «{search.trim()}», {CONFLICTS_PAGE.removeFilter}
              </Chip>
            {/if}
            {#if onlyNumeric}
              <Chip pressed onclick={() => (onlyNumeric = false)}>
                {CONFLICTS_PAGE.filterNumeric}, {CONFLICTS_PAGE.removeFilter}
              </Chip>
            {/if}
            {#each pickedClasses as code (code)}
              <Chip pressed onclick={() => toggleClass(code)}>
                {DATA_CLASS_LABELS[code]}, {CONFLICTS_PAGE.removeFilter}
              </Chip>
            {/each}
            {#if sortKey !== 'divergence'}
              <Chip pressed onclick={() => (sortKey = 'divergence')}>
                {CONFLICTS_PAGE.chipSort}: {sortLabel}, {CONFLICTS_PAGE.removeFilter}
              </Chip>
            {/if}
          </div>
          <div class="row conf__applied-actions">
            <Button size="sm" variant="quiet" expanded={filtersOpen} onclick={toggleFilters}>
              {filtersOpen ? CONFLICTS_ACTION.filterClose : CONFLICTS_ACTION.filterOpen}
            </Button>
            <Button size="sm" variant="ghost" onclick={resetFilters}>
              {CONFLICTS_ACTION.filterReset}
            </Button>
          </div>
        </div>
      {:else if !filtersOpen}
        <p class="micro muted conf__nofilter">{CONFLICTS_ACTION.noFilter}</p>
      {/if}

      {#if filtersOpen}
        <Panel raised>
          <form class="conf__form" onsubmit={(event) => event.preventDefault()}>
            <Field
              label={CONFLICTS_PAGE.filterSearch}
              name="conf-search"
              type="search"
              placeholder="формулировка, источник, показатель"
              bind:value={search}
              hint={CONFLICTS_PAGE.filterSearchHint}
            />
            <Select label={CONFLICTS_PAGE.filterSort} name="conf-sort" bind:value={sortKey} options={SORT_OPTIONS} />
            <div class="conf__chips">
              <p class="micro">{CONFLICTS_PAGE.filterAccess}</p>
              <div class="row">
                <Chip pressed={onlyNumeric} onclick={() => (onlyNumeric = !onlyNumeric)}>
                  {CONFLICTS_PAGE.filterNumeric}
                </Chip>
                {#each classOptions as code (code)}
                  <Chip pressed={pickedClasses.includes(code)} onclick={() => toggleClass(code)}>
                    {DATA_CLASS_LABELS[code]}
                  </Chip>
                {/each}
                <Chip
                  pressed={pickedClasses.length === 0}
                  disabled={pickedClasses.length === 0}
                  onclick={showAllClasses}>
                  {CONFLICTS_PAGE.filterAnyAccess}
                </Chip>
              </div>
            </div>
          </form>
        </Panel>
      {/if}

      <!-- Строка-ориентир: из чего состоит экран и сколько в каждой части. -->
      <nav class="conf__marks" aria-label="Части экрана">
        <a class="conf__mark" href="#conf-topics">
          <span class="conf__mark-label">{CONFLICTS_PAGE.markTopics}</span>
          <span class="num conf__mark-value">{marks.topics ?? '—'}</span>
        </a>
        <a class="conf__mark" href="#conf-queue">
          <span class="conf__mark-label">{CONFLICTS_PAGE.markQueue}</span>
          <!-- Знаменатель относится к загруженной части очереди: когда пар
               показали не все, число нижнее, и знак «+» это говорит. -->
          <span class="num conf__mark-value">
            {marks.queue === null ? '—' : `${marks.queue}${queueNotAll ? '+' : ''}`}
          </span>
        </a>
        <a class="conf__mark" href="#conf-gaps">
          <span class="conf__mark-label">{CONFLICTS_PAGE.markGaps}</span>
          <span class="num conf__mark-value">{marks.gaps ?? '—'}</span>
        </a>
        {#if shownGroupRows.length > 0}
          <Button size="sm" variant="link" onclick={toggleAllGroups}>
            {allOpen ? CONFLICTS_ACTION.collapseAll : CONFLICTS_ACTION.expandAll}
          </Button>
        {/if}
      </nav>

      {#if groups.length === 0}
        <Empty
          icon="filter"
          title={CONFLICTS_PAGE.filterEmptyTitle}
          body={CONFLICTS_PAGE.filterEmptyBody}>
          {#snippet action()}
            <div class="row">
              <Button variant="action" size="sm" onclick={resetFilters}>
                {CONFLICTS_ACTION.filterReset}
              </Button>
              <Button variant="quiet" size="sm" onclick={openFilters}>
                {CONFLICTS_ACTION.filterOpen}
              </Button>
            </div>
          {/snippet}
        </Empty>
      {:else}
        <section class="stack conf__topics" id="conf-topics" aria-label={CONFLICTS_PAGE.markTopics}>
          {#each shownGroupRows as group, gi (group.id)}
            {@const open = groupOpen(gi, group)}
            {@const pair = pairOf(group)}
            {@const others = othersFor(group, pair)}
            {@const mainRows = mainOptions(group, pair)}
            {@const hiddenMain = group.findings.length - mainRows.length}
            <!-- Там, где числа несопоставимы, сторону нельзя назвать меньшей или
                 большей: она остаётся первым и вторым утверждением расхождения. -->
            {@const scaled = group.headline !== null}
            <Panel tag="article" tone="default" flush={true}>
              <h3 class="h4 conf-group__heading">
                <button
                  class="conf-group__head"
                  type="button"
                  aria-expanded={open}
                  aria-controls={group.domId}
                  onclick={() => toggleGroup(gi, group)}>
                  <span class="conf-group__title">
                    <span class="conf-group__name">{group.topic.title}</span>
                  </span>
                  <span class="row conf-group__counts">
                    <span class="micro conf-group__stat">
                      <span class="conf-group__stat-label">{CONFLICTS_PAGE.statFindings}</span>
                      <span class="num">{group.findings.length}</span>
                    </span>
                    <span class="micro conf-group__stat">
                      <span class="conf-group__stat-label">{CONFLICTS_PAGE.statSources}</span>
                      <span class="num">{group.sourceCount}</span>
                    </span>
                    <span class="conf-group__div">
                      {#if group.divergence !== null}
                        {CONFLICTS_PAGE.topicDivergence}
                        <span class="num">{pct(group.divergence)}</span>
                      {:else}
                        {CONFLICTS_PAGE.topicNotComparable}
                      {/if}
                    </span>
                  </span>
                  <span class="conf-group__icon">
                    <Icon name={open ? 'chevronDown' : 'chevronRight'} size={18} />
                  </span>
                </button>
              </h3>

              <!-- Тело живёт в DOM и получает `hidden`: aria-controls ведёт к
                   существующему элементу в обоих состояниях раскрытия. -->
              <div class="conf-group__body" id={group.domId} hidden={!open}>
                  {#if group.findings.length > 1}
                    <div class="conf-pick">
                      <p class="micro conf-pick__label">
                        {CONFLICTS_ACTION.mainSource}
                        <span class="conf-pick__hint">{CONFLICTS_ACTION.mainSourceNote}</span>
                      </p>
                      <div class="row">
                        {#each mainRows as finding (finding.id)}
                          <Chip
                            pressed={finding.id === pair.base?.id}
                            onclick={() => pickMain(group, finding.id)}>
                            {baseOptionLabel(finding)}
                          </Chip>
                        {/each}
                        {#if hiddenMain > 0}
                          <Button
                            variant="quiet"
                            size="sm"
                            onclick={() => toggleMain(group.id)}>
                            {CONFLICTS_ACTION.showMoreMain}
                          </Button>
                        {:else if openMain.has(group.id) && group.findings.length > OTHERS_PREVIEW}
                          <Button
                            variant="link"
                            size="sm"
                            onclick={() => toggleMain(group.id)}>
                            {CONFLICTS_ACTION.collapseMain}
                          </Button>
                        {/if}
                      </div>
                      {#if pair.manual}
                        <div class="row conf-pick__side">
                          <Button size="sm" variant="link" onclick={() => resetMain(group)}>
                            {CONFLICTS_ACTION.resetMain}
                          </Button>
                        </div>
                      {/if}
                    </div>
                  {/if}

                  {#if pair.base}
                    <div class="pair">
                      <div class="pair__side pair__side--left">
                        <p class="micro pair__mark">{scaled ? CONFLICT_SIDE.main : CONFLICT_SIDE.first}</p>
                        {@render claimBlock(pair.base)}
                      </div>

                      <div class="pair__seam" aria-hidden="true">
                        <span class="pair__seam-label">{CONFLICTS_PAGE.seamTopics}</span>
                      </div>

                      {#if pair.other}
                        <div class="pair__side pair__side--right">
                          <p class="micro pair__mark">
                            {scaled ? CONFLICT_SIDE.compared : CONFLICT_SIDE.second}
                          </p>
                          {@render claimBlock(pair.other)}
                        </div>
                      {:else}
                        <div class="pair__side">
                          <Notice tone="warn" title={CONFLICTS_PAGE.noSecondTitle}>
                            {CONFLICTS_PAGE.noSecondBody}
                          </Notice>
                        </div>
                      {/if}
                    </div>
                  {:else}
                    <Notice tone="warn" title={CONFLICTS_PAGE.noPairTopicTitle}>
                      {CONFLICTS_PAGE.noPairTopicBody}
                    </Notice>
                  {/if}

                  {#if group.comparisons.length === 0}
                    <p class="micro conf__nocomp">{CONFLICT_SCALE_WORDS.noScale}</p>
                  {:else}
                    <div class="stack scales">
                      <p class="eyebrow scales__title">
                        <Icon name="scale" size={16} />
                        {CONFLICT_SCALE_WORDS.eyebrow}
                        <span class="micro scales__note">{CONFLICT_SCALE_WORDS.note}</span>
                      </p>
                      {#each group.comparisons as cmp (cmp.key)}
                        {@const prop = nameOf(PROPERTY_LABELS, cmp.property)}
                        {@const band = bandPoints(cmp)}
                        {@const from = fromMainLabel(cmp, pair)}
                        <div class="scale">
                          <p class="scale__head">
                            <span class="scale__prop" title={propHint(cmp.property, prop.named)}>
                              {prop.name}
                            </span>
                            <span class="micro">{cmp.unit || CONFLICT_TOPIC_WORDS.noUnit}</span>
                            <span class="micro grow">
                              {countOf(cmp.points.length, 'значение', 'значения', 'значений')}
                              {CONFLICT_SCALE_WORDS.values}
                            </span>
                          </p>
                          {#each band.rows as point, pi (`${point.findingId}-${pi}`)}
                            {@const track = bandStyle(point, cmp)}
                            {@const isMain = pair.base !== null && point.findingId === pair.base.id}
                            <div class="scale__line" data-base={isMain ? '1' : undefined}>
                              <p class="scale__src">
                                <span class="num">{pi + 1}</span>
                                <span>{point.source || NO_SOURCE}</span>
                                {#if isMain}
                                  <span class="scale__base">{CONFLICT_SCALE_WORDS.mainTag}</span>
                                {/if}
                                {#each point.locs as loc (loc.kind)}
                                  <span class="locator">{loc.kind} <span class="num">{loc.value}</span></span>
                                {/each}
                                {#if point.locs.length === 0}
                                  <span class="locator">{CONFLICT_TOPIC_WORDS.noLocators}</span>
                                {/if}
                              </p>
                              <p class="scale__value num">
                                {point.text}{#if cmp.unit} {cmp.unit}{/if}
                              </p>
                              {#if track}
                                <span class="bar scale__track">
                                  <span class="bar__fill" style={track}></span>
                                </span>
                              {:else}
                                <p class="micro scale__noband">{CONFLICT_SCALE_WORDS.noband}</p>
                              {/if}
                            </div>
                          {/each}
                          {#if band.hidden > 0}
                            <Button variant="quiet" size="sm" onclick={() => toggleBand(cmp.key)}>
                              {CONFLICT_SCALE_WORDS.showMoreBand}
                            </Button>
                          {:else if openBand.has(cmp.key) && cmp.points.length > BAND_PREVIEW}
                            <Button variant="link" size="sm" onclick={() => toggleBand(cmp.key)}>
                              {CONFLICT_SCALE_WORDS.collapseBand}
                            </Button>
                          {/if}
                          <!-- Две короткие строки вместо цепочки через «·»: сначала
                               факты полосы, затем сравнение от выбранного источника,
                               затем пересечение диапазонов. -->
                          {#if cmp.spread === 0}
                            <p class="scale__cap micro">{CONFLICT_SCALE_WORDS.equal}</p>
                          {:else}
                            <p class="row scale__cap">
                              <span>
                                {CONFLICT_SCALE_WORDS.range}:
                                <span class="num">{num(cmp.lo)}</span>–<span class="num">{num(cmp.hi)}</span>{#if cmp.unit} {cmp.unit}{/if}
                              </span>
                              <span>
                                {CONFLICT_SCALE_WORDS.diff}
                                <span class="num">{num(cmp.spread)}</span>{#if cmp.unit} {cmp.unit}{/if}
                              </span>
                            </p>
                            <p class="scale__from micro">
                              {#if from.named}{CONFLICT_SIDE.compared} {/if}{from.lead}
                              {#if from.value}<span class="num">{from.value}</span>{/if}
                            </p>
                            <p class="scale__cross micro">
                              {#if cmp.disjoint}
                                {CONFLICT_SCALE_WORDS.disjoint}
                              {:else if cmp.overlap}
                                {CONFLICT_SCALE_WORDS.overlap}:
                                <span class="num">{num(cmp.overlap[0])}</span>–<span class="num">{num(cmp.overlap[1])}</span>{#if cmp.unit} {cmp.unit}{/if}
                              {:else}
                                {CONFLICT_SCALE_WORDS.noOverlap}
                              {/if}
                            </p>
                          {/if}
                        </div>
                      {/each}
                    </div>
                  {/if}
                  {#if others.length > 0}
                    {@const othersOpen = openOthers.has(group.id)}
                    {@const visibleOthers = othersOpen ? others.length : Math.min(others.length, OTHERS_PREVIEW)}
                    <div class="stack others">
                      <p class="micro others__title">
                        {othersShown(visibleOthers, others.length)} по этой теме
                      </p>
                      {#each othersOpen ? others : others.slice(0, OTHERS_PREVIEW) as other (other.id)}
                        <div class="other">
                          <p class="small">{other.statement}</p>
                          <p class="row other__meta">
                            <span class="locator">{sourceOf(other) || NO_SOURCE}</span>
                            {#if other.evidence[0]}
                              {#each locatorsOf(other.evidence[0]) as loc (loc.kind)}
                                <span class="locator">{loc.kind} <span class="num">{loc.value}</span></span>
                              {/each}
                            {:else}
                              <span class="locator">{CONFLICT_TOPIC_WORDS.noLocators}</span>
                            {/if}
                            <span class="micro">
                              {CONFLICT_TOPIC_WORDS.versionTitle}
                              <span class="num">{other.version}</span>
                            </span>
                            {#if other.scope}
                              {#each describeScope(other.scope) as line, li (li)}
                                <span class="scope__item">{line}</span>
                              {/each}
                            {/if}
                            <StatusPill status={other.status} label={STATUS_SHORT[other.status]} />
                          </p>
                        </div>
                      {/each}
                      {#if visibleOthers < others.length || othersOpen}
                        <Button variant="quiet" size="sm" onclick={() => toggleOthers(group.id)}>
                          {othersOpen
                            ? CONFLICT_TOPIC_WORDS.collapseOthers
                            : CONFLICT_TOPIC_WORDS.showMore}
                        </Button>
                      {/if}
                    </div>
                  {/if}

                  <!-- Идентификаторы, сырой текст из источника и символовые оффсеты
                       не решают, кому верить: они под раскрытием. -->
                  {#if group.topic.keys.length > 0 || pair.base || pair.other}
                    <details class="svc">
                      <summary class="micro">{CONFLICT_TOPIC_SVC.head}</summary>
                      {#each group.topic.keys as key (key)}
                        <p class="micro svc__row">
                          {CONFLICT_TOPIC_SVC.topicKeys} <code class="code">{key}</code>
                        </p>
                      {/each}
                      {#if pair.base}
                        {@render svcFinding(pair.base, CONFLICT_TOPIC_SVC.mainCode)}
                      {/if}
                      {#if pair.other}
                        {@render svcFinding(pair.other, CONFLICT_TOPIC_SVC.comparedCode)}
                      {/if}
                    </details>
                  {/if}

                  <div class="row conf-group__foot">
                    <Button href={decisionHref(pair)} variant="action" size="sm" icon="shield">
                      {CONFLICTS_ACTION.record}
                    </Button>
                    <p class="micro muted conf-group__note">
                      {conflictsRecordNote(navLabel('/feedback'))}
                    </p>
                  </div>

                  <div class="row conf-group__links">
                    <Button href="/findings" variant="link" size="sm" iconEnd="arrowRight">
                      {conflictsSectionLink(navLabel('/findings'))}
                    </Button>
                    <Button href="/graph" variant="link" size="sm" iconEnd="arrowRight">
                      {conflictsSectionLink(navLabel('/graph'))}
                    </Button>
                  </div>
              </div>
            </Panel>
          {/each}
        </section>

        <div class="conf__pager">
          <p class="micro">{topicsShown(shownGroupRows.length, groups.length)}</p>
          {#if groups.length > shownGroupRows.length}
            <Button variant="quiet" size="sm" onclick={() => (shownGroups += GROUP_PAGE_SIZE)}>
              {CONFLICT_TOPIC_WORDS.showMore}
            </Button>
          {/if}
        </div>
      {/if}

      {#if groups.length > 0 || gaps.length > 0}
        <section class="conf__gaps" id="conf-gaps" aria-labelledby="conf-gaps-title">
          <SectionHead
            level="2"
            id="conf-gaps-title"
            eyebrow={CONFLICTS_PAGE.gapsEyebrow}
            title={CONFLICTS_PAGE.gapsTitle}
            lead={CONFLICTS_PAGE.gapsLead}
          />

          {#if gaps.length === 0}
            <Empty
              icon="checkCircle"
              title={CONFLICTS_PAGE.gapsEmptyTitle}
              body={CONFLICTS_PAGE.gapsEmptyBody}
            />
          {:else}
            <div class="stack gaps">
              {#each shownGapsRows as note (note.id)}
                <article class="gap">
                  <p class="gap__kind micro">
                    <Icon name={note.kind === 'locator' ? 'pin' : note.kind === 'value' ? 'minus' : 'conflict'} size={15} />
                    {GAP_KIND_LABELS[note.kind]}
                  </p>
                  <h3 class="h4">{GAP_KIND_TITLES[note.kind]}</h3>
                  <p class="small muted">
                    {gapBody(note.kind, note.topic.title, countOf(note.findings, 'утверждение', 'утверждения', 'утверждений'))}
                  </p>
                  <!-- Тема «pair» и «scale» уже названа в формулировке пробела:
                       второй раз её печатать не нужно. -->
                  {#if note.kind === 'value' || note.kind === 'locator'}
                    <p class="gap__meta">
                      <strong class="small">{note.topic.title}</strong>
                    </p>
                  {/if}
                  <p class="row gap__locs">
                    <span class="locator">{note.source || NO_SOURCE}</span>
                    {#each note.locs as loc (loc.kind)}
                      <span class="locator">{loc.kind} <span class="num">{loc.value}</span></span>
                    {/each}
                    {#if note.locs.length === 0}
                      <span class="locator">{CONFLICT_TOPIC_WORDS.noGapLocators}</span>
                    {/if}
                  </p>
                  {#if note.topic.keys.length > 0}
                    <details class="svc">
                      <summary class="micro">{CONFLICT_TOPIC_SVC.head}</summary>
                      {#each note.topic.keys as key (key)}
                        <p class="micro svc__row">
                          {CONFLICT_TOPIC_SVC.topicKeys} <code class="code">{key}</code>
                        </p>
                      {/each}
                    </details>
                  {/if}
                </article>
              {/each}
            </div>

            <div class="conf__pager">
              <p class="micro">
                {gapsShown(shownGapsRows.length, gaps.length)}
                <span class="conf__pager-note">{CONFLICT_TOPIC_WORDS.gapsNotFiltered}</span>
              </p>
              {#if gaps.length > shownGapsRows.length}
                <Button variant="quiet" size="sm" onclick={() => (shownGaps += GAP_PAGE_SIZE)}>
                  {CONFLICT_TOPIC_WORDS.showMore}
                </Button>
              {/if}
            </div>
          {/if}
        </section>
      {/if}
    {/if}

    {#if canRead}
      <section class="conf__queue" id="conf-queue" aria-labelledby="conf-queue-title">
        <SectionHead
          level="2"
          id="conf-queue-title"
          eyebrow={CONFLICT_QUEUE_HEAD.eyebrow}
          title={CONFLICT_QUEUE_HEAD.title}
          lead={CONFLICT_QUEUE_HEAD.lead}>
          <div class="row conf__aside">
            {#if queueStatus === 'ready' && queueLoadedAt}
              <p class="micro muted">
                {CONFLICT_QUEUE_WORDS.loadedAt}
                <time datetime={queueLoadedAt.toISOString()}>{queueLoadedAtText}</time>
              </p>
            {/if}
            <Button
              variant="quiet"
              size="sm"
              icon="refresh"
              busy={queueStatus === 'loading' && candidates.length === 0}
              disabled={queueBusy}
              onclick={() => void loadQueue()}>
              {CONFLICT_QUEUE_ACTION.readAgain}
            </Button>
          </div>
        </SectionHead>

        {#if queueStatus === 'ready' && candidates.length > 0}
          <!-- Каждый счётчик подписан своим элементом: видно, сколько пар показано
               из очереди и сколько из них ещё не решены. -->
          <div class="row conf-queue__counter">
            <span class="micro">{queueCounter(shownQueueRows.length, queueTotal)}</span>
            <span class="micro">{queueWaiting(queueWaitingCount)}</span>
            {#if queueNotAll}
              <span class="micro">{CONFLICT_QUEUE_WORDS.notAll}</span>
            {/if}
          </div>
        {/if}

        {#if !canDecide && candidates.length > 0}
          <Notice tone="info" title={CONFLICT_QUEUE_WORDS.gateTitle}>
            {CONFLICT_QUEUE_WORDS.gate}
          </Notice>
        {/if}

        {#if queueMessage}
          <Notice tone={queueMessage.tone}>{queueMessage.text}</Notice>
        {/if}

        {#if queueStatus === 'loading' && candidates.length === 0}
          <div class="conf-queue__skeleton" role="status" aria-label={CONFLICT_QUEUE_WORDS.skeletonAria}>
            <span class="skeleton conf-queue__sk-panel"></span>
            <span class="skeleton conf-queue__sk-panel"></span>
            <p class="micro muted">{CONFLICT_QUEUE_WORDS.skeleton}</p>
          </div>
        {:else if queueStatus === 'error' && candidates.length === 0}
          <!-- Сбой очереди не пустота: «пар нет» и «пары не пришли» это разные
               экраны и разные действия. -->
          <Notice
            tone={queueFailure?.denied ? 'warn' : 'error'}
            title={queueFailure?.title ?? CONFLICT_QUEUE_WORDS.queueFailedTitle}>
            {queueFailure?.text ?? CONFLICT_QUEUE_FAILURE.queueFailed}
            <div class="row conf__aside">
              <Button variant="quiet" size="sm" icon="refresh" onclick={() => void loadQueue()}>
                {CONFLICT_QUEUE_ACTION.readAgain}
              </Button>
              <Button href="/dashboard" variant="ghost" size="sm">
                Раздел «{navLabel('/dashboard')}»
              </Button>
            </div>
          </Notice>
        {:else if candidates.length === 0 && queueTotal === 0}
          <Empty
            icon="checkCircle"
            title={CONFLICT_QUEUE_WORDS.emptyTitle}
            body={CONFLICT_QUEUE_WORDS.emptyBody}
          />
        {:else if candidates.length === 0}
          <!-- Пустой ответ без подтверждённого нуля: это неполная загрузка, а не
               вывод о том, что противоречий в корпусе нет. -->
          <Notice tone="warn" title={CONFLICT_QUEUE_WORDS.partialTitle}>
            {CONFLICT_QUEUE_WORDS.notAll}
            <div class="row conf__aside">
              <Button variant="quiet" size="sm" icon="refresh" onclick={() => void loadQueue()}>
                {CONFLICT_QUEUE_ACTION.readAgain}
              </Button>
            </div>
          </Notice>
        {:else}
          <div class="stack conf-queue__list">
            {#each shownQueueRows as candidate (candidate.id)}
              {@const topic = pairTopic(candidate)}
              {@const busy = pendingId === candidate.id}
              {@const decided = candidate.status !== 'candidate'}
              <Panel tag="article" tone="default" flush={true}>
                <div class="conf-pair">
                  <div class="conf-pair__head">
                    <h3 class="h4 conf-pair__title">{topic.title}</h3>
                    <span class="row conf-pair__marks">
                      <StatusPill
                        status={CONFLICT_QUEUE_STATUS_TONE[candidate.status]}
                        label={CONFLICT_QUEUE_STATUS_LABELS[candidate.status]}
                      />
                      <span class="micro muted">{nameOf(PROPERTY_LABELS, candidate.property_name).name}</span>
                    </span>
                  </div>

                  <p class="micro conf-pair__why">{CONFLICT_QUEUE_WORDS.whyTitle}</p>
                  <p class="small conf-pair__reason">
                    {candidate.reason || CONFLICT_QUEUE_WORDS.noReason}
                  </p>

                  {#if Object.keys(candidate.scope).length > 0}
                    <p class="scope">
                      <span class="micro scope__title">{CONFLICT_QUEUE_WORDS.scopeTitle}</span>
                      {#each describeScope(candidate.scope) as line (line)}
                        <span class="scope__item">{line}</span>
                      {/each}
                    </p>
                  {:else}
                    <p class="micro scope scope__title">{CONFLICT_QUEUE_WORDS.noScope}</p>
                  {/if}

                  <div class="pair">
                    <div class="pair__side pair__side--left" data-class={candidate.left.data_class}>
                      <p class="micro pair__mark">{CONFLICT_SIDE.first}</p>
                      {@render sideBlock(candidate.left)}
                    </div>

                    <div class="pair__seam" aria-hidden="true">
                      <span class="pair__seam-label">{CONFLICT_QUEUE_WORDS.seam}</span>
                    </div>

                    <div class="pair__side pair__side--right" data-class={candidate.right.data_class}>
                      <p class="micro pair__mark">{CONFLICT_SIDE.second}</p>
                      {@render sideBlock(candidate.right)}
                    </div>
                  </div>

                  <p class="micro conf-pair__decided">
                    {#if candidate.decided_by === session.account?.id}
                      <span>{CONFLICT_QUEUE_WORDS.decidedSelf}</span>
                    {:else if candidate.decided_by}
                      <span>{CONFLICT_QUEUE_WORDS.decidedBy}</span>
                    {:else}
                      <span>{CONFLICT_QUEUE_WORDS.waiting}</span>
                    {/if}
                    {#if candidate.decided_at}
                      <span>
                        {CONFLICT_QUEUE_WORDS.decidedTitle}
                        <time datetime={candidate.decided_at}>{dateTime(candidate.decided_at)}</time>
                      </span>
                    {/if}
                  </p>

                  <details class="svc">
                    <summary class="micro">{CONFLICT_QUEUE_SVC.head}</summary>
                    <p class="micro svc__row">
                      {CONFLICT_QUEUE_SVC.pair} <code class="code">{candidate.id}</code>
                    </p>
                    <p class="micro svc__row">
                      {CONFLICT_QUEUE_SVC.subject} <code class="code">{candidate.subject}</code>
                    </p>
                    <p class="micro svc__row">
                      {CONFLICT_QUEUE_SVC.property}
                      <code class="code">{candidate.property_name}</code>
                    </p>
                    <p class="micro svc__row">
                      {CONFLICT_QUEUE_SVC.leftClaim} <code class="code">{candidate.left.claim_id}</code>
                    </p>
                    <p class="micro svc__row">
                      {CONFLICT_QUEUE_SVC.leftFinding}
                      <code class="code">{candidate.left.finding_id}</code>
                    </p>
                    <p class="micro svc__row">
                      {CONFLICT_QUEUE_SVC.rightClaim} <code class="code">{candidate.right.claim_id}</code>
                    </p>
                    <p class="micro svc__row">
                      {CONFLICT_QUEUE_SVC.rightFinding}
                      <code class="code">{candidate.right.finding_id}</code>
                    </p>
                    {#if candidate.decided_by}
                      <p class="micro svc__row">
                        {CONFLICT_QUEUE_SVC.decidedBy}
                        <code class="code">{candidate.decided_by}</code>
                      </p>
                    {/if}
                  </details>

                  {#if cardMessage && cardMessage.id === candidate.id}
                    <Notice tone={cardMessage.tone}>{cardMessage.text}</Notice>
                  {/if}

                  {#if canDecide}
                    <div class="row conf-pair__actions">
                      <Button
                        variant="action"
                        size="sm"
                        icon="shield"
                        busy={busy && pendingChoice}
                        disabled={busy && !pendingChoice}
                        title={decided ? CONFLICT_QUEUE_HINTS.revision : undefined}
                        onclick={() => void decide(candidate, true)}>
                        {busy && pendingChoice
                          ? CONFLICT_QUEUE_ACTION.pending
                          : CONFLICT_QUEUE_ACTION.confirm}
                      </Button>
                      <Button
                        variant="quiet"
                        size="sm"
                        icon="close"
                        busy={busy && !pendingChoice}
                        disabled={busy && pendingChoice}
                        title={decided ? CONFLICT_QUEUE_HINTS.revision : undefined}
                        onclick={() => void decide(candidate, false)}>
                        {busy && !pendingChoice
                          ? CONFLICT_QUEUE_ACTION.pending
                          : CONFLICT_QUEUE_ACTION.dismiss}
                      </Button>
                      <p class="micro muted conf-pair__hint">{CONFLICT_QUEUE_HINTS.confirm}</p>
                    </div>
                  {/if}
                </div>
              </Panel>
            {/each}
          </div>

          <div class="conf__pager">
            <p class="micro">{queueCounter(shownQueueRows.length, queueTotal)}</p>
            {#if shownQueueRows.length < candidates.length}
              <Button variant="quiet" size="sm" onclick={() => (shownPairs += QUEUE_PREVIEW)}>
                {CONFLICT_TOPIC_WORDS.showMore}
              </Button>
            {:else if queueHasMore}
              <!-- Одна кнопка «Показать ещё»: сначала раскрывает загруженное,
                   потом молча дозагружает следующее. -->
              <Button
                variant="quiet"
                size="sm"
                busy={queueBusy}
                disabled={queueBusy}
                onclick={() => void loadQueue(true)}>
                {queueBusy ? CONFLICT_QUEUE_WORDS.loadingMore : CONFLICT_TOPIC_WORDS.showMore}
              </Button>
            {/if}
          </div>
        {/if}
      </section>
    {/if}
  </div>
</div>

{#snippet sideBlock(side: ConflictSide)}
  <div class="conf-side">
    <p class="conf-side__head">
      <span class="tag" title={CONFLICT_QUEUE_CLASS_HINT[side.data_class]}>
        <Icon
          name={side.data_class === 'restricted' ? 'lock' : side.data_class === 'internal' ? 'eye' : 'doc'}
          size={14}
        />
        {DATA_CLASS_LABELS[side.data_class]}
      </span>
      {#if side.data_class !== 'public'}
        <span class="micro conf-side__class">{CONFLICT_QUEUE_CLASS_HINT[side.data_class]}</span>
      {/if}
    </p>

    <h3 class="claim__statement">{side.statement || CONFLICT_QUEUE_WORDS.noStatement}</h3>

    <p class="conf-side__value">
      <span class="micro conf-side__label">{CONFLICT_QUEUE_WORDS.valueLabel}</span>
      <span class="conf-side__num num">{side.value || CONFLICT_QUEUE_WORDS.noValue}</span>
    </p>

    {#if side.document_ids.length > 0}
      <p class="row conf-side__docs">
        {#each side.document_ids as documentId, di (`${side.finding_id}-${documentId}-${di}`)}
          <Button
            href={documentHref(documentId)}
            variant="link"
            size="sm"
            iconEnd="arrowRight"
            title={docHint(documentId)}>
            {CONFLICT_QUEUE_ACTION.openSource}
          </Button>
        {/each}
      </p>
    {:else}
      <p class="micro conf-side__nodoc">{CONFLICT_QUEUE_WORDS.noDocs}</p>
    {/if}
  </div>
{/snippet}

{#snippet claimBlock(finding: FindingListItem)}
  <div class="claim">
    <p class="claim__head">
      <StatusPill status={finding.status} label={STATUS_SHORT[finding.status]} />
      <span class="micro">
        {CONFLICT_TOPIC_WORDS.versionTitle} <span class="num">{finding.version}</span>
      </span>
      <span class="micro">{DATA_CLASS_LABELS[finding.data_class]}</span>
    </p>
    <h3 class="claim__statement">{finding.statement}</h3>

    {#if finding.scope && Object.keys(finding.scope).length > 0}
      <p class="scope">
        <span class="micro scope__title">{CONFLICT_TOPIC_WORDS.scopeTitle}</span>
        {#each describeScope(finding.scope) as line (line)}
          <span class="scope__item">{line}</span>
        {/each}
      </p>
    {:else}
      <p class="micro scope scope__title">{CONFLICT_TOPIC_WORDS.noScope}</p>
    {/if}

    {#if finding.observations.length > 0}
      <ul class="obs">
        {#each finding.observations as obs, oi (`${finding.id}-obs-${oi}`)}
          {@const prop = nameOf(PROPERTY_LABELS, obs.property_name)}
          <li class="obs__row">
            <span class="obs__name" title={propHint(obs.property_name, prop.named)}>
              {prop.name}
            </span>
            <span class="obs__value num">{obsText(obs)}</span>
            <span class="micro">{unitOf(obs) || CONFLICT_TOPIC_WORDS.noUnit}</span>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="micro obs obs__none">{CONFLICT_TOPIC_WORDS.noNumbers}</p>
    {/if}

    {#if finding.evidence.length > 0}
      {@const quotesOpen = openQuotes.has(finding.id)}
      {@const visibleQuotes = quotesOpen
        ? finding.evidence.length
        : Math.min(finding.evidence.length, EVIDENCE_PREVIEW)}
      {#each quotesOpen ? finding.evidence : finding.evidence.slice(0, EVIDENCE_PREVIEW) as ev, ei (`${finding.id}-ev-${ei}`)}
        <figure class="quote ev">
          <blockquote class="small">{ev.quote}</blockquote>
          <figcaption class="row ev__loc">
            <strong class="micro">{ev.source_title || NO_SOURCE}</strong>
            {#each locatorsOf(ev) as loc (loc.kind)}
              <span class="locator">{loc.kind} <span class="num">{loc.value}</span></span>
            {/each}
            {#if locatorsOf(ev).length === 0}
              <span class="locator">{CONFLICT_TOPIC_WORDS.noLocatorPlace}</span>
            {/if}
          </figcaption>
        </figure>
      {/each}
      {#if visibleQuotes < finding.evidence.length || quotesOpen}
        <span class="row claim__quotes">
          <span class="micro muted">{quotesShown(visibleQuotes, finding.evidence.length)}</span>
          <Button variant="quiet" size="sm" onclick={() => toggleQuotes(finding.id)}>
            {quotesOpen
              ? CONFLICT_TOPIC_WORDS.collapseEvidence
              : CONFLICT_TOPIC_WORDS.showMore}
          </Button>
        </span>
      {/if}
    {:else}
      <p class="micro ev__none">{CONFLICT_TOPIC_WORDS.noEvidence}</p>
    {/if}
  </div>
{/snippet}

{#snippet svcFinding(finding: FindingListItem, label: string)}
  <p class="micro svc__row">{label} <code class="code">{finding.id}</code></p>
  <p class="micro svc__row">
    {CONFLICT_TOPIC_SVC.confidence} <span class="num">{pct(finding.confidence)}</span>
  </p>
  {#each finding.observations as obs, oi (`${finding.id}-svc-obs-${oi}`)}
    <p class="micro svc__row">
      {CONFLICT_TOPIC_SVC.rawText} <code class="code">{obs.raw_text}</code>
    </p>
  {/each}
  {#each finding.evidence as ev, ei (`${finding.id}-svc-ev-${ei}`)}
    {@const range = charRangeOf(ev)}
    {#if range}
      <p class="micro svc__row">
        {CONFLICT_TOPIC_SVC.charRange} <code class="code">{range}</code>
      </p>
    {/if}
  {/each}
{/snippet}

<style>
  .conf .wrap {
    display: flex;
    flex-direction: column;
    gap: var(--s5);
  }

  .conf__aside {
    justify-content: flex-end;
  }

  /* Замечание сервиса о неполном чтении занимает в шапке свою строку:
     предложение длиннее кнопок и не имеет права их сжимать. */
  .conf__window-note {
    flex: 1 0 100%;
    text-align: right;
  }

  .conf__skeleton {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    padding: var(--s6);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-xl);
    background: var(--surface-raised);
  }

  /* Геометрия скелетона — в классе, а не в inline-стилях разметки. */
  .conf__sk-title {
    display: block;
    height: var(--s4);
    width: 38%;
  }

  .conf__sk-panel {
    display: block;
    height: var(--s9);
    border-radius: var(--r-lg);
  }

  /* ── Применённый отбор и его форма ─────────────────────────────────────── */
  .conf__applied {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s4);
    flex-wrap: wrap;
    padding: var(--s4);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-lg);
    background: var(--surface-sunk);
  }

  .conf__applied-title {
    display: flex;
    align-items: center;
    gap: var(--s2);
    color: var(--ink-3);
    font-weight: 600;
  }

  .conf__applied-chips {
    flex: 1 1 auto;
    min-width: 0;
  }

  .conf__applied-actions {
    justify-content: flex-end;
  }

  .conf__form {
    display: grid;
    gap: var(--s4);
    grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr);
    align-items: end;
  }

  .conf__chips {
    grid-column: 1 / -1;
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  .conf__nofilter {
    color: var(--ink-3);
  }

  /* Строка-ориентир: три части экрана с живыми числами каждой из них. */
  .conf__marks {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .conf__mark {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    padding: var(--s2) var(--s4);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-pill);
    background: var(--surface-raised);
    color: var(--ink);
    text-decoration: none;
    transition: background var(--dur-fast) var(--ease-soft);
  }

  .conf__mark:hover {
    background: var(--sage);
  }

  .conf__mark-label {
    font-size: var(--t-micro);
    color: var(--ink-3);
  }

  .conf__mark-value {
    font-size: var(--t-small);
    font-weight: 600;
  }

  .conf__topics {
    --gap: var(--s5);
    scroll-margin-top: var(--s6);
  }

  .conf__gaps,
  .conf__queue {
    scroll-margin-top: var(--s6);
  }

  .conf__pager {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s4);
    flex-wrap: wrap;
    padding: var(--s3) var(--s4);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-lg);
    background: var(--surface-raised);
  }

  /* Пояснение к счётчику стоит отдельным подписанным элементом, а не хвостом
     одной строки. */
  .conf__pager-note {
    margin-left: var(--s3);
    color: var(--ink-3);
    font-size: var(--t-micro);
  }

  /* ── Тема расхождения ──────────────────────────────────────────────────── */
  /* Название темы держит заголовок уровня h3: у экрана появляется читаемая
     структура, а не одна простыня strong-ов внутри кнопки. */
  .conf-group__heading {
    margin: 0;
    min-width: 0;
  }

  .conf-group__head {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    align-items: center;
    gap: var(--s4);
    width: 100%;
    padding: var(--s5) clamp(var(--s4), 2.4vw, var(--s6));
    border: 0;
    border-bottom: 1px solid var(--line-soft);
    background: var(--paper-deep);
    text-align: left;
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-soft);
  }

  .conf-group__head:hover {
    background: var(--sage);
  }

  .conf-group__title {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    flex-wrap: wrap;
    min-width: 0;
  }

  .conf-group__name {
    font-size: var(--t-h4);
    font-weight: 600;
    line-height: var(--lh-head);
    letter-spacing: var(--tr-body);
    text-wrap: pretty;
  }

  .conf-group__counts {
    justify-content: flex-end;
    gap: var(--s4);
  }

  .conf-group__stat {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    color: var(--ink-3);
  }

  .conf-group__stat-label {
    font-size: var(--t-micro);
  }

  /* Ключевое число темы видно без приближения: оно крупнее служебных подписей. */
  .conf-group__div {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    color: var(--ink-2);
    font-size: var(--t-micro);
  }

  .conf-group__div .num {
    font-size: var(--t-body);
    font-weight: 600;
    color: var(--ink);
  }

  .conf-group__icon {
    color: var(--ink-3);
    transition: color var(--dur-fast) var(--ease-soft);
  }

  .conf-group__head[aria-expanded='true'] .conf-group__icon {
    color: var(--action-ink);
  }

  .conf-group__body {
    display: flex;
    flex-direction: column;
    gap: var(--s6);
    padding: clamp(var(--s4), 2.4vw, var(--s6));
  }

  /* Тело свёрнутой темы скрыто атрибутом `hidden`: без этого правила
     display:flex авторского стиля перебивает скрытие из браузерных стилей. */
  .conf-group__body[hidden] {
    display: none;
  }

  /* Выбор базы: аналитик называет источник, с которым сверяет остальное. */
  .conf-pick {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    padding: var(--s4);
    border: 1px dashed var(--line-strong);
    border-radius: var(--r-md);
    background: var(--surface-sunk);
  }

  .conf-pick__label {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
    color: var(--ink-2);
    font-weight: 600;
  }

  .conf-pick__hint {
    color: var(--ink-3);
    font-weight: 400;
  }

  .conf-pick__side {
    justify-content: flex-start;
  }

  .pair {
    position: relative;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    border-radius: var(--r-lg);
    overflow: hidden;
  }

  .pair__side {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    padding: var(--s5);
    min-width: 0;
  }

  .pair__side--left {
    background: var(--sage);
  }

  .pair__side--right {
    background: var(--peach-wash);
  }

  .pair__mark {
    color: var(--ink-3);
  }

  /* Разделитель сторон: на широком экране вертикальная метка по центру. */
  .pair__seam {
    position: absolute;
    top: var(--s4);
    bottom: var(--s4);
    left: 50%;
    display: grid;
    place-items: center;
    transform: translateX(-50%);
    border-left: 2px dashed var(--line-strong);
  }

  .pair__seam-label {
    align-self: center;
    padding: var(--s1) var(--s4);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-pill);
    background: var(--surface);
    color: var(--ink-2);
    font-size: var(--t-micro);
    font-weight: 500;
    box-shadow: var(--shadow-soft);
    writing-mode: vertical-rl;
    letter-spacing: 0.08em;
  }

  .claim {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    min-width: 0;
  }

  .claim__head {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .claim__statement {
    font-size: var(--t-body);
    font-weight: 600;
    line-height: var(--lh-dense);
    letter-spacing: var(--tr-body);
    text-wrap: pretty;
  }

  .claim__quotes {
    justify-content: flex-start;
    align-items: center;
  }

  .scope {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
  }

  .scope__title {
    color: var(--ink-3);
  }

  .scope__item {
    padding: var(--s1) var(--s3);
    border: 1px solid var(--line);
    border-radius: var(--r-pill);
    background: var(--surface);
    font-size: var(--t-micro);
    color: var(--ink-2);
  }

  .obs {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: var(--s1);
    margin: 0;
    padding: 0;
  }

  .obs__row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    align-items: baseline;
    gap: var(--s1) var(--s3);
    padding: var(--s2) var(--s3);
    border-radius: var(--r-xs);
    background: var(--surface);
  }

  .obs__name {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
    min-width: 0;
    color: var(--ink-2);
  }

  .obs__value {
    font-size: var(--t-small);
    font-weight: 600;
    color: var(--ink);
    text-align: right;
  }

  .obs__none {
    color: var(--ink-3);
  }

  .ev {
    margin: 0;
  }

  .ev blockquote {
    margin: 0;
  }

  .ev__loc {
    gap: var(--s3);
    margin-top: var(--s2);
  }

  .ev__loc strong {
    font-weight: 600;
    color: var(--ink-2);
  }

  .ev__none {
    color: var(--disputed);
  }

  .conf__nocomp {
    max-width: var(--maxw-measure);
    color: var(--ink-3);
  }

  /* ── Полоса величин ────────────────────────────────────────────────────── */
  .scales {
    --gap: var(--s4);
    padding: var(--s5);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-lg);
    background: var(--surface-sunk);
  }

  .scales__title {
    align-items: center;
    gap: var(--s3);
  }

  .scales__note {
    color: var(--ink-3);
  }

  .scale {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    padding-top: var(--s3);
    border-top: 1px solid var(--line);
  }

  .scale__head {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
  }

  .scale__prop {
    font-size: var(--t-small);
    font-weight: 600;
    color: var(--ink);
  }

  /* Числовой столбец не фиксируем: величина обязана помещаться целиком. */
  .scale__line {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: var(--s2) var(--s4);
    align-items: baseline;
  }

  /* Строка выбранной базы читается первой: она и есть точка отсчёта спора. */
  .scale__line[data-base='1'] .scale__src {
    color: var(--ink);
    font-weight: 600;
  }

  .scale__src {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
    font-size: var(--t-small);
    color: var(--ink-2);
    min-width: 0;
  }

  .scale__base {
    padding: var(--s1) var(--s3);
    border: 1px solid var(--line-strong);
    border-radius: var(--r-pill);
    background: var(--surface);
    color: var(--ink-3);
    font-size: var(--t-micro);
  }

  .scale__value {
    font-size: var(--t-small);
    font-weight: 600;
    text-align: right;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .scale__track {
    grid-column: 1 / -1;
    height: var(--s2);
    background: var(--surface);
    box-shadow: inset 0 0 0 1px var(--line);
  }

  .scale__noband,
  .scale__cap,
  .scale__from,
  .scale__cross {
    grid-column: 1 / -1;
    color: var(--ink-3);
  }

  /* Сравнение от главного источника и пересечение диапазонов читаются
     отдельными строками: цепочка подписей через разделитель не работает. */
  .scale__from,
  .scale__cross {
    max-width: var(--maxw-measure);
    color: var(--ink-2);
  }

  /* ── Прочие утверждения темы ───────────────────────────────────────────── */
  .others {
    --gap: var(--s2);
  }

  .others__title {
    color: var(--ink-3);
  }

  .other {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
    padding: var(--s4);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-md);
    background: var(--surface-raised);
  }

  .other__meta {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  /* ── Решение и выходы из темы ──────────────────────────────────────────── */
  .conf-group__foot {
    justify-content: flex-start;
    align-items: center;
    padding-top: var(--s4);
    border-top: 1px solid var(--line-soft);
  }

  .conf-group__note {
    flex: 1 1 22ch;
    min-width: 0;
    max-width: var(--maxw-measure);
  }

  .conf-group__links {
    justify-content: flex-start;
    gap: var(--s5);
    flex-wrap: wrap;
  }

  /* Служебные имена и коды: под раскрытием, вывод от них не зависит. */
  .svc {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    padding: var(--s3) var(--s4);
    border: 1px dashed var(--line);
    border-radius: var(--r-md);
    background: var(--surface-sunk);
  }

  .svc summary {
    color: var(--ink-3);
    font-weight: 500;
    cursor: pointer;
  }

  .svc .code {
    color: var(--ink-4);
    overflow-wrap: anywhere;
  }

  .svc__row {
    color: var(--ink-3);
  }

  /* ── Пробелы ───────────────────────────────────────────────────────────── */
  .conf__gaps {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    margin-top: var(--s7);
    padding-top: var(--s7);
    border-top: 1px solid var(--line);
  }

  .gaps {
    --gap: var(--s3);
  }

  .gap {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    padding: var(--s5);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-lg);
    background: var(--surface-sunk);
  }

  .gap__kind {
    display: flex;
    align-items: center;
    gap: var(--s2);
    color: var(--ink-3);
  }

  .gap__meta {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .gap__locs {
    justify-content: flex-start;
    gap: var(--s4);
  }

  /* ── Очередь противоречий ──────────────────────────────────────────────── */
  .conf__queue {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    margin-top: var(--s7);
    padding-top: var(--s7);
    border-top: 1px solid var(--line);
  }

  .conf-queue__counter {
    color: var(--ink-3);
  }

  .conf-queue__list {
    --gap: var(--s5);
  }

  .conf-queue__skeleton {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    padding: var(--s6);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-xl);
    background: var(--surface-raised);
  }

  /* Геометрия скелетона — в классе, а не в inline-стилях разметки. */
  .conf-queue__sk-panel {
    display: block;
    height: var(--s9);
    border-radius: var(--r-lg);
  }

  .conf-pair {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    padding: clamp(var(--s4), 2.4vw, var(--s6));
  }

  .conf-pair__head {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: start;
    gap: var(--s3) var(--s4);
    padding-bottom: var(--s3);
    border-bottom: 1px solid var(--line-soft);
  }

  .conf-pair__title {
    min-width: 0;
    text-wrap: pretty;
  }

  .conf-pair__marks {
    justify-content: flex-end;
    gap: var(--s3);
  }

  /* Почему это противоречие: подпись над пояснением, чтобы строка не читалась
     как ещё одна формулировка источника. */
  .conf-pair__why {
    color: var(--ink-3);
  }

  .conf-pair__reason {
    max-width: var(--maxw-measure);
    color: var(--ink-2);
    text-wrap: pretty;
  }

  .conf-pair__decided {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
    color: var(--ink-3);
  }

  .conf-pair__actions {
    justify-content: flex-start;
    align-items: center;
    gap: var(--s4);
    padding-top: var(--s4);
    border-top: 1px solid var(--line-soft);
  }

  .conf-pair__hint {
    flex: 1 1 26ch;
    min-width: 0;
    max-width: var(--maxw-measure);
  }

  /* Сторона пары: формулировка тезиса, число с единицей и путь в первоисточник. */
  .conf-side {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    min-width: 0;
  }

  .conf-side__head {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .conf-side__class {
    color: var(--ink-3);
  }

  .conf-side__value {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .conf-side__label {
    color: var(--ink-3);
  }

  .conf-side__num {
    font-family: var(--font-data);
    font-size: var(--t-small);
    font-weight: 600;
    color: var(--ink);
    overflow-wrap: anywhere;
  }

  .conf-side__docs {
    justify-content: flex-start;
    gap: var(--s4);
  }

  .conf-side__nodoc {
    color: var(--ink-3);
  }

  /* Закрытая сторона не должна выглядеть открытым тезисом: её поле отличается
     границей и фоном из токенов, а не цветом «ошибки». */
  .pair__side[data-class='restricted'],
  .pair__side[data-class='internal'] {
    border: 1px dashed var(--line-strong);
    border-radius: var(--r-md);
    background: var(--surface-sunk);
  }

  @media (max-width: 900px) {
    .conf__form {
      grid-template-columns: minmax(0, 1fr);
    }

    .pair {
      grid-template-columns: minmax(0, 1fr);
    }

    /* Разделитель сторон на мобильном — подписанная линия, а не сжатая таблица. */
    .pair__seam {
      position: static;
      transform: none;
      display: flex;
      align-items: center;
      gap: var(--s3);
      width: 100%;
      border-left: 0;
      padding: 0 var(--s2);
    }

    .pair__seam::before,
    .pair__seam::after {
      content: '';
      flex: 1 1 auto;
      height: 0;
      border-top: 2px dashed var(--line-strong);
    }

    .pair__seam-label {
      writing-mode: horizontal-tb;
      letter-spacing: var(--tr-body);
    }

    .conf-group__head {
      grid-template-columns: minmax(0, 1fr) auto;
    }

    .conf-group__counts {
      grid-column: 1 / -1;
      justify-content: flex-start;
    }

    .conf-pair__head {
      grid-template-columns: minmax(0, 1fr);
    }

    .conf-pair__marks {
      justify-content: flex-start;
    }
  }

  @media (max-width: 640px) {
    /* Строка отбора не толкается: чипы, действия и решение встают друг под
       друга, а полоса величин остаётся читаемой. */
    .conf__applied {
      align-items: flex-start;
      flex-direction: column;
    }

    .conf__applied-actions {
      justify-content: flex-start;
      width: 100%;
    }

    .conf-pair__actions {
      align-items: flex-start;
      flex-direction: column;
    }

    .conf-group__counts {
      flex-direction: column;
      align-items: flex-start;
      gap: var(--s1);
    }

    .conf-group__foot {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
