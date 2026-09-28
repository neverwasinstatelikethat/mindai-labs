<script lang="ts">
  // Расхождение — это спор двух источников об одном и том же числе. Тема держит
  // обе формулировки рядом, их величины лежат на одной полосе, базовый источник
  // выбирает аналитик, а решение фиксируется в «Оценке ответов». Сбой чтения,
  // пустой корпус и корпус без споров — три разных состояния, не одно «пусто».
  // Шапку, skip-link и <main id="main"> рендерит +layout.svelte.
  import { api, ApiError } from '$lib/api';
  import { countOf, num, pct } from '$lib/format';
  import { navLabel } from '$lib/nav';
  import { session } from '$lib/sessionStore.svelte';
  import {
    CONFLICTS_ACTION,
    CONFLICT_SCALE_WORDS,
    CONFLICT_SIDE,
    GAP_KIND_LABELS,
    GAP_KIND_TITLES,
    OPERATOR_SYMBOL,
    PREDICATE_LABELS,
    PROPERTY_LABELS,
    STATUS_SHORT,
    SUBJECT_LABELS,
    describeScope,
    gapBody,
    knownTerm,
    termOf,
    type ConflictGapKind,
  } from '$lib/terms';
  import {
    DATA_CLASS_LABELS,
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
  // превращаться в одну простыню. Темы, цитаты, «прочие утверждения» и пробелы
  // раскрываются порциями, и объём скрытого назван числом.
  const GROUP_PAGE_SIZE = 6;
  const GAP_PAGE_SIZE = 8;
  const EVIDENCE_PREVIEW = 2;
  const OTHERS_PREVIEW = 3;
  // Сколько тем открыто по умолчанию: остальное раскрывает сам читатель.
  const FIRST_OPEN = 1;

  let items = $state<FindingListItem[]>([]);
  let status = $state<'loading' | 'ready' | 'error'>('loading');
  let failure = $state<{ title: string; text: string; denied: boolean } | null>(null);
  let loadedAt = $state<Date | null>(null);
  // Сколько утверждений в корпусе всего. Без этого «расхождений нет» неотличимо
  // от пустого корпуса; null — показаний не пришли, и экран обязан это сказать.
  let corpusClaims = $state<number | null>(null);

  // ── Отбор и раскрытие: каждый контроль реально меняет список ────────────
  let search = $state('');
  let onlyNumeric = $state(false);
  // pressed у чипа класса данных значит «класс выбран и показан» — тот же
  // смысл, что у фасетных чипов в находках. Пустой выбор = показаны все.
  let pickedClasses = $state<DataClass[]>([]);
  let sortKey = $state<string>('divergence');
  // Форма отбора под сворачиванием: смотреть темы нужно чаще, чем править
  // отбор, — применённые условия остаются строкой над списком.
  let filtersOpen = $state(false);
  let collapsed = $state<Set<string>>(new Set());
  let expanded = $state<Set<string>>(new Set());
  let openQuotes = $state<Set<string>>(new Set());
  let openOthers = $state<Set<string>>(new Set());
  // Ключ темы → пара идентификаторов [база, с чем сверяем]. Пока записи нет,
  // работает авторская пара корпуса; выбирать базу может аналитик.
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
    ratio: number | null;
    relative: number | null;
    overlap: [number, number] | null;
    disjoint: boolean;
  }

  interface Topic {
    /** Заголовок темы: русское имя связки либо формулировка, когда имени нет. */
    title: string;
    named: boolean;
    /** Служебные ключи остаются подписью при человекочитаемом названии. */
    keys: string[];
  }

  /**
   * Пара для сравнения: base — источник, который аналитик считает основным,
   * other — тот, что он с ним сверяет. manual — пару выбрал человек, а не экран.
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
    /** null — относительной величины нет: нет и настоящей базы у шкалы. */
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

  function locatorsOf(evidence: Evidence): Locator[] {
    const rows: Locator[] = [];
    if (evidence.page != null) rows.push({ kind: 'стр.', value: String(evidence.page), numeric: true });
    if (evidence.sheet) rows.push({ kind: 'лист', value: evidence.sheet, numeric: false });
    if (evidence.cell_range) rows.push({ kind: 'ячейки', value: evidence.cell_range, numeric: false });
    if (evidence.char_start != null && evidence.char_end != null) {
      rows.push({ kind: 'симв.', value: `${evidence.char_start}–${evidence.char_end}`, numeric: true });
    }
    return rows;
  }

  function sourceOf(finding: FindingListItem): string {
    return finding.evidence.find((row) => row.source_title)?.source_title ?? '';
  }

  /** Имя свойства: русское, если оно есть в словаре, и технический ключ подписью. */
  function nameOf(map: Record<string, string>, key: string): { name: string; named: boolean } {
    const known = knownTerm(map, key);
    return known ? { name: known, named: true } : { name: key || '—', named: false };
  }

  /** Служебное имя показателя — наведением: русское название важнее ключа. */
  function propHint(propertyName: string, named: boolean): string | undefined {
    return named ? `служебное имя показателя: ${propertyName}` : undefined;
  }

  /** Служебные ключи темы — подписью, а не строкой в заголовке. */
  function keysHint(keys: string[]): string | undefined {
    return keys.length > 0 ? `служебные имена темы: ${keys.join(' · ')}` : undefined;
  }

  function bounds(obs: NumericObservation): { lo: number; hi: number } | null {
    if (obs.min_value != null && obs.max_value != null) return { lo: obs.min_value, hi: obs.max_value };
    if (obs.value != null) return { lo: obs.value, hi: obs.value };
    if (obs.min_value != null) return { lo: obs.min_value, hi: obs.min_value };
    if (obs.max_value != null) return { lo: obs.max_value, hi: obs.max_value };
    return null;
  }

  /** Число вместе с пределом: оператор — часть величины, а не украшение. */
  function obsText(obs: NumericObservation): string {
    const b = bounds(obs);
    if (!b) return 'нет значения';
    if (obs.operator === 'between' && obs.min_value != null && obs.max_value != null) {
      return `${num(b.lo)}–${num(b.hi)}`;
    }
    return `${OPERATOR_SYMBOL[obs.operator]} ${num(obs.value ?? b.lo)}`;
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
        ratio: lo > 0 && hi > lo ? hi / lo : null,
        // Относительная величина есть только там, где есть настоящая база:
        // ноль или переход через ноль базы не дают.
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
   * отношение к меньшему значению. Без базы — null.
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
   * Заголовок темы. Когда словарь знает связку — она называется по-русски, а
   * служебные ключи остаются подписью. Когда не знает — заголовком становится
   * формулировка утверждения, а ключ уходит в подчинённую строку: сырой ключ
   * заголовком не бывает.
   */
  function topicFor(subject: string, predicate: string, list: FindingListItem[]): Topic {
    const keys = [subject, predicate].filter((key) => key.length > 0);
    const subjectName = knownTerm(SUBJECT_LABELS, subject);
    const predicateName = knownTerm(PREDICATE_LABELS, predicate);
    if (subjectName && predicateName) {
      return { title: `${subjectName} · ${predicateName}`, named: true, keys };
    }
    const statement = list.find((finding) => finding.statement.trim().length > 0)?.statement.trim();
    return { title: statement ?? 'тема без формулировки', named: false, keys };
  }

  /**
   * Авторская пара темы: стороны берутся из самого широкого числового
   * расхождения, чтобы рядом встали именно спорящие величины, а не случайные две
   * карточки. Аналитик вправе выбрать базу сам — см. pairOf.
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

  /** Сборка темы из бакета находок: одна и для отбора, и для полного среза. */
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
   * Пара темы на экране: выбор аналитика важнее авторской пары корпуса. Если
   * записанная находка пропала из среза, тема возвращается к авторской паре,
   * а не остаётся с половиной.
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

  function pickBase(group: Group, findingId: string): void {
    const current = pairOf(group);
    if (!current.base || current.base.id === findingId) return;
    // Прежняя база отходит на вторую сторону: иначе выбор одного источника
    // молча терял бы то, с чем сверяли.
    const other =
      current.other && current.other.id !== findingId ? current.other.id : current.base.id;
    pairPicks = { ...pairPicks, [group.id]: [findingId, other] };
  }

  function swapSides(group: Group): void {
    const current = pairOf(group);
    if (!current.base || !current.other) return;
    pairPicks = { ...pairPicks, [group.id]: [current.other.id, current.base.id] };
  }

  function autoPair(group: Group): void {
    if (!(group.id in pairPicks)) return;
    const next = { ...pairPicks };
    delete next[group.id];
    pairPicks = next;
  }

  function othersFor(group: Group, pair: Pair): FindingListItem[] {
    return group.findings.filter((f) => f.id !== pair.base?.id && f.id !== pair.other?.id);
  }

  /** Список выбора базы: без версии два утверждения одного документа не различить. */
  function baseOptionLabel(finding: FindingListItem): string {
    return `${sourceOf(finding) || NO_SOURCE} · версия ${finding.version}`;
  }

  /**
   * Решение по спору уходит в «Оценку ответов» с предвыбранным утверждением:
   * базу аналитик уже назвал, поэтому в отзыв идёт сверяемая сторона.
   */
  function decisionHref(pair: Pair): string {
    const claim = pair.other ?? pair.base;
    return claim ? `/feedback?claim=${encodeURIComponent(claim.id)}` : '/feedback';
  }

  // Полный срез без отбора: по нему считают пробелы, отбор фильтрует только
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
   * которые видны в срезе /api/v1/conflicts, — и названы они по данным.
   * Считаются по полному срезу (allGroups): отбор фильтрует список тем,
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

  /**
   * Три разных факта, а не один «сбой»: доступ закрыт, данные не пришли,
   * список пуст. Объяснение — человеческое, без кодов ответов и путей.
   */
  function describe(reason: unknown): { title: string; text: string; denied: boolean } {
    if (reason instanceof ApiError && reason.status === 403) {
      return {
        title: 'Разбор расхождений недоступен',
        text: 'Оспоренные утверждения этому аккаунту не открыты — доступ к находкам корпуса выдаёт администратор. Проверьте вход и повторите запрос.',
        denied: true,
      };
    }
    if (reason instanceof ApiError && (reason.status === 502 || reason.status === 503)) {
      return {
        title: 'Расхождения не прочитаны',
        text: 'Данные не пришли. Проверьте соединение и повторите запрос: расхождения — данные корпуса, а не ответ модели.',
        denied: false,
      };
    }
    return {
      title: 'Расхождения не загрузились',
      text: 'Список оспоренных утверждений не прочитан. Проверьте соединение и повторите запрос.',
      denied: false,
    };
  }

  let requestSeq = 0;
  async function load(): Promise<void> {
    const call = ++requestSeq;
    status = 'loading';
    failure = null;
    // Объём корпуса читаем параллельно и отдельно: без него нечем отличить
    // пустой корпус от корпуса без споров, а его сбой не имеет права
    // превращать пустой список в вывод о согласных источниках.
    const claimsPromise = api
      .corpusStats()
      .then((stats) => stats.claims)
      .catch(() => null);
    try {
      const data = await api.conflicts();
      const claims = await claimsPromise;
      if (call !== requestSeq) return;
      items = data;
      corpusClaims = claims;
      collapsed = new Set();
      expanded = new Set();
      openQuotes = new Set();
      openOthers = new Set();
      pairPicks = {};
      shownGroups = GROUP_PAGE_SIZE;
      shownGaps = GAP_PAGE_SIZE;
      loadedAt = new Date();
      status = 'ready';
    } catch (reason) {
      if (call !== requestSeq) return;
      failure = describe(reason);
      status = 'error';
    }
  }

  // Срез класса данных считает сервер по подтверждённой сессии — перечитываем,
  // когда права появились.
  let started = false;
  $effect(() => {
    if (session.state === 'unknown' || !canRead) return;
    if (started) return;
    started = true;
    void load();
  });
</script>

<svelte:head>
  <title>Расхождения — Научный Клубок</title>
  <meta
    name="description"
    content="Два источника называют разные числа об одном и том же: здесь видно обе формулировки, места в документах и полосу величин — и можно зафиксировать, какому источнику вы верите."
  />
</svelte:head>

<div class="page conf">
  <div class="wrap">
    <SectionHead
      level="1"
      eyebrow="Расхождения · спор источников"
      title="Расхождения в числах"
      lead="Здесь собраны случаи, когда два документа называют разные числа об одном и том же. Откройте тему: видно, кто что утверждает и где именно в документе. Выберите, какому источнику верите, и зафиксируйте это — остальное останется в корпусе."
    >
      <div class="row conf__aside">
        {#if status === 'ready' && loadedAt}
          <p class="micro muted">
            прочитано <time datetime={loadedAt.toISOString()}>{loadedAtText}</time>
          </p>
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
      <Notice tone="warn" title="Разбор расхождений недоступен">
        Оспоренные утверждения этому аккаунту не открыты — доступ к находкам корпуса выдаёт
        администратор. Проверьте вход и повторите запрос.
        <div class="row conf__aside">
          <Button href="/" variant="quiet" size="sm">На витрину</Button>
        </div>
      </Notice>
    {:else if status === 'loading' && items.length === 0}
      <div class="conf__skeleton" role="status" aria-label="Читаем оспоренные утверждения">
        <span class="skeleton conf__sk-title"></span>
        <span class="skeleton conf__sk-panel"></span>
        <span class="skeleton conf__sk-panel"></span>
        <p class="micro muted">Читаем оспоренные утверждения корпуса…</p>
      </div>
    {:else if status === 'error'}
      <Notice tone={failure?.denied ? 'warn' : 'error'} title={failure?.title ?? 'Расхождения не загрузились'}>
        {failure?.text ?? 'Список оспоренных утверждений не прочитан.'}
        <div class="row conf__aside">
          <Button variant="quiet" size="sm" icon="refresh" onclick={() => void load()}>Повторить запрос</Button>
          <Button href="/findings" variant="ghost" size="sm">Раздел «{navLabel('/findings')}»</Button>
        </div>
      </Notice>
    {:else if items.length === 0}
      {#if corpusClaims === 0}
        <!-- Пустой корпус: числам здесь ещё не между собой спорить, и выдавать
             это за «источники согласованы» нельзя. -->
        <Empty
          icon="layers"
          title="В корпусе пока нет данных"
          body="Расхождение — это два документа, которые называют разные числа об одном и том же. Сравнивать пока нечего: в корпусе нет ни одного утверждения. Пришлите хотя бы два документа — споры появятся здесь сами.">
          {#snippet action()}
            <div class="row">
              <Button href="/findings" variant="action" size="sm">
                Открыть раздел «{navLabel('/findings')}»
              </Button>
            </div>
          {/snippet}
        </Empty>
      {:else if corpusClaims === null}
        <!-- Расхождений нет, а объём корпуса не прочитан: экран не вправе
             объявлять ни пустой корпус, ни согласие источников. -->
        <Empty
          icon="alert"
          title="Расхождений нет, но объём корпуса не прочитан"
          body="Оспоренных утверждений сервис не вернул, а сколько в корпусе данных — прочитать не удалось. Поэтому здесь нет ни вывода о согласии источников, ни утверждения, что корпус пуст. Перечитайте; если повтор даст то же самое — это сбой сервиса, а не пустой корпус.">
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
          title="Расхождений в числах нет"
          body={`В корпусе ${countOf(corpusClaims, 'утверждение', 'утверждения', 'утверждений')}, но ни одно утверждение не помечено как оспоренное: два документа пока не назвали разных чисел об одном и том же. Это не значит, что источники согласованы — расхождение появится, когда в корпусе встанет второй документ по тому же показателю.`}>
          {#snippet action()}
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
      <!-- Применённый отбор — строкой над темами: видно, что список отсеял, и
           есть куда вернуться за изменением. Форма — под сворачиванием. -->
      <div class="conf__applied">
        <p class="micro conf__applied-title"><Icon name="filter" size={14} /> Отбор</p>
        <div class="row conf__applied-chips">
          {#if search.trim()}
            <Chip pressed onclick={() => (search = '')}>поиск: «{search.trim()}» · снять</Chip>
          {/if}
          {#if onlyNumeric}
            <Chip pressed onclick={() => (onlyNumeric = false)}>
              только где числа расходятся · снять
            </Chip>
          {/if}
          {#each pickedClasses as code (code)}
            <Chip pressed onclick={() => toggleClass(code)}>
              {DATA_CLASS_LABELS[code]} · снять
            </Chip>
          {/each}
          {#if sortKey !== 'divergence'}
            <Chip pressed onclick={() => (sortKey = 'divergence')}>порядок: {sortLabel} · снять</Chip>
          {/if}
          {#if !filtersActive}
            <span class="micro muted">{CONFLICTS_ACTION.noFilter}</span>
          {/if}
        </div>
        <div class="row conf__applied-actions">
          <Button size="sm" variant="quiet" expanded={filtersOpen} onclick={toggleFilters}>
            {filtersOpen ? CONFLICTS_ACTION.filterClose : CONFLICTS_ACTION.filterOpen}
          </Button>
          <Button size="sm" variant="ghost" disabled={!filtersActive} onclick={resetFilters}>
            {CONFLICTS_ACTION.filterReset}
          </Button>
          {#if shownGroupRows.length > 0}
            <Button size="sm" variant="link" onclick={toggleAllGroups}>
              {allOpen ? CONFLICTS_ACTION.collapseAll : CONFLICTS_ACTION.expandAll}
            </Button>
          {/if}
        </div>
      </div>

      {#if filtersOpen}
        <Panel raised>
          <form class="conf__form" onsubmit={(event) => event.preventDefault()}>
            <Field
              label="Поиск по теме спора"
              name="conf-search"
              type="search"
              placeholder="формулировка, источник, показатель"
              bind:value={search}
              hint="Ищем по формулировке, названию темы, числам из документа и цитатам доказательств."
            />
            <Select label="Порядок тем" name="conf-sort" bind:value={sortKey} options={SORT_OPTIONS} />
            <div class="conf__chips">
              <p class="micro">Отбор по классу данных</p>
              <div class="row">
                <Chip pressed={onlyNumeric} onclick={() => (onlyNumeric = !onlyNumeric)}>
                  только где числа расходятся
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
                  все классы
                </Chip>
              </div>
            </div>
          </form>
        </Panel>
      {/if}

      {#if groups.length === 0}
        <Empty
          icon="filter"
          title="Под этот отбор тем нет"
          body="Отбор убрал {countOf(items.length, 'оспоренное утверждение', 'оспоренных утверждения', 'оспоренных утверждений')} среза: ни одна тема не прошла по тексту поиска, классу данных или требованию числового расхождения.">
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
        <div class="stack conf__groups">
          {#each shownGroupRows as group, gi (group.id)}
            {@const open = groupOpen(gi, group)}
            {@const pair = pairOf(group)}
            {@const others = othersFor(group, pair)}
            <!-- Там, где числа несопоставимы, сторону нельзя назвать меньшей или
                 большей: она остаётся первым и вторым утверждением спора. -->
            {@const scaled = group.headline !== null}
            <Panel tag="article" tone="default" flush={true}>
              <button
                class="conf-group__head"
                type="button"
                aria-expanded={open}
                aria-controls={group.domId}
                onclick={() => toggleGroup(gi, group)}>
                <span class="conf-group__title">
                  <strong class="h4" title={keysHint(group.topic.keys)}>{group.topic.title}</strong>
                </span>
                <span class="row conf-group__counts">
                  <span class="micro">
                    {countOf(group.findings.length, 'утверждение', 'утверждения', 'утверждений')} ·
                    {countOf(group.sourceCount, 'источник', 'источника', 'источников')}
                  </span>
                  <span class="micro conf-group__div">
                    {#if group.divergence !== null}
                      разница в числах <span class="num">{pct(group.divergence)}</span>
                    {:else}
                      числа не сопоставимы
                    {/if}
                  </span>
                </span>
                <span class="conf-group__icon">
                  <Icon name={open ? 'chevronDown' : 'chevronRight'} size={18} />
                </span>
              </button>

              <!-- Тело живёт в DOM и получает `hidden`: aria-controls ведёт к
                   существующему элементу в обоих состояниях раскрытия. -->
              <div class="conf-group__body" id={group.domId} hidden={!open}>
                  {#if group.findings.length > 1}
                    <div class="conf-pick">
                      <p class="micro conf-pick__label">
                        {CONFLICTS_ACTION.pickBase}: <span class="conf-pick__hint">
                          база — тот источник, с которым сверяем остальное
                        </span>
                      </p>
                      <div class="row">
                        {#each group.findings as finding (finding.id)}
                          <Chip
                            pressed={finding.id === pair.base?.id}
                            onclick={() => pickBase(group, finding.id)}>
                            {baseOptionLabel(finding)}
                          </Chip>
                        {/each}
                      </div>
                      <div class="row conf-pick__side">
                        <Button
                          size="sm"
                          variant="quiet"
                          disabled={!pair.other}
                          onclick={() => swapSides(group)}>
                          {CONFLICTS_ACTION.swapSides}
                        </Button>
                        {#if pair.manual}
                          <Button size="sm" variant="link" onclick={() => autoPair(group)}>
                            {CONFLICTS_ACTION.autoPair}
                          </Button>
                        {/if}
                      </div>
                    </div>
                  {/if}

                  {#if pair.base}
                    <div class="pair">
                      <div class="pair__side pair__side--left">
                        <p class="micro pair__mark">{scaled ? CONFLICT_SIDE.base : CONFLICT_SIDE.first}</p>
                        {@render claimBlock(pair.base)}
                      </div>

                      <div class="pair__seam" aria-hidden="true">
                        <span class="pair__seam-label">расхождение</span>
                      </div>

                      {#if pair.other}
                        <div class="pair__side pair__side--right">
                          <p class="micro pair__mark">
                            {scaled ? CONFLICT_SIDE.other : CONFLICT_SIDE.second}
                          </p>
                          {@render claimBlock(pair.other)}
                        </div>
                      {:else}
                        <div class="pair__side">
                          <Notice tone="warn" title="Второго источника нет">
                            По этой теме в корпусе только одно оспоренное утверждение:
                            сравнивать числа пока не с чем. Пришлите второй документ —
                            спор встанет здесь.
                          </Notice>
                        </div>
                      {/if}
                    </div>
                  {:else}
                    <Notice tone="warn" title="В теме нет утверждений">
                      Отбор не оставил в этой теме ни одного утверждения.
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
                        <div class="scale">
                          <p class="scale__head">
                            <span class="scale__prop" title={propHint(cmp.property, prop.named)}>
                              {prop.name}
                            </span>
                            <span class="micro">{cmp.unit || 'единица в данных не указана'}</span>
                            <span class="micro grow">
                              {countOf(cmp.points.length, 'значение', 'значения', 'значений')}
                              {CONFLICT_SCALE_WORDS.values}
                            </span>
                          </p>
                          {#each cmp.points as point, pi (`${point.findingId}-${pi}`)}
                            {@const band = bandStyle(point, cmp)}
                            {@const isBase = pair.base !== null && point.findingId === pair.base.id}
                            <div class="scale__line" data-base={isBase ? '1' : undefined}>
                              <p class="scale__src">
                                <span class="num">{pi + 1}</span> · {point.source || NO_SOURCE}
                                {#if isBase}
                                  <span class="scale__base">{CONFLICT_SCALE_WORDS.baseTag}</span>
                                {/if}
                                {#each point.locs as loc (loc.kind)}
                                  <span class="locator">{loc.kind} <span class="num">{loc.value}</span></span>
                                {/each}
                                {#if point.locs.length === 0}
                                  <span class="locator">мест в источнике не указаны</span>
                                {/if}
                              </p>
                              <p class="scale__value num">
                                {point.text}{#if cmp.unit} {cmp.unit}{/if}
                              </p>
                              {#if band}
                                <span class="bar scale__track">
                                  <span class="bar__fill" style={band}></span>
                                </span>
                              {:else}
                                <p class="micro scale__noband">{CONFLICT_SCALE_WORDS.noband}</p>
                              {/if}
                            </div>
                          {/each}
                          <p class="scale__cap micro">
                            {#if cmp.spread === 0}
                              {CONFLICT_SCALE_WORDS.equal}
                            {:else}
                              {CONFLICT_SCALE_WORDS.range} <span class="num">{num(cmp.lo)}</span>–<span class="num">{num(cmp.hi)}</span>{#if cmp.unit} {cmp.unit}{/if} ·
                              {CONFLICT_SCALE_WORDS.diff} <span class="num">{num(cmp.spread)}</span>{#if cmp.unit} {cmp.unit}{/if} ·
                              {#if cmp.ratio !== null}
                                {CONFLICT_SCALE_WORDS.times} <span class="num">{num(cmp.ratio)}</span> {CONFLICT_SCALE_WORDS.timesEnd} ·
                              {/if}
                              {#if cmp.relative !== null}
                                {CONFLICT_SCALE_WORDS.share} <span class="num">{pct(cmp.relative)}</span> {CONFLICT_SCALE_WORDS.shareEnd} ·
                              {:else}
                                {CONFLICT_SCALE_WORDS.noBase} ·
                              {/if}
                              {#if cmp.disjoint}
                                {CONFLICT_SCALE_WORDS.disjoint}
                              {:else if cmp.overlap}
                                {CONFLICT_SCALE_WORDS.overlap} <span class="num">{num(cmp.overlap[0])}</span>–<span class="num">{num(cmp.overlap[1])}</span>{#if cmp.unit} {cmp.unit}{/if}
                              {:else}
                                {CONFLICT_SCALE_WORDS.noOverlap}
                              {/if}
                            {/if}
                          </p>
                        </div>
                      {/each}
                    </div>
                  {/if}
                  {#if others.length > 0}
                    {@const othersOpen = openOthers.has(group.id)}
                    {@const hiddenOthers = othersOpen ? 0 : Math.max(others.length - OTHERS_PREVIEW, 0)}
                    <div class="stack others">
                      <p class="micro others__title">
                        Ещё {countOf(others.length, 'утверждение', 'утверждения', 'утверждений')} по этой теме
                        {#if hiddenOthers > 0}· показано {countOf(OTHERS_PREVIEW, 'утверждение', 'утверждения', 'утверждений')}{/if}
                      </p>
                      {#each othersOpen ? others : others.slice(0, OTHERS_PREVIEW) as other (other.id)}
                        <div class="other">
                          <p class="small">{other.statement}</p>
                          <p class="micro other__meta">
                            <span class="locator">{sourceOf(other) || NO_SOURCE}</span>
                            {#if other.evidence[0]}
                              {#each locatorsOf(other.evidence[0]) as loc (loc.kind)}
                                <span class="locator">{loc.kind} <span class="num">{loc.value}</span></span>
                              {/each}
                            {:else}
                              <span class="locator">мест в источнике нет</span>
                            {/if}
                            · версия <span class="num">{other.version}</span>
                            {#if other.scope}
                              {#each describeScope(other.scope) as line, li (li)}
                                <span class="scope__item">{line}</span>
                              {/each}
                            {/if}
                            <StatusPill status={other.status} label={STATUS_SHORT[other.status]} />
                          </p>
                        </div>
                      {/each}
                      {#if hiddenOthers > 0 || othersOpen}
                        <Button variant="quiet" size="sm" onclick={() => toggleOthers(group.id)}>
                          {othersOpen
                            ? 'Свернуть прочие утверждения'
                            : `Показать ещё ${countOf(hiddenOthers, 'утверждение', 'утверждения', 'утверждений')}`}
                        </Button>
                      {/if}
                    </div>
                  {/if}

                  <!-- Идентификаторы не решают, кому верить, но нужны, когда
                       сверяешь запись с сервером: они под раскрытием. -->
                  {#if group.topic.keys.length > 0 || pair.base || pair.other}
                    <details class="svc">
                      <summary class="micro">Служебные данные</summary>
                      {#each group.topic.keys as key (key)}
                        <p class="micro svc__row">служебное имя темы <code class="code">{key}</code></p>
                      {/each}
                      {#if pair.base}
                        <p class="micro svc__row">
                          код утверждения · база <code class="code">{pair.base.id}</code>
                        </p>
                      {/if}
                      {#if pair.other}
                        <p class="micro svc__row">
                          код сверяемого утверждения <code class="code">{pair.other.id}</code>
                        </p>
                      {/if}
                    </details>
                  {/if}

                  <div class="row conf-group__foot">
                    <Button
                      href={decisionHref(pair)}
                      variant="action"
                      size="sm"
                      icon="shield"
                      title={`Решение записывают в разделе «${navLabel('/feedback')}» — ссылка несёт с собой это утверждение`}>
                      {CONFLICTS_ACTION.record}
                    </Button>
                    <p class="micro muted conf-group__note">
                      Сначала выберите, какой источник считать базой: в отзыв уходит то
                      утверждение, которое вы с ней сверяете.
                    </p>
                  </div>

                  <div class="row conf-group__links">
                    <Button href="/findings" variant="link" size="sm" iconEnd="arrowRight">
                      Все находки · «{navLabel('/findings')}»
                    </Button>
                    <Button href="/graph" variant="link" size="sm" iconEnd="arrowRight">
                      Связи темы · «{navLabel('/graph')}»
                    </Button>
                  </div>
              </div>
            </Panel>
          {/each}
        </div>

        <div class="conf__pager">
          <p class="micro">
            показано {countOf(shownGroupRows.length, 'тема', 'темы', 'тем')} из
            <span class="num">{groups.length}</span>
            {#if filtersActive}в отбор{:else}в срезе{/if}
          </p>
          <div class="row">
            {#if groups.length > shownGroupRows.length}
              <Button variant="quiet" size="sm" onclick={() => (shownGroups += GROUP_PAGE_SIZE)}>
                Показать ещё {countOf(Math.min(GROUP_PAGE_SIZE, groups.length - shownGroupRows.length), 'тему', 'темы', 'тем')}
              </Button>
            {:else if shownGroups > GROUP_PAGE_SIZE}
              <Button variant="ghost" size="sm" onclick={() => (shownGroups = GROUP_PAGE_SIZE)}>
                Только первые <span class="num">{GROUP_PAGE_SIZE}</span>
              </Button>
            {/if}
          </div>
        </div>
      {/if}

      {#if groups.length > 0 || gaps.length > 0}
        <section class="conf__gaps">
          <SectionHead
            level="2"
            eyebrow="Чего не хватает"
            title="Почему спор пока нельзя проверить числом"
            lead="Отдельного списка пробелов у сервиса нет: эти записи экран считает по тому же срезу оспоренных утверждений — где нет второго источника, где числа названы в разных единицах, где нет самого числа или места в документе."
          />

          {#if gaps.length === 0}
            <Empty
              icon="checkCircle"
              title="Пробелов в срезе нет"
              body="Каждая тема среза держит два утверждения, где один и тот же показатель назван в одинаковых единицах, и у обоих есть место в первоисточнике: страницу, лист или диапазон ячеек."
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
                  <p class="gap__meta">
                    <strong class="small" title={keysHint(note.topic.keys)}>{note.topic.title}</strong>
                  </p>
                  <p class="row gap__locs">
                    <span class="locator">{note.source || NO_SOURCE}</span>
                    {#each note.locs as loc (loc.kind)}
                      <span class="locator">{loc.kind} <span class="num">{loc.value}</span></span>
                    {/each}
                    {#if note.locs.length === 0}
                      <span class="locator">доказательств нет</span>
                    {/if}
                  </p>
                </article>
              {/each}
            </div>

            <div class="conf__pager">
              <p class="micro">
                показано {countOf(shownGapsRows.length, 'запись', 'записи', 'записей')} из
                <span class="num">{gaps.length}</span> — отбор тем на пробелы не влияет
              </p>
              <div class="row">
                {#if gaps.length > shownGapsRows.length}
                  <Button variant="quiet" size="sm" onclick={() => (shownGaps += GAP_PAGE_SIZE)}>
                    Показать ещё {countOf(Math.min(GAP_PAGE_SIZE, gaps.length - shownGapsRows.length), 'запись', 'записи', 'записей')}
                  </Button>
                {:else if shownGaps > GAP_PAGE_SIZE}
                  <Button variant="ghost" size="sm" onclick={() => (shownGaps = GAP_PAGE_SIZE)}>
                    Только первые <span class="num">{GAP_PAGE_SIZE}</span>
                  </Button>
                {/if}
              </div>
            </div>
          {/if}
        </section>
      {/if}
    {/if}
  </div>
</div>

{#snippet claimBlock(finding: FindingListItem)}
  <div class="claim">
    <p class="claim__head">
      <StatusPill status={finding.status} label={STATUS_SHORT[finding.status]} />
      <span class="micro">версия <span class="num">{finding.version}</span></span>
      <span class="micro">{DATA_CLASS_LABELS[finding.data_class]}</span>
    </p>
    <h3 class="claim__statement">{finding.statement}</h3>
    <p class="micro claim__conf">
      уверенность извлечения <span class="num">{pct(finding.confidence)}</span>
    </p>

    {#if finding.scope && Object.keys(finding.scope).length > 0}
      <p class="scope">
        <span class="micro scope__title">условия применения</span>
        {#each describeScope(finding.scope) as line (line)}
          <span class="scope__item">{line}</span>
        {/each}
      </p>
    {:else}
      <p class="micro scope scope__title">условия применения в данных не заданы</p>
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
            <span class="micro">{unitOf(obs) || 'единица не указана'}</span>
            <code class="micro obs__raw">{obs.raw_text}</code>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="micro obs obs__none">
        Чисел в находке нет: сравнивать величинами нечем.
      </p>
    {/if}

    {#if finding.evidence.length > 0}
      {@const quotesOpen = openQuotes.has(finding.id)}
      {@const hiddenQuotes = quotesOpen ? 0 : Math.max(finding.evidence.length - EVIDENCE_PREVIEW, 0)}
      {#each quotesOpen ? finding.evidence : finding.evidence.slice(0, EVIDENCE_PREVIEW) as ev, ei (`${finding.id}-ev-${ei}`)}
        <figure class="quote ev">
          <blockquote class="small">{ev.quote}</blockquote>
          <figcaption class="row ev__loc">
            <strong class="micro">{ev.source_title || NO_SOURCE}</strong>
            {#each locatorsOf(ev) as loc (loc.kind)}
              <span class="locator">{loc.kind} <span class="num">{loc.value}</span></span>
            {/each}
            {#if locatorsOf(ev).length === 0}
              <span class="locator">место в источнике не указано</span>
            {/if}
          </figcaption>
        </figure>
      {/each}
      {#if hiddenQuotes > 0 || quotesOpen}
        <Button variant="quiet" size="sm" onclick={() => toggleQuotes(finding.id)}>
          {quotesOpen
            ? 'Свернуть доказательства'
            : `Показать ещё ${countOf(hiddenQuotes, 'цитату', 'цитаты', 'цитат')} из ${finding.evidence.length}`}
        </Button>
      {/if}
    {:else}
      <p class="micro ev__none">
        Мест в источнике нет: утверждение нельзя проверить по документу.
      </p>
    {/if}
  </div>
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

  .conf__groups {
    --gap: var(--s5);
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

  /* ── Тема спора ────────────────────────────────────────────────────────── */
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

  .conf-group__counts {
    justify-content: flex-end;
    gap: var(--s3);
  }

  .conf-group__div {
    color: var(--ink-3);
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

  .claim__conf {
    display: flex;
    gap: var(--s2);
    flex-wrap: wrap;
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

  .obs__raw {
    grid-column: 1 / -1;
    font-family: var(--font-data);
    color: var(--ink-3);
    word-break: break-word;
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
  .scale__cap {
    grid-column: 1 / -1;
    color: var(--ink-3);
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
