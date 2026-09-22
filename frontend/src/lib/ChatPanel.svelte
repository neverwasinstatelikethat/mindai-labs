<script lang="ts">
  /**
   * Рабочая поверхность запроса: вопрос › след прохода › ответ-бумага.
   * Компонент ничего не запрашивает сам: все вызовы живут в маршруте
   * `(app)/research` и приходят колбэками, а право на действие читается из
   * подтверждённой сессии (`session.can`), а не из выбора на клиенте.
   */
  import { tick } from 'svelte';
  import { countOf, num, plural } from '$lib/format';
  import { bandOf, groupIntervals } from '$lib/rail';
  import { session } from '$lib/sessionStore.svelte';
  import {
    AGENT_LABELS,
    INTENT_LABELS,
    MODEL_MODE_LABELS,
    OPERATOR_SYMBOL,
    OPERATOR_WORD,
    PREDICATE_LABELS,
    PROPERTY_LABELS,
    STEP_STATE_LABELS,
    STATUS_PHRASE,
    STATUS_SUPERSEDED,
    SUBJECT_LABELS,
    describeScope,
    describeValue,
    knownTerm,
    termOf,
  } from '$lib/terms';
  import { DATA_CLASS_LABELS } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Chip from '$lib/ui/Chip.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Field from '$lib/ui/Field.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Mascot from '$lib/ui/Mascot.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import PromptInput from '$lib/ui/PromptInput.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';
  import type {
    AgentEvent,
    AnswerPayload,
    ClaimHistory,
    CorpusStats,
    Evidence,
    Finding,
    NumericObservation,
  } from '$lib/types';

  export interface RunFailure {
    label: string;
    detail: string;
    recovery: string;
    question: string;
    // Ссылка на вход нужна только когда доступ истёк: в остальных отказах
    // перелогин ничего не меняет, и предлагать его — значит уводить человека
    // от причины.
    login?: boolean;
  }

  // Поля шага приходят из потока и могут отсутствовать: клиент не додумывает
  // за сервер ни узла, ни состояния, ни длительности — null рисуется как
  // «нет данных».
  export interface TrailStep {
    agent: string | null;
    status: AgentEvent['status'] | null;
    message: string | null;
    duration_ms: number | null;
  }

  export interface SheetNotice {
    kind: 'ok' | 'error';
    title: string;
    detail: string;
  }

  export type HistoryResult =
    | { history: ClaimHistory; error?: undefined }
    | { history: null; error: string };

  interface Measure {
    key: string;
    property: string;
    propertyCode: string | null;
    operatorLabel: string;
    valueText: string;
    unit: string;
    normalizedText: string;
    normalizedUnit: string;
    raw: string;
    left: number;
    width: number;
    limits: { at: number; label: string }[];
    scaleFrom: string;
    scaleTo: string;
    filterNote: string;
    outsideFilter: boolean;
  }

  interface Props {
    question: string;
    answer: AnswerPayload | null;
    running: boolean;
    steps: TrailStep[];
    elapsedMs: number;
    error: RunFailure | null;
    corpus: CorpusStats | null;
    corpusState: 'loading' | 'ready' | 'error';
    examples: string[];
    examplesState: 'loading' | 'ready' | 'error';
    openClaimId: string | null;
    focusKey: string | null;
    busy: 'correction' | 'feedback' | 'export' | 'import' | null;
    notice: SheetNotice | null;
    // Вопрос, пришедший со входа в раздел: им лишь заполняется поле,
    // сам проход по нему не запускается.
    seed: string;
    onask: (question: string) => void;
    onstop: () => void;
    onselect: (id: string | null) => void;
    onfocus: (key: string | null) => void;
    oncorrection: (input: {
      finding: Finding;
      comment: string;
      correction: string;
    }) => Promise<boolean>;
    onfeedback: (input: { verdict: 'accept' | 'reject'; comment: string }) => Promise<boolean>;
    onexport: (format: 'markdown' | 'json-ld') => Promise<boolean>;
    onimport: (file: File) => Promise<boolean>;
    onnotice: (notice: SheetNotice | null) => void;
    onhistory: (claimId: string) => Promise<HistoryResult>;
    onexamples: () => void;
  }

  const {
    question,
    answer,
    running,
    steps,
    elapsedMs,
    error,
    corpus,
    corpusState,
    examples,
    examplesState,
    openClaimId,
    focusKey,
    busy,
    notice,
    seed = '',
    onask,
    onstop,
    onselect,
    onfocus,
    oncorrection,
    onfeedback,
    onexport,
    onimport,
    onnotice,
    onhistory,
    onexamples,
  }: Props = $props();

  // ── Имена реальных значений контракта ─────────────────────────────────────
  // Статусы, намерения, узлы, операторы и свойства берутся из `terms.ts`:
  // здесь остаются только те словари, которых в нём нет (инструменты обхода,
  // исход шага, стадии прохода).

  const TOOL_LABELS: Record<string, string> = {
    hybrid_search: 'Гибридный поиск',
    graph_traverse: 'Обход графа',
    community_search: 'Поиск по сообществам',
    numeric_filter: 'Числовая фильтрация',
    conflict_scan: 'Поиск конфликтов',
    gap_scan: 'Поиск пробелов',
    expert_lookup: 'Поиск экспертов',
  };

  const OBS_LABELS: Record<string, string> = {
    success: 'выполнено',
    warning: 'с предупреждением',
    error: 'с ошибкой',
  };

  type StageKey = 'setup' | 'traverse' | 'answer' | 'other';

  const STAGES: { key: StageKey; label: string; hint: string }[] = [
    {
      key: 'setup',
      label: 'Постановка задачи',
      hint: 'как прочитан вопрос: сущности, фильтры, география, период, глубина',
    },
    { key: 'traverse', label: 'Обход графа', hint: 'что вернул обход и чего не хватает' },
    { key: 'answer', label: 'Сборка ответа', hint: 'тезисы, проверка и ревизии' },
    { key: 'other', label: 'Иные шаги', hint: 'события вне трёх стадий' },
  ];

  // Стадии соответствуют узлам ResearchWorkflow.stream: узел, которого в таблице
  // нет, честно попадает в «иные шаги», а не теряется.
  const AGENT_STAGE: Record<string, StageKey> = {
    planning_agent: 'setup',
    action_planner: 'traverse',
    tool_executor: 'traverse',
    controller: 'traverse',
    reasoner: 'answer',
    critic: 'answer',
    improver: 'answer',
    synthesizer: 'answer',
    finalize: 'answer',
  };

  // Цвет плашки шага: «готово» — состояние по умолчанию, а не успех, поэтому
  // оно нейтральное; заметно только отклонение.
  function stepPill(status: AgentEvent['status']): 'off' | 'hypothesis' | 'disputed' {
    if (status === 'failed') return 'disputed';
    if (status === 'revised') return 'hypothesis';
    return 'off';
  }

  // ── Локальное состояние листа ────────────────────────────────────────────

  let flow = $state<HTMLDivElement | null>(null);
  let rail = $state<HTMLElement | null>(null);
  let narrow = $state(false);
  let draft = $state('');
  let seeded = false;
  let guideOpen = $state(false);
  let correctionFor = $state<string | null>(null);
  let correctionComment = $state('');
  let correctionText = $state('');
  let feedbackVerdict = $state<'accept' | 'reject' | null>(null);
  let feedbackComment = $state('');

  // История версий — по каждому утверждению отдельно: загрузка одного тезиса не
  // перекрашивает остальные, и отказ относится к одному месту, а не ко всем.
  interface HistoryState {
    busy: boolean;
    error: string;
    data: ClaimHistory | null;
  }
  const EMPTY_HISTORY: HistoryState = { busy: false, error: '', data: null };
  let histories = $state<Record<string, HistoryState>>({});

  let trailUser = $state<boolean | null>(null);
  let scaleUser = $state<boolean | null>(null);

  const canAsk = $derived(session.can('query:ask'));
  const canExport = $derived(session.can('export:run'));
  const canFeedback = $derived(session.can('feedback:give'));
  const canSupersede = $derived(session.can('restricted:read'));

  const draftValid = $derived(draft.trim().length >= 3);
  const corpusEmpty = $derived(corpus ? corpus.documents === 0 : null);
  // Ответ уже лёг в лист, а проход ещё открыт: это реальный момент, когда
  // маскот «говорит», а не выдуманное состояние.
  const answerArriving = $derived(running && answer !== null);

  // Вопрос со входа в раздел подставляется в поле один раз и сразу запускается:
  // человек уже отправил его с входной страницы, повторять нажатие не нужно.
  // Пока сессия не подтверждена, ждём — права на проход приходят с сервера.
  $effect.pre(() => {
    const value = seed;
    if (seeded || !value) return;
    draft = value;
    if (!canAsk) return;
    seeded = true;
    queueMicrotask(() => onask(value.trim()));
  });

  // След сворачивается сам, когда проход закончился, но решение человека
  // всегда перекрывает авто-состояние.
  const trailOpen = $derived(trailUser ?? running);
  const scaleOpen = $derived(scaleUser ?? !narrow);

  // Узкий экран — одна вертикальная композиция: шкала и след убираются в
  // раскрытия. $effect.pre, чтобы не мелькать развёрнутым списком на мобильном.
  $effect.pre(() => {
    const query = window.matchMedia('(max-width: 1119px)');
    const sync = () => {
      narrow = query.matches;
    };
    sync();
    query.addEventListener('change', sync);
    return () => query.removeEventListener('change', sync);
  });

  // Новый прогон не наследует правки, вердикты и истории прошлого листа.
  const runKey = $derived(`${answer?.query_id ?? 'нет'}|${running ? 'идёт' : 'стоп'}`);
  $effect(() => {
    void runKey;
    correctionFor = null;
    correctionComment = '';
    correctionText = '';
    feedbackVerdict = null;
    feedbackComment = '';
    histories = {};
  });

  const railGroups = $derived(groupIntervals(answer?.findings ?? []));
  const trailSteps = $derived<TrailStep[]>(
    steps.length ? steps : (answer?.trace ?? []).map((step) => ({ ...step })),
  );

  function stageOf(agent: string): StageKey {
    return AGENT_STAGE[agent] ?? 'other';
  }

  function stepsFor(key: StageKey): TrailStep[] {
    return trailSteps.filter((step) => (step.agent ? stageOf(step.agent) : 'other') === key);
  }

  const stageStrip = $derived(
    STAGES.filter((stage) => stage.key !== 'other' && stepsFor(stage.key).length > 0).map((stage) => ({
      label: stage.label,
      count: stepsFor(stage.key).length,
    })),
  );

  const evidenceTotal = $derived(
    answer ? answer.findings.reduce((sum, finding) => sum + finding.evidence.length, 0) : 0,
  );

  // ── Форматирование ──────────────────────────────────────────────────────

  function seconds(ms: number): string {
    return `${(ms / 1000).toFixed(1)} с`;
  }

  function shortCode(value: string | null | undefined, size = 8): string {
    if (!value) return '—';
    return value.slice(0, size);
  }

  function keyOf(finding: Finding, index: number): string {
    return `${finding.id}#${index}`;
  }

  function prefersReduced(): boolean {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  // ── Служебные строки прохода ──────────────────────────────────────────────
  // Рабочий процесс пишет часть строк ключами онтологии и по-английски
  // (workflow.py). Правим только эти, проверенные шаблоны; незнакомый текст
  // остаётся как есть — переводить данные, которые нельзя назвать, интерфейс не
  // вправе.
  const DECISION_WORDS: Record<string, string> = {
    continue_tools: 'ищем недостающие доказательства',
    reason: 'переходим к ответу',
  };

  const SERVER_PHRASES: { pattern: RegExp; say: (parts: string[]) => string }[] = [
    {
      pattern: /^Intent=([a-z_]+), план из (\d+) действий$/i,
      say: ([intent, count]) =>
        `вопрос прочитан как «${termOf(INTENT_LABELS, intent)}» · план: ${countOf(
          Number(count),
          'действие',
          'действия',
          'действий',
        )}`,
    },
    {
      pattern: /^(\d+) actions → (\d+) findings, (\d+) конфликтов, (\d+) пробелов$/i,
      say: ([actions, findings, conflicts, gaps]) =>
        [
          countOf(Number(actions), 'действие', 'действия', 'действий'),
          countOf(Number(findings), 'утверждение', 'утверждения', 'утверждений'),
          countOf(Number(conflicts), 'расхождение', 'расхождения', 'расхождений'),
          countOf(Number(gaps), 'пробел', 'пробела', 'пробелов'),
        ].join(' · '),
    },
    {
      pattern: /^Перепланировано: (\d+) actions$/i,
      say: ([count]) =>
        `план обновлён: ${countOf(Number(count), 'действие', 'действия', 'действий')}`,
    },
    {
      pattern: /^Решение: (continue_tools|reason)(?:; пробелы: (\d+))?$/i,
      say: ([decision, missing]) =>
        DECISION_WORDS[decision.toLowerCase()] +
        (missing ? ` · не хватает подтверждений: ${missing}` : ''),
    },
    { pattern: /^Собран answer на подтверждённых findings$/i, say: () => 'ответ собран по подтверждённым утверждениям' },
    { pattern: /^Critic одобрил ответ$/i, say: () => 'проверка ответа пройдена' },
    {
      pattern: /^Ревизия ответа по замечаниям Critic$/i,
      say: () => 'ответ пересобран по замечаниям проверки',
    },
    { pattern: /^Деградированный ответ: (.+)$/i, say: ([reason]) => `ответ неполный: ${reason}` },
    { pattern: /^Ответ собран$/i, say: () => 'ответ собран' },
  ];

  function agentName(key: string | null | undefined): string | null {
    if (!key) return null;
    return AGENT_LABELS[key] ?? AGENT_LABELS[key.toLowerCase()] ?? null;
  }

  // Замечания проверки и причины деградации приходят свободным текстом и
  // склеиваются в одну строку: служебные имена, перечни идентификаторов и классы
  // python-ошибок вычищаются по частям.
  // Все замены — функциями: `String.replace` не принимает смешанный набор
  // «строка или функция», а возвращаемая строка нужна одна и та же.
  const PROSE_FIXES: [RegExp, (...parts: string[]) => string][] = [
    [/неизвестные finding ids:\s*\[[^\]]*\]/gi, () => 'утверждения вне собранного доказательства'],
    [/числовая fidelity не выдержана/gi, () => 'числа не подтверждены цитатами'],
    [/\bfinding ids?\b/gi, () => 'идентификаторы утверждений'],
    // Идентификатор записи в середине предложения аналитику не о чём говорит.
    [/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, () => 'утверждение'],
    [
      /узел ([a-z_]+) завершился ошибкой(?:\s*\([^)]*\))?/gi,
      (_all: string, node: string) => `${agentName(node) ?? 'шаг прохода'}: сбой`,
    ],
    [
      /^Узел ([a-z_]+):/i,
      (_all: string, node: string) => `${agentName(node) ?? 'Проход'}:`,
    ],
  ];

  function phraseOf(text: string): string {
    let phrase = text;
    for (const { pattern, say } of SERVER_PHRASES) {
      const match = pattern.exec(text);
      if (match) {
        phrase = say(match.slice(1).map((part) => part ?? ''));
        break;
      }
    }
    return PROSE_FIXES.reduce(
      (value, [pattern, replacement]) => value.replace(pattern, replacement),
      phrase,
    );
  }

  // Полоса интервала: цвет несёт статус, поэтому варианты consensus/disputed
  // берут классы из `app.css`; «гипотеза» доопределена ниже единственной строкой.
  function bandClass(status: Finding['status']): string {
    if (status === 'disputed') return 'bar__fill--disputed';
    if (status === 'consensus') return 'bar__fill--consensus';
    return '';
  }

  function stepName(step: TrailStep): { label: string; code: string | null } {
    const known = agentName(step.agent);
    if (known) return { label: known, code: null };
    return { label: 'шаг прохода', code: step.agent ?? null };
  }

  function stepNote(step: TrailStep): string {
    if (step.duration_ms == null) return 'нет данных';
    return step.duration_ms > 0 ? seconds(step.duration_ms) : '—';
  }

  function signOf(operator: string): string {
    if (operator === 'range') return OPERATOR_SYMBOL.between;
    return OPERATOR_SYMBOL[operator as NumericObservation['operator']] ?? operator;
  }

  function labelOf(operator: string): string {
    return OPERATOR_WORD[operator as NumericObservation['operator']] ?? operator;
  }

  // Ключ онтологии в русском имени или техническая подпись, когда имени нет:
  // сырой ключ в позицию заголовка или текста не попадает.
  function termPair(
    map: Record<string, string>,
    key: string | null | undefined,
    whenMissing: string,
  ): { label: string; code: string | null } {
    if (!key) return { label: whenMissing, code: null };
    const known = knownTerm(map, key);
    return known ? { label: known, code: null } : { label: whenMissing, code: key };
  }

  function propertyTerm(name: string): { label: string; code: string | null } {
    return termPair(PROPERTY_LABELS, name, 'свойство вне словаря');
  }

  function subjectTerm(finding: Finding): { label: string; code: string | null } {
    return termPair(SUBJECT_LABELS, finding.subject, 'субъект не указан');
  }

  function predicateTerm(finding: Finding): { label: string; code: string | null } {
    return termPair(PREDICATE_LABELS, finding.predicate, 'предикат не указан');
  }

  function filterText(filter: {
    operator: string;
    value?: number;
    min_value?: number;
    max_value?: number;
    unit: string;
  }): string {
    if (filter.operator === 'between' || filter.operator === 'range') {
      return `${num(filter.min_value ?? 0)}–${num(filter.max_value ?? 0)} ${filter.unit}`;
    }
    return `${signOf(filter.operator)} ${num(filter.value ?? 0)} ${filter.unit}`;
  }

  // Источник тезиса: название документа, а не идентификатор записи — UUID сам
  // по себе аналитику не о чём говорит.
  function sourceOf(finding: Finding): string {
    return finding.evidence[0]?.source_title || 'запись корпуса';
  }

  function railCode(finding: Finding): string {
    const evidence = finding.evidence[0];
    if (!evidence) return 'без ссылки на источник';
    const place =
      evidence.page != null
        ? `с. ${evidence.page}`
        : evidence.sheet
          ? `лист ${evidence.sheet}`
          : (evidence.cell_range ?? '');
    return `${evidence.source_title || 'запись корпуса'}${place ? ` · ${place}` : ''}`;
  }

  function locParts(evidence: Evidence): string[] {
    const parts: string[] = [];
    if (evidence.page != null) parts.push(`стр. ${evidence.page}`);
    if (evidence.sheet) parts.push(`лист ${evidence.sheet}`);
    if (evidence.cell_range) parts.push(`ячейки ${evidence.cell_range}`);
    if (evidence.char_start != null) {
      parts.push(`сим. ${evidence.char_start}–${evidence.char_end ?? evidence.char_start}`);
    }
    return parts.length ? parts : ['без локатора'];
  }

  function scopeText(scope: Record<string, string> | undefined): string {
    const conditions = describeScope(scope);
    return conditions.length ? conditions.join(' · ') : 'условия не заданы';
  }

  function reviewDate(value: string | null): string {
    if (!value) return '—';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('ru-RU');
  }

  // ── Число: значение, нормализация и относительно фильтра запроса ─────────

  function envelope(observation: NumericObservation): [number, number] | null {
    const lo = observation.normalized_min ?? observation.min_value;
    const hi = observation.normalized_max ?? observation.max_value;
    if (lo != null && hi != null) return [lo, hi];
    const point = observation.normalized_value ?? observation.value;
    return point == null ? null : [point, point];
  }

  function rawText(observation: NumericObservation): string {
    const lo = observation.min_value;
    const hi = observation.max_value;
    if (observation.operator === 'between' && lo != null && hi != null) return `${num(lo)}–${num(hi)}`;
    return observation.value == null ? observation.raw_text : num(observation.value);
  }

  function normalizedText(observation: NumericObservation): string {
    const range = envelope(observation);
    if (observation.operator === 'between' && range) return `${num(range[0])}–${num(range[1])}`;
    const point = observation.normalized_value ?? observation.value;
    return point == null ? '—' : num(point);
  }

  function niceCeil(value: number): number {
    if (!Number.isFinite(value) || value === 0) return 1;
    const magnitude = 10 ** Math.floor(Math.log10(Math.abs(value)));
    return (Math.ceil((value / magnitude) * 4) / 4) * magnitude;
  }

  type Filter = AnswerPayload['query_plan']['numeric_filters'][number];

  function filterRange(filter: Filter): [number, number] {
    if (filter.operator === 'between' || filter.operator === 'range') {
      return [filter.min_value ?? 0, filter.max_value ?? 0];
    }
    if (filter.operator === 'lte' || filter.operator === 'lt') return [-Infinity, filter.value ?? 0];
    if (filter.operator === 'gte' || filter.operator === 'gt') return [filter.value ?? 0, Infinity];
    const point = filter.value ?? 0;
    return [point, point];
  }

  function measureOf(finding: Finding, observation: NumericObservation, index: number): Measure {
    const range = envelope(observation);
    const unit = observation.unit || observation.normalized_unit;
    const axisUnit = observation.normalized_unit || observation.unit;
    const filters = (answer?.query_plan.numeric_filters ?? []).filter(
      (filter) => filter.property_name.toLowerCase() === observation.property_name.toLowerCase(),
    );
    const comparable = filters.find((filter) => filter.unit === observation.normalized_unit) ?? null;
    const comparableRange = comparable ? filterRange(comparable) : null;

    const numbers = [
      range?.[0],
      range?.[1],
      comparableRange?.[0],
      comparableRange?.[1],
    ].filter((value): value is number => typeof value === 'number' && Number.isFinite(value));

    let from = 0;
    let to = 1;
    if (numbers.length) {
      const peak = Math.max(...numbers);
      const floor = Math.min(...numbers, 0);
      const isPercent = axisUnit.trim() === '%' || axisUnit.trim() === 'проц';
      to = isPercent ? Math.max(100, peak) : Math.max(niceCeil(peak * 1.25), niceCeil(peak));
      from = floor < 0 ? -niceCeil(Math.abs(floor) * 1.25) : 0;
      if (to <= from) to = from + 1;
    }
    const span = to - from;
    const pos = (value: number) => Math.max(0, Math.min(100, ((value - from) / span) * 100));

    let left = 0;
    let width = 1.5;
    if (range) {
      left = pos(range[0]);
      width = Math.max(pos(range[1]) - left, 1.5);
    }
    if (observation.operator === 'lt' || observation.operator === 'lte') {
      left = 0;
      width = Math.max(pos(range?.[1] ?? 0), 1.5);
    }
    if (observation.operator === 'gt' || observation.operator === 'gte') {
      left = pos(range?.[0] ?? 0);
      width = Math.max(100 - left, 1.5);
    }

    const limits: { at: number; label: string }[] = [];
    if (range) {
      if (observation.operator === 'between') {
        limits.push({ at: pos(range[0]), label: `низ ${num(range[0])}` });
        limits.push({ at: pos(range[1]), label: `верх ${num(range[1])}` });
      } else {
        limits.push({
          at: pos(range[0]),
          label: `${signOf(observation.operator)} ${num(range[0])}`,
        });
      }
    }
    if (comparable && comparableRange) {
      const [filterLo, filterHi] = comparableRange;
      if (Number.isFinite(filterLo)) limits.push({ at: pos(filterLo), label: `фильтр ${num(filterLo)}` });
      if (Number.isFinite(filterHi) && filterHi !== filterLo) {
        limits.push({ at: pos(filterHi), label: `фильтр ${num(filterHi)}` });
      }
    }

    let outside = false;
    if (comparableRange && range) {
      outside = range[1] < comparableRange[0] || range[0] > comparableRange[1];
    }

    const property = propertyTerm(observation.property_name);

    return {
      key: `${finding.id}#${observation.property_name}#${index}`,
      property: property.label,
      propertyCode: property.code,
      operatorLabel: labelOf(observation.operator),
      valueText: rawText(observation),
      unit,
      normalizedText: normalizedText(observation),
      normalizedUnit: axisUnit,
      raw: observation.raw_text,
      left,
      width,
      limits,
      scaleFrom: num(from),
      scaleTo: num(to),
      filterNote: comparable
        ? `условие плана: ${labelOf(comparable.operator)} ${filterText(comparable)}`
        : '',
      outsideFilter: outside,
    };
  }

  function measuresOf(finding: Finding): Measure[] {
    return finding.observations.map((observation, index) => measureOf(finding, observation, index));
  }

  function findingRange(finding: Finding): [number, number] | null {
    const ranges = finding.observations
      .map(envelope)
      .filter((item): item is [number, number] => item !== null);
    if (!ranges.length) return null;
    return [Math.min(...ranges.map((item) => item[0])), Math.max(...ranges.map((item) => item[1]))];
  }

  function rivalsOf(finding: Finding): { finding: Finding; delta: number }[] {
    if (!finding.subject || !finding.predicate || !answer) return [];
    const own = findingRange(finding);
    if (!own) return [];
    const out: { finding: Finding; delta: number }[] = [];
    for (const other of answer.findings) {
      if (other.id === finding.id) continue;
      if (other.subject !== finding.subject || other.predicate !== finding.predicate) continue;
      const range = findingRange(other);
      if (!range) continue;
      if (range[1] >= own[0] && range[0] <= own[1]) continue;
      out.push({
        finding: other,
        delta: range[0] > own[1] ? range[0] - own[1] : own[0] - range[1],
      });
    }
    return out;
  }

  function valueSummary(finding: Finding): string {
    if (!finding.observations.length) return 'числовых наблюдений нет';
    return finding.observations.map(describeValue).join(' · ');
  }

  const filterRows = $derived(
    (answer?.query_plan.numeric_filters ?? []).map((filter) => {
      const [lo, hi] = filterRange(filter);
      const covered = (answer?.findings ?? [])
        .flatMap((finding) => finding.observations)
        .filter((observation) => {
          const range = envelope(observation);
          if (!range || observation.property_name.toLowerCase() !== filter.property_name.toLowerCase()) {
            return false;
          }
          return range[1] >= lo && range[0] <= hi;
        }).length;
      return { filter, covered };
    }),
  );

  // ── Что показывать внутри стадии: только реальные поля ответа прогона ─────
  // Виды собираются в скрипте, чтобы разметка не сужала типы на глаз.

  const intentView = $derived.by(() => {
    const intent = answer?.intent;
    if (!intent) return null;
    return {
      label: termOf(INTENT_LABELS, intent.primary),
      secondary: intent.secondary.length
        ? intent.secondary.map((code) => termOf(INTENT_LABELS, code)).join(' · ')
        : 'не заявлено',
      entities: intent.entities,
      constraints: intent.constraints,
    };
  });

  const planView = $derived.by(() => {
    const plan = answer?.query_plan;
    if (!plan) return null;
    return {
      mentions: plan.entity_mentions,
      countries: plan.countries,
      period: `${plan.year_from ?? '—'}–${plan.year_to ?? '—'}`,
      hops: plan.max_hops ?? null,
      filters: plan.numeric_filters.length,
    };
  });

  // Итог одного обхода: что вернулось по числам и чем это кончилось. Служебная
  // бухгалтерия цикла (причина, повтор, условие остановки, id following-действий
  // и артефактов) аналитику не нужна — на неполный ответ указывает причина
  // деградации в шапке листа.
  const obsView = $derived(
    (answer?.tool_observations ?? []).map((observation, index) => {
      const tool = knownTerm(TOOL_LABELS, observation.tool);
      return {
        key: `${observation.action_id}#${index}`,
        name: tool ?? 'чтение корпуса',
        toolCode: tool ? null : observation.tool,
        status: observation.status,
        statusLabel: knownTerm(OBS_LABELS, observation.status) ?? 'исход не описан',
        summary: observation.summary,
        findings: observation.finding_ids?.length ?? 0,
        nodes: observation.graph_node_ids?.length ?? 0,
      };
    }),
  );

  // Стадия видна, когда на неё пришли события или когда у неё есть данные.
  // До ответа стадии остаются в каркасе с честной подписью «придёт с ответом»:
  // по ним аналитик читает проход.
  const stageBlocks = $derived.by(() => {
    const waiting = !answer;
    return STAGES.map((stage) => {
      const items = stepsFor(stage.key);
      const data =
        stage.key === 'setup'
          ? intentView !== null || planView !== null
          : stage.key === 'traverse'
            ? obsView.length > 0
            : false;
      const show = items.length > 0 || data || (waiting && stage.key !== 'other');
      return { key: stage.key, label: stage.label, hint: stage.hint, items, data, show };
    }).filter((block) => block.show);
  });

  // ── Фокус и прокрутка ───────────────────────────────────────────────────

  function scrollTo(attribute: string, value: string): void {
    const nodes = Array.from(flow?.querySelectorAll<HTMLElement>(`[data-${attribute}]`) ?? []);
    const target = nodes.find((node) => node.dataset[attribute] === value);
    target?.scrollIntoView({ block: 'nearest', behavior: prefersReduced() ? 'auto' : 'smooth' });
  }

  $effect(() => {
    const id = openClaimId;
    if (!id) return;
    void tick().then(() => scrollTo('claim', id));
  });

  $effect(() => {
    const key = focusKey;
    if (!key) return;
    void tick().then(() => scrollTo('evidence', key));
  });

  function onRowKeys(event: KeyboardEvent): void {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    const rows = Array.from(rail?.querySelectorAll<HTMLButtonElement>('[data-tick]') ?? []);
    if (!rows.length) return;
    const current = rows.indexOf(event.currentTarget as HTMLButtonElement);
    const next =
      event.key === 'ArrowDown'
        ? (current + 1) % rows.length
        : (current - 1 + rows.length) % rows.length;
    event.preventDefault();
    rows[next]?.focus();
  }

  // ── Действия ────────────────────────────────────────────────────────────

  function ask(value: string): void {
    const trimmed = value.trim();
    if (trimmed.length < 3 || running || !canAsk) return;
    draft = '';
    correctionFor = null;
    feedbackVerdict = null;
    onnotice(null);
    onask(trimmed);
  }

  function toggleClaim(id: string): void {
    onfocus(null);
    onselect(openClaimId === id ? null : id);
  }

  function toggleEvidence(key: string): void {
    onfocus(focusKey === key ? null : key);
  }

  function startCorrection(finding: Finding): void {
    correctionFor = finding.id;
    correctionComment = '';
    correctionText = finding.statement;
    onnotice(null);
  }

  async function sendCorrection(finding: Finding): Promise<void> {
    const comment = correctionComment.trim();
    const correction = correctionText.trim();
    if (comment.length < 3 || correction.length < 3) return;
    let accepted = false;
    try {
      accepted = await oncorrection({ finding, comment, correction });
    } catch (reason) {
      // Отказ и сетевой сбой не притворяются успехом: причину показываем
      // сообщением, введённый текст сохраняем.
      accepted = false;
      onnotice({
        kind: 'error',
        title: 'Исправление не отправлено',
        detail:
          reason instanceof Error && reason.message
            ? reason.message
            : 'Проверьте соединение и отправьте правку снова — текст сохранён.',
      });
    }
    if (accepted) {
      correctionFor = null;
      correctionComment = '';
      correctionText = '';
    }
  }

  async function sendFeedback(): Promise<void> {
    const comment = feedbackComment.trim();
    if (!feedbackVerdict || comment.length < 3) return;
    let accepted = false;
    try {
      accepted = await onfeedback({ verdict: feedbackVerdict, comment });
    } catch (reason) {
      accepted = false;
      onnotice({
        kind: 'error',
        title: 'Отзыв не отправлен',
        detail:
          reason instanceof Error && reason.message
            ? reason.message
            : 'Проверьте соединение и отправьте отзыв снова — текст сохранён.',
      });
    }
    if (accepted) {
      feedbackVerdict = null;
      feedbackComment = '';
    }
  }

  async function pickFile(event: Event): Promise<void> {
    const field = event.currentTarget as HTMLInputElement;
    const file = field.files?.[0];
    if (!file) return;
    await onimport(file);
    field.value = '';
  }

  function historyState(claimId: string): HistoryState {
    return histories[claimId] ?? EMPTY_HISTORY;
  }

  async function loadHistory(claimId: string): Promise<void> {
    histories = { ...histories, [claimId]: { busy: true, error: '', data: null } };
    try {
      const result = await onhistory(claimId);
      histories = {
        ...histories,
        [claimId]: { busy: false, error: result.error ?? '', data: result.history },
      };
    } catch (reason) {
      // Сбой канала не должен оставаться «вечной загрузкой»: состояние
      // разворачивается в отказ с повтором.
      histories = {
        ...histories,
        [claimId]: {
          busy: false,
          error: reason instanceof Error && reason.message ? reason.message : 'Соединение не ответило',
          data: null,
        },
      };
    }
  }
</script>

<svelte:window
  onkeydown={(event) => {
    // Escape закрывает раскрытое доказательство, но не мешает модальному окну:
    // пока открыт диалог, клавишу забирает он.
    if (event.key !== 'Escape' || !focusKey) return;
    if (document.querySelector('[role="dialog"]')) return;
    onfocus(null);
  }}
/>

<div class="ask">
  <div class="ask__flow" bind:this={flow}>
    <!-- ── 1. СОСТОЯНИЯ ДО ОТВЕТА ────────────────────────────────────────── -->
    {#if !answer && !running && !error}
      {#if corpusEmpty === true}
        <Panel tone="sunk">
          <div class="case">
            <Notice tone="warn" title="Корпус пуст: спрашивать не о чем">
              <p>
                В корпусе пока ни одного документа — сравнивать и трассировать нечего.
              </p>
              <p class="micro">
                Загрузите PDF, DOCX, XLSX, JSON или TXT: как только разбор закончится, вопрос по
                новому материалу можно задать прямо здесь.
              </p>
            </Notice>
            <div class="row">
              <label class="btn btn--action btn--sm upload">
                <Icon name="upload" size={16} />
                {busy === 'import' ? 'Обрабатываем…' : 'Импортировать документ'}
                <input
                  type="file"
                  accept=".pdf,.docx,.xlsx,.json,.txt"
                  disabled={busy === 'import'}
                  onchange={pickFile}
                />
              </label>
              <Button href="/dashboard" variant="quiet" size="sm" icon="gauge">
                Панель состояния корпуса
              </Button>
            </div>
          </div>
        </Panel>
      {:else}
        <Empty icon="compass" title="Ни одного прогона на этом листе" body="
          Впишите вопрос с числом и условием применения: проход вернёт утверждения, и каждое — с
          цитатой и локатором до страницы, листа или диапазона ячеек.">
          {#snippet action()}
            <div class="case__actions">
              {#if corpusEmpty === false}
                <p class="micro">
                  корпус читается: каждый новый вопрос собирает отдельный проход и отдельный лист
                </p>
              {:else if corpusState === 'loading'}
                <p class="micro">показания корпуса ещё не пришли — вопрос можно задать и без них</p>
              {:else if corpusState === 'error'}
                <p class="micro">
                  показания корпуса не получены — вопрос можно задать и без них, на ход это не влияет
                </p>
              {/if}
              {#if examplesState === 'loading'}
                <p class="micro">читаем эталонные вопросы корпуса…</p>
              {:else if examplesState === 'error'}
                <Notice tone="error" title="Эталонные вопросы не получены">
                  <p>Список не загрузился — свои вопросы можно задавать как обычно.</p>
                  <div class="row">
                    <Button variant="quiet" size="sm" icon="refresh" onclick={onexamples}>
                      Загрузить снова
                    </Button>
                  </div>
                </Notice>
              {:else if examples.length}
                <p class="micro case__label">эталонные вопросы корпуса</p>
                {#each examples as example (example)}
                  <Button variant="quiet" size="sm" icon="quote" class="example" onclick={() => ask(example)}>
                    {example}
                  </Button>
                {/each}
              {:else}
                <p class="micro">
                  эталонных вопросов в этом наборе нет — сформулируйте свой, поле ниже
                </p>
              {/if}
            </div>
          {/snippet}
        </Empty>
      {/if}
    {/if}

    {#if error}
      <Panel tone="lav">
        <div class="case">
          <Notice tone="error" title={error.label}>
            <p>{error.detail}</p>
          </Notice>
          <p class="micro"><b>Что дальше.</b> {error.recovery}</p>
          {#if error.question}
            <div class="row">
              <Button variant="ink" size="sm" icon="refresh" onclick={() => ask(error.question)}>
                Повторить проход
              </Button>
              {#if error.login}
                <a class="small" href="/login">Войти заново</a>
              {/if}
            </div>
          {/if}
        </div>
      </Panel>
    {/if}

    {#if notice}
      <div class="notice-row">
        <Notice tone={notice.kind === 'error' ? 'error' : 'ok'} title={notice.title}>
          <p>{notice.detail}</p>
        </Notice>
        <Button variant="ghost" size="sm" onclick={() => onnotice(null)}>Скрыть</Button>
      </div>
    {/if}

    <!-- ── 2. СЛЕД ПРОХОДА: стадии обхода на утопленном листе ───────────── -->
    {#if running || trailSteps.length}
      <Panel tone="sunk">
        <section class="trail" aria-labelledby="trail-title">
          <div class="trail__head">
            <div class="trail__title">
              <h2 class="h4" id="trail-title">
                {#if running}
                  <Mascot mood={answerArriving ? 'say' : 'think'} size={32} label="Агент собирает ответ" />
                  {answerArriving ? 'Ответ приходит' : 'Собираем ответ'}
                {:else}
                  След прохода
                {/if}
              </h2>
              <p class="micro">
                {#if stageStrip.length}
                  {#each stageStrip as stage (stage.label)}
                    <span class="tag">{stage.label} · <b class="num">{stage.count}</b></span>
                  {/each}
                {:else}
                  <span class="tag">шагов ещё не было</span>
                {/if}
              </p>
            </div>
            <div class="trail__meter">
              <span class="trail__elapsed">{seconds(elapsedMs)}</span>
              <!-- Живая область вне сворачиваемого тела: объявление доходит и
                   тогда, когда след закрыт (так он закрыт по умолчанию). -->
              <p class="micro trail__count" role="status" aria-live="polite" aria-atomic="true">
                {#if running}
                  получено {countOf(trailSteps.length, 'шаг', 'шага', 'шагов')} · идёт {seconds(elapsedMs)}
                {:else}
                  получено {countOf(trailSteps.length, 'шаг', 'шага', 'шагов')} · проход занял
                  {seconds(elapsedMs)}
                {/if}
              </p>
              <Button
                variant="quiet"
                size="sm"
                class="trail__toggle"
                expanded={trailOpen}
                controls="trail-body"
                onclick={() => (trailUser = !trailOpen)}
              >
                {trailOpen ? 'Свернуть след' : 'Развернуть след'}
              </Button>
            </div>
          </div>

          <div id="trail-body" class="trail__body" hidden={!trailOpen}>
            <p class="micro trail__honest">
              <Icon name="info" size={14} />
              Общего числа шагов у прохода нет: ниже состоявшиеся шаги и время.
            </p>

            {#if running && !trailSteps.length}
              <p class="trail__waiting">
                Читаем корпус — шаги появятся по мере прохода.
              </p>
            {/if}

            {#each stageBlocks as block (block.key)}
              <div class="stage" data-stage={block.key}>
                <div class="stage__head">
                  <h3 class="small">{block.label}</h3>
                  <span class="micro">{block.hint}</span>
                  {#if block.items.length}
                    <span class="tag num">{block.items.length}</span>
                  {/if}
                </div>

                {#if block.items.length}
                  <ol class="stage__steps">
                    {#each block.items as step, index (index)}
                      {@const name = stepName(step)}
                      {@const phrase = step.message ? phraseOf(step.message) : ''}
                      <li class="step" data-state={step.status ?? 'unknown'}>
                        <span class="step__idx num">{String(index + 1).padStart(2, '0')}</span>
                        <span class="step__body">
                          <span class="step__agent">{name.label}</span>
                          {#if name.code}<code class="code tech">{name.code}</code>{/if}
                          {#if phrase}<span class="step__msg">{phrase}</span>{/if}
                        </span>
                        {#if step.status}
                          <StatusPill
                            status={stepPill(step.status)}
                            label={STEP_STATE_LABELS[step.status]}
                          />
                        {:else}
                          <span class="tag">нет данных</span>
                        {/if}
                        <span class="step__note num">{stepNote(step)}</span>
                      </li>
                    {/each}
                  </ol>
                {:else}
                  <p class="micro">шагов этой стадии ещё не было</p>
                {/if}

                {#if block.key === 'setup'}
                  {#if intentView || planView}
                    {#if intentView}
                      <dl class="kv stage__kv">
                        <dt>основное намерение</dt>
                        <dd>{intentView.label}</dd>
                        <dt>вторичные</dt>
                        <dd>{intentView.secondary}</dd>
                      </dl>
                      {#if intentView.entities.length || intentView.constraints.length}
                        <div class="chips">
                          {#each intentView.entities as entity (entity)}
                            <span class="chip chip--static"><Icon name="target" size={13} />{entity}</span>
                          {/each}
                          {#each intentView.constraints as constraint (constraint)}
                            <span class="chip chip--static"><Icon name="filter" size={13} />{constraint}</span>
                          {/each}
                        </div>
                      {/if}
                    {/if}

                    {#if planView}
                      <div class="chips">
                        {#if planView.mentions.length}
                          {#each planView.mentions as mention (mention)}
                            <span class="chip chip--static"><Icon name="target" size={13} />{mention}</span>
                          {/each}
                        {:else}
                          <span class="chip chip--static">сущности в вопросе не распознаны</span>
                        {/if}
                        {#each planView.countries as country (country)}
                          <span class="chip chip--static"><Icon name="pin" size={13} />{country}</span>
                        {/each}
                      </div>
                      <dl class="kv stage__kv">
                        <dt>период</dt>
                        <dd class="num">{planView.period}</dd>
                        <dt>глубина обхода</dt>
                        <dd class="num">
                          {planView.hops ?? '—'}
                          {planView.hops ? plural(planView.hops, 'скачок', 'скачка', 'скачков') : ''}
                        </dd>
                        <dt>числовых фильтров</dt>
                        <dd class="num">{planView.filters}</dd>
                      </dl>
                      {#if filterRows.length}
                        <div class="table-wrap">
                          <table class="table">
                            <caption class="micro">
                              условия вопроса по числам
                            </caption>
                            <thead>
                              <tr>
                                <th>свойство</th><th>условие</th><th>значение</th><th>покрытие</th>
                              </tr>
                            </thead>
                            <tbody>
                              {#each filterRows as row (row.filter.property_name + row.filter.operator)}
                                {@const property = propertyTerm(row.filter.property_name)}
                                <tr class:no-coverage={row.covered === 0}>
                                  <td>
                                    {property.label}
                                    {#if property.code}<code class="code tech">{property.code}</code>{/if}
                                  </td>
                                  <td>
                                    {labelOf(row.filter.operator)}
                                    <span class="num">{signOf(row.filter.operator)}</span>
                                  </td>
                                  <td class="num">{filterText(row.filter)}</td>
                                  <td class="num">
                                    {row.covered}
                                    {plural(row.covered, 'наблюдение', 'наблюдения', 'наблюдений')}
                                  </td>
                                </tr>
                              {/each}
                            </tbody>
                          </table>
                        </div>
                        <p class="micro">
                          «покрытие» — сколько числовых наблюдений ответа попало под условие вопроса.
                        </p>
                      {/if}
                    {/if}
                  {:else}
                    <p class="micro">
                      намерение и параметры вопроса (сущности, фильтры, география, период, глубина)
                      приходят одним блоком с ответом
                    </p>
                  {/if}
                {/if}

                {#if block.key === 'traverse'}
                  {#if obsView.length}
                    <div class="stage__obs">
                      {#each obsView as observation (observation.key)}
                        <div class="obs">
                          <div class="obs__head">
                            <span class="small">{observation.name}</span>
                            {#if observation.toolCode}<code class="code tech">{observation.toolCode}</code>{/if}
                            <StatusPill
                              status={observation.status === 'error'
                                ? 'disputed'
                                : observation.status === 'warning'
                                  ? 'hypothesis'
                                  : 'consensus'}
                              label={observation.statusLabel}
                            />
                          </div>
                          <p class="small">{observation.summary}</p>
                          <p class="micro">
                            утверждений <b class="num">{observation.findings}</b> · узлов
                            <b class="num">{observation.nodes}</b>
                          </p>
                        </div>
                      {/each}
                    </div>
                  {:else if !answer}
                    <p class="micro">
                      итоги обхода приходят вместе с ответом — пока видно только завершение шагов
                    </p>
                  {/if}
                {/if}
              </div>
            {/each}
          </div>
        </section>
      </Panel>
    {/if}

    <!-- ── 3. ОТВЕТ = БУМАГА ─────────────────────────────────────────────── -->
    {#if answer}
      {@const payload = answer}
      <article class="paper">
        <header class="paper__head">
          <p class="eyebrow">
            <Icon name="doc" size={16} />
            Ответ прогона
            <code class="code paper__id">{shortCode(payload.query_id)}</code>
          </p>
          <h2 class="h3">{payload.question || question}</h2>
          <!-- Режим сборки меняет доверие к ответу: «scripted» и «unavailable»
               выглядят на листе иначе, чем ответ модели. -->
          {#if payload.model_mode !== 'gigachat'}
            <p class="micro paper__mode">
              <Icon name="alert" size={14} />
              {MODEL_MODE_LABELS[payload.model_mode]}
            </p>
          {/if}
          <div class="prose">
            <p class="summary">
              {payload.summary || 'Проход не вернул текстового резюме — ниже только утверждения.'}
            </p>
          </div>
          <div class="paper__bar">
            <dl class="kv paper__kv">
              <dt>затрачено</dt>
              <dd class="num">{seconds(elapsedMs)}</dd>
              <dt>утверждений · ссылок</dt>
              <dd class="num">{payload.findings.length} · {evidenceTotal}</dd>
              <dt>расхождений · пробелов</dt>
              <dd class="num">{payload.conflicts.length} · {payload.knowledge_gaps.length}</dd>
            </dl>
            {#if canExport}
              <div class="paper__export">
                <span class="micro">выгрузка этого ответа</span>
                <Button
                  variant="quiet"
                  size="sm"
                  icon="download"
                  busy={busy === 'export'}
                  disabled={busy === 'export'}
                  onclick={() => void onexport('markdown')}
                >
                  Markdown
                </Button>
                <Button
                  variant="quiet"
                  size="sm"
                  disabled={busy === 'export'}
                  onclick={() => void onexport('json-ld')}
                >
                  JSON-LD
                </Button>
              </div>
            {/if}
            <p class="micro paper__confidence">
              уверенность сборки {num(Math.round(payload.confidence * 100))} % — оценка полноты
              подбора, не проверка числа
            </p>
          </div>
          <!-- Легенда листа свёрнута: порядок чтения важнее пояснений, а правила
               нужны ровно один раз. -->
          <div class="acc paper__guide">
            <button
              class="acc__head"
              type="button"
              aria-expanded={guideOpen}
              aria-controls="paper-guide"
              onclick={() => (guideOpen = !guideOpen)}
            >
              <span>Как читать этот лист</span>
              <Icon name="plus" size={16} class="acc__icon" />
            </button>
            <div id="paper-guide" class="acc__body" hidden={!guideOpen}>
              <ul class="paper__guide-list">
                <li>
                  Число без условия применения и локатора результатом не считается: раскройте тезис и
                  прочитайте фрагмент первоисточника.
                </li>
                <li>
                  Шкала интервалов повторяет список тезисов: ↑ ↓ — переход по шкале, Enter — открыть
                  тезис; полосы сравнимы только внутри одной единицы измерения.
                </li>
                <li>
                  Неполный ответ помечен причинами над списком тезисов; там же видно, что снять из
                  условий, чтобы проход собрался целиком.
                </li>
              </ul>
            </div>
          </div>
        </header>

        {#if payload.degradation_reasons.length}
          <div class="paper__degraded">
            <Notice tone="warn" title="Ответ собран не полностью">
              {#each payload.degradation_reasons as reason (reason)}
                <p>{phraseOf(reason)}</p>
              {/each}
            </Notice>
            <p class="micro">
              Снимите часть условий — числовой фильтр, период или глубину обхода — и спросите заново.
            </p>
          </div>
        {/if}

        {#if !payload.findings.length}
          <div class="paper__empty">
            <Empty icon="search" title="Совпадений нет" body="
              Обход корпуса прошёл до конца и не нашёл ни одного утверждения под этот вопрос. Это
              результат, а не сбой.">
              {#snippet action()}
                <div class="case__actions">
                  {#if payload.knowledge_gaps.length}
                    <p class="micro case__label">пробелы, отмеченные прогоном</p>
                    {#each payload.knowledge_gaps.slice(0, 3) as gap, position (position)}
                      <p class="small gap">{gap}</p>
                    {/each}
                  {/if}
                  <p class="micro">
                    Снимите числовой фильтр, расширьте период или переформулируйте вопрос ближе к
                    формулировкам источников — параметры плана видны в следу прохода выше.
                  </p>
                  {#if payload.conflicts.length || payload.recommendations.length}
                    <p class="micro">
                      При этом прогон всё равно вернул расхождения и рекомендации — они под этим
                      блоком.
                    </p>
                  {/if}
                </div>
              {/snippet}
            </Empty>
          </div>
        {:else}
          <div class="paper__body">
            <!-- Шкала интервалов: клавиатурный путь к тому же содержимому,
                 что и список тезисов, и общий масштаб по единице. -->
            <nav class="scale" bind:this={rail} aria-label="Шкала интервалов ответа">
              <div class="scale__head">
                <h3 class="small">Шкала интервалов</h3>
                <span class="micro">
                  {countOf(payload.findings.length, 'утверждение', 'утверждения', 'утверждений')}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  class="scale__toggle"
                  expanded={scaleOpen}
                  controls="scale-body"
                  onclick={() => (scaleUser = !scaleOpen)}
                >
                  {scaleOpen ? 'Свернуть' : 'Показать'}
                </Button>
              </div>
              <div id="scale-body" hidden={!scaleOpen}>
                {#if railGroups.length}
                  {#each railGroups as group (group.unit)}
                    <div class="scale__group">
                      <p class="micro scale__unit">
                        <b>{group.unit}</b>
                        {#if Number.isFinite(group.lo)}
                          <span class="num">{num(group.lo)}–{num(group.hi)}</span>
                        {:else}
                          <span>числовых наблюдений нет</span>
                        {/if}
                      </p>
                      {#each group.rows as row (row.item.id)}
                        {@const band = bandOf(row, group)}
                        <button
                          class="tick"
                          type="button"
                          data-tick={row.item.id}
                          aria-current={row.item.id === openClaimId ? 'true' : undefined}
                          onkeydown={onRowKeys}
                          onclick={() => {
                            onselect(row.item.id);
                            onfocus(null);
                          }}
                        >
                          <span class="tick__idx num">{String(row.index + 1).padStart(2, '0')}</span>
                          <span class="tick__body">
                            <span class="tick__code">{railCode(row.item)}</span>
                            <span class="tick__label">{row.item.statement}</span>
                            <span class="bar">
                              {#if band}
                                <span
                                  class="bar__fill {bandClass(row.item.status)}"
                                  data-status={row.item.status}
                                  style="left: {band.left}%; width: {band.width}%;"
                                ></span>
                              {/if}
                            </span>
                            <span class="micro">
                              {#if row.bounds}
                                <b class="num">{num(row.bounds[0])}–{num(row.bounds[1])} {group.unit}</b>
                              {:else}
                                <b>без числа</b>
                              {/if}
                              · ссылок <b class="num">{row.item.evidence.length}</b>
                            </span>
                          </span>
                        </button>
                      {/each}
                    </div>
                  {/each}
                  <p class="micro">
                    полосы сравнимы только внутри одной единицы: шкала группирует наблюдения по ней.
                  </p>
                {:else}
                  <p class="micro">проход не оставил интервалов — утверждения без числовых наблюдений</p>
                {/if}
              </div>
            </nav>

            <div class="theses">
              {#each payload.findings as finding (finding.id)}
                {@const open = finding.id === openClaimId}
                {@const rivals = rivalsOf(finding)}
                <!-- subject/predicate объявляются на уровне перебора: {@const}
                     внутри <div> компилятор Svelte не принимает. -->
                {@const subject = subjectTerm(finding)}
                {@const predicate = predicateTerm(finding)}
                <article class="thesis" class:thesis--open={open} data-claim={finding.id}>
                  <div class="thesis__head">
                    <div class="thesis__marks">
                      <StatusPill status={finding.status} label={STATUS_PHRASE[finding.status]} />
                      <span class="tag">
                        <Icon
                          name={finding.data_class === 'restricted'
                            ? 'lock'
                            : finding.data_class === 'internal'
                              ? 'shield'
                              : 'eye'}
                          size={13}
                        />
                        {DATA_CLASS_LABELS[finding.data_class]}
                      </span>
                      <span class="tag">версия <b class="num">{finding.version}</b></span>
                      {#if finding.superseded_by}
                        <span class="tag">
                          {STATUS_SUPERSEDED} <code class="code tech">{shortCode(finding.superseded_by)}</code>
                        </span>
                      {/if}
                    </div>
                    <h3 class="thesis__statement">{finding.statement}</h3>
                    <p class="micro thesis__spo">
                      <b>{subject.label}</b> · {predicate.label} · {scopeText(finding.scope)}
                      {#if subject.code || predicate.code}
                        <code class="code tech"
                          >{subject.code ?? ''}{#if subject.code && predicate.code} · {/if}{predicate.code ?? ''}</code
                        >
                      {/if}
                    </p>
                    <Button
                      variant="quiet"
                      size="sm"
                      class="thesis__toggle"
                      expanded={open}
                      controls={`trace-${finding.id}`}
                      onclick={() => toggleClaim(finding.id)}
                    >
                      {open ? 'Свернуть разбор' : 'Открыть разбор'}
                    </Button>
                  </div>

                  <div id={`trace-${finding.id}`}>
                    {#if !open}
                      <p class="micro thesis__closed">
                        {valueSummary(finding)} ·
                        {countOf(finding.evidence.length, 'доказательство', 'доказательства', 'доказательств')} —
                        откройте тезис, чтобы прочитать цитаты и локаторы
                      </p>
                    {:else}
                      {#if finding.observations.length}
                        <div class="measures">
                          <p class="micro measures__label">
                            числовые наблюдения · нормализация и положение относительно условия вопроса
                          </p>
                          {#each measuresOf(finding) as measure (measure.key)}
                            <div class="measure">
                              <div class="measure__top">
                                <span class="micro">
                                  {measure.property}
                                  {#if measure.propertyCode}<code class="code tech">{measure.propertyCode}</code>{/if}
                                  · {measure.operatorLabel}
                                </span>
                                <span class="measure__value num">{measure.valueText} {measure.unit}</span>
                              </div>
                              <div class="bar measure__track">
                                <span
                                  class="bar__fill {bandClass(finding.status)}"
                                  data-status={finding.status}
                                  style="left: {measure.left}%; width: {measure.width}%;"
                                ></span>
                                {#each measure.limits as limit, position (position)}
                                  <span class="measure__limit" style="left: {limit.at}%"></span>
                                {/each}
                              </div>
                              <p class="micro measure__scale">
                                шкала <span class="num">{measure.scaleFrom}–{measure.scaleTo} {measure.normalizedUnit}</span>
                                · норм. <span class="num">{measure.normalizedText} {measure.normalizedUnit}</span>
                              </p>
                              {#if measure.limits.length}
                                <p class="micro measure__limits">
                                  {#each measure.limits as limit, position (position)}
                                    <span class="tag num">{limit.label}</span>
                                  {/each}
                                </p>
                              {/if}
                              <p class="micro">в источнике: «{measure.raw}»</p>
                              {#if measure.filterNote}
                                <p class="micro measure__filter" class:outside={measure.outsideFilter}>
                                  {measure.filterNote}{#if measure.outsideFilter} · наблюдение вне фильтра{/if}
                                </p>
                              {/if}
                            </div>
                          {/each}
                        </div>
                      {:else}
                        <p class="thesis__bare">
                          числовых наблюдений нет — тезис держится только на цитате
                        </p>
                      {/if}

                      {#if rivals.length}
                        <div class="rivals">
                          <p class="micro rivals__label">
                            расхождение на одном интервале · {subjectTerm(finding).label} ›
                            {predicateTerm(finding).label}
                          </p>
                          <div class="rivals__row" data-self>
                            <span class="small">{sourceOf(finding)} · этот тезис</span>
                            <span class="num">{valueSummary(finding)}</span>
                          </div>
                          {#each rivals as rival (rival.finding.id)}
                            <div class="rivals__row">
                              <span class="small">{sourceOf(rival.finding)}</span>
                              <span class="num">{valueSummary(rival.finding)}</span>
                            </div>
                            <p class="micro">
                              нормализованные границы не пересекаются на
                              <b class="num">{num(rival.delta)}</b>
                              {finding.observations[0]?.normalized_unit ?? ''}
                            </p>
                          {/each}
                          <p class="micro">
                            Полный разбор — в разделе «Расхождения»; там видно, какая пара попала в
                            противоречие по всем условиям.
                          </p>
                        </div>
                      {/if}

                      <!-- Каждое доказательство раскрывается с клавиатуры -->
                      <div class="trace">
                        <p class="micro trace__label">
                          доказательство ·
                          {countOf(finding.evidence.length, 'ссылка', 'ссылки', 'ссылок')} на фрагменты
                        </p>
                        {#if !finding.evidence.length}
                          <Notice tone="error" title="Тезис не трассируется">
                            <p>
                              Утверждение пришло без единого локатора: проверить его по
                              первоисточнику нельзя, пока разбор не добавит доказательство.
                            </p>
                          </Notice>
                        {:else}
                          {#each finding.evidence as evidence, index (index)}
                            {@const itemKey = keyOf(finding, index)}
                            {@const expanded = itemKey === focusKey}
                            <div
                              class="evidence"
                              class:evidence--open={expanded}
                              data-evidence={itemKey}
                            >
                              <button
                                class="evidence__head"
                                type="button"
                                aria-expanded={expanded}
                                aria-controls={`body-${itemKey.replace('#', '-')}`}
                                onclick={() => toggleEvidence(itemKey)}
                              >
                                <span class="evidence__src">
                                  <Icon name={expanded ? 'chevronDown' : 'chevronRight'} size={16} />
                                  <b>{evidence.source_title}</b>
                                </span>
                                <span class="locator">
                                  {#each locParts(evidence) as part, position (part)}
                                    {#if position > 0}<span class="locator__sep" aria-hidden="true">·</span>{/if}
                                    <span>{part}</span>
                                  {/each}
                                </span>
                                <span class="micro">{expanded ? 'скрыть цитату' : 'прочитать цитату'}</span>
                              </button>
                              <div
                                class="evidence__body"
                                id={`body-${itemKey.replace('#', '-')}`}
                                hidden={!expanded}
                              >
                                <p class="quote">«{evidence.quote}»</p>
                                <dl class="kv evidence__kv">
                                  <dt>источник</dt>
                                  <dd>{evidence.source_title || 'запись корпуса'}</dd>
                                  {#if evidence.page != null}
                                    <dt>страница</dt><dd class="num">{evidence.page}</dd>
                                  {/if}
                                  {#if evidence.sheet}
                                    <dt>лист</dt><dd>{evidence.sheet}</dd>
                                  {/if}
                                  {#if evidence.cell_range}
                                    <dt>ячейки</dt><dd class="num">{evidence.cell_range}</dd>
                                  {/if}
                                  {#if evidence.char_start != null}
                                    <dt>смещение символов</dt>
                                    <dd class="num">{evidence.char_start}–{evidence.char_end ?? evidence.char_start}</dd>
                                  {/if}
                                </dl>
                                <p class="micro">
                                  Esc — закрыть раскрытие; тот же фрагмент доступен в «Находках» и на
                                  карте связей.
                                </p>
                              </div>
                            </div>
                          {/each}
                        {/if}
                      </div>

                      {#if canSupersede}
                        <div class="thesis__actions">
                          {#if correctionFor === finding.id}
                            <div class="correction">
                              <Field
                                label="Причина правки"
                                name={`correction-comment-${finding.id}`}
                                type="textarea"
                                rows={2}
                                placeholder="Что не так с формулировкой или числом"
                                hint="Минимум три символа — иначе правку не принять."
                                bind:value={correctionComment}
                              />
                              <Field
                                label="Исправленное утверждение"
                                name={`correction-text-${finding.id}`}
                                type="textarea"
                                rows={2}
                                hint="Минимум три символа — текст станет новой версией утверждения."
                                bind:value={correctionText}
                              />
                              <Notice tone="info" title="Правка относится к этому тезису">
                                <p>
                                  Она создаёт новую версию утверждения и оставляет прежнюю в истории
                                  связей — ответ целиком не меняется.
                                </p>
                              </Notice>
                              <div class="row">
                                <Button
                                  variant="action"
                                  size="sm"
                                  busy={busy === 'correction'}
                                  disabled={busy === 'correction'
                                    || correctionComment.trim().length < 3
                                    || correctionText.trim().length < 3}
                                  onclick={() => void sendCorrection(finding)}
                                >
                                  Заменить версию
                                </Button>
                                <Button variant="quiet" size="sm" onclick={() => (correctionFor = null)}>
                                  Отмена
                                </Button>
                              </div>
                            </div>
                          {:else}
                            <Button
                              variant="quiet"
                              size="sm"
                              icon="plus"
                              disabled={running}
                              onclick={() => startCorrection(finding)}
                            >
                              Исправить это утверждение
                            </Button>
                          {/if}
                        </div>
                      {:else if canFeedback}
                        <p class="micro thesis__note">
                          Замена утверждения доступна при расширенном доступе. Отзыв по ответу
                          целиком — ниже.
                        </p>
                      {/if}

                      <!-- История версий открытого утверждения: состояние своё у
                           каждого тезиса, отказ — тоже свой. -->
                      {@const hist = historyState(finding.id)}
                      <div class="history">
                        <p class="micro history__label">история версий утверждения</p>
                        <div class="row">
                          <Button
                            variant="quiet"
                            size="sm"
                            icon="clock"
                            busy={hist.busy}
                            disabled={hist.busy}
                            onclick={() => void loadHistory(finding.id)}
                          >
                            {hist.busy ? 'Читаем…' : hist.data ? 'Обновить версии' : 'Запросить версии'}
                          </Button>
                        </div>
                        {#if hist.error}
                          <Notice tone="error" title="История версий не получена">
                            <p>{hist.error}</p>
                            <div class="row">
                              <Button
                                variant="quiet"
                                size="sm"
                                icon="refresh"
                                onclick={() => void loadHistory(finding.id)}
                              >
                                Повторить запрос
                              </Button>
                            </div>
                          </Notice>
                        {:else if hist.data}
                          {#if !hist.data.versions.length}
                            <p class="micro">других версий у этого тезиса нет.</p>
                          {:else}
                            {#each hist.data.versions as version (version.finding_id + version.version)}
                              <div class="version">
                                <p class="small">{version.statement}</p>
                                <p class="micro">
                                  версия <b class="num">{version.version}</b> ·
                                  <StatusPill
                                    status={version.status}
                                    label={STATUS_PHRASE[version.status]}
                                  />
                                  {#if version.review_date}
                                    <time datetime={version.review_date}>{reviewDate(version.review_date)}</time>
                                  {:else}
                                    разбор не проводился
                                  {/if}
                                  {#if version.reviewer_id}
                                    · правил <code class="code tech">{shortCode(version.reviewer_id)}</code>
                                  {/if}
                                </p>
                                {#if version.review_reason}<p class="micro">{version.review_reason}</p>{/if}
                              </div>
                            {/each}
                          {/if}
                        {/if}
                      </div>
                    {/if}
                  </div>
                </article>
              {/each}
            </div>
          </div>
        {/if}

        {#if payload.conflicts.length || payload.knowledge_gaps.length || payload.recommendations.length}
          <section class="block">
            <div class="block__head">
              <h3 class="h4">Где источники расходятся и где молчат</h3>
              <p class="micro">
                Конфликт — пара утверждений об одном субъекте и предикате при непересекающихся
                нормализованных диапазонах; пробел — непокрытая комбинация измерений.
              </p>
            </div>
            {#each payload.conflicts as item, index (index)}
              <div class="line">
                <span class="tag">расхождение {String(index + 1).padStart(2, '0')}</span>
                <p class="small">{item}</p>
              </div>
            {/each}
            {#each payload.knowledge_gaps as item, index (index)}
              <div class="line">
                <span class="tag">пробел {String(index + 1).padStart(2, '0')}</span>
                <p class="small">{item}</p>
              </div>
            {/each}
            {#each payload.recommendations as item, index (index)}
              <div class="line">
                <span class="tag">шаг проверки {String(index + 1).padStart(2, '0')}</span>
                <p class="small">{item}</p>
              </div>
            {/each}
            <div class="row">
              <Button href="/conflicts" variant="quiet" size="sm" icon="conflict">
                Все расхождения корпуса
              </Button>
            </div>
          </section>
        {/if}

        {#if canFeedback}
          <section class="block">
            <div class="block__head">
              <h3 class="h4">Отзыв по ответу</h3>
              <p class="micro">
                Отзыв ложится на ответ этого прогона и создаёт предложение на проверку. Замена
                конкретного утверждения — в разборе тезиса выше.
              </p>
            </div>
            <div class="verdicts">
              <span class="micro">вердикт</span>
              <Chip pressed={feedbackVerdict === 'accept'} onclick={() => (feedbackVerdict = 'accept')}>
                Ответ полезен
              </Chip>
              <Chip pressed={feedbackVerdict === 'reject'} onclick={() => (feedbackVerdict = 'reject')}>
                Ответу не верю
              </Chip>
            </div>
            <Field
              label="Комментарий"
              name="answer-feedback"
              type="textarea"
              rows={2}
              placeholder="Что проверить в первую очередь"
              hint="Минимум три символа — пустой комментарий не принять."
              bind:value={feedbackComment}
            />
            <Button
              variant="action"
              size="sm"
              busy={busy === 'feedback'}
              disabled={busy === 'feedback' || !feedbackVerdict || feedbackComment.trim().length < 3}
              onclick={() => void sendFeedback()}
            >
              Отправить отзыв
            </Button>
          </section>
        {:else}
          <p class="micro">
            Отзыв по ответу доступен при расширенном доступе: без него ответ остаётся на листе, но
            предложение эволюции из него не создаётся.
          </p>
        {/if}
      </article>
    {/if}
  </div>

  <!-- ── 4. КОМПОЗЕР: прилип ко дну рабочего листа, маскот = состояние прогона -->
  <div class="ask__composer">
    <!-- Показания корпуса, остановка прохода и «Приложить документ» живут своей
         строкой над полем: ряд инструментов композера на узком экране уходит в
         скрытый горизонтальный скролл, а эти действия обязательные. -->
    <div class="ask__meta">
      <span class="tag ask__corpus">
        {#if corpusState === 'loading'}
          читаем показания корпуса…
        {:else if corpusState === 'error'}
          показания корпуса не получены
        {:else if corpus}
          корпус: документов <b class="num">{corpus.documents}</b> · утверждений
          <b class="num">{corpus.claims}</b> · фрагментов <b class="num">{corpus.chunks}</b> ·
          сущностей <b class="num">{corpus.entities}</b>
        {:else}
          показания корпуса не получены
        {/if}
      </span>
      {#if running}
        <Button variant="quiet" size="sm" icon="close" onclick={onstop}>Остановить проход</Button>
      {/if}
      <label class="btn btn--sm btn--quiet upload ask__attach">
        <Icon name="upload" size={16} />
        {busy === 'import' ? 'Обрабатываем…' : 'Приложить документ'}
        <input
          type="file"
          accept=".pdf,.docx,.xlsx,.json,.txt"
          disabled={busy === 'import'}
          onchange={pickFile}
        />
      </label>
    </div>

    <PromptInput
      bind:value={draft}
      variant="docked"
      name="ask"
      label="Вопрос к графу доказательств"
      placeholder="Какие методы обессоливания подходят при сульфатах и хлоридах 200–300 мг/л?"
      busy={running}
      disabled={!canAsk}
      hint={draft && !draftValid ? 'минимум три символа' : 'Enter — спросить · Shift + Enter — строка'}
      onsubmit={ask}
    />

    {#if draft && !draftValid}
      <p class="field__error ask__note">
        Вопрос короче трёх символов — добавьте формулировку, иначе ответа не будет.
      </p>
    {/if}
    {#if !canAsk}
      <p class="field__error ask__note">
        Спрашивать корпус может подтверждённый аккаунт с доступом к запросам: сейчас его нет.
        Находки и карта связей работают.
      </p>
    {/if}
  </div>
</div>

<style>
  .ask {
    display: flex;
    flex-direction: column;
    gap: var(--s5);
  }

  .ask__flow {
    display: flex;
    flex-direction: column;
    gap: var(--s5);
    min-width: 0;
  }

  /* ── Композер: прилип ко дну листа вместе со своими подсказками ──────── */

  .ask__composer {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    /* Единица прилипания — весь композер: поле PromptInput растёт само по
       содержимому, а строка подсказки не отрывается от поля при прокрутке.
       Лоток непрозрачен: без подложки строка показаний лежит поверх прокрутки
       и список эталонных вопросов читается сквозь неё. */
    position: sticky;
    bottom: var(--s4);
    z-index: var(--z-dock);
    padding: var(--s2) var(--s3) var(--s3);
    border-radius: var(--r-lg);
    background: var(--surface-sunk);
  }

  .ask__note {
    margin: 0;
    padding-inline: var(--s4);
    font-size: var(--t-micro);
  }

  /* Ряд над полем: показания корпуса, остановка прохода и приложение
     документа. Переносится, а не уходит в скрытый скролл. */
  .ask__meta {
    display: flex;
    align-items: center;
    gap: var(--s2);
    flex-wrap: wrap;
    padding-inline: var(--s4);
  }

  .ask__corpus {
    display: inline-flex;
    align-items: center;
    gap: var(--s1);
    flex-wrap: wrap;
    min-width: 0;
    font-variant-numeric: tabular-nums;
  }

  .ask__attach {
    flex: none;
  }

  @media (max-width: 640px) {
    .ask__composer {
      bottom: calc(var(--s3) + env(safe-area-inset-bottom, 0px));
    }

    .ask__note,
    .ask__meta {
      padding-inline: var(--s3);
    }
  }

  /* Файловый инпут живёт в label: сам инпут невидим, но остаётся в
     клавиатурном пути, поэтому focus показываем на всей подписи. */
  .upload {
    position: relative;
  }

  .upload input {
    position: absolute;
    width: 1px;
    height: 1px;
    opacity: 0;
    pointer-events: none;
  }

  .btn.upload:focus-within {
    outline: 2px solid var(--action-ink);
    outline-offset: 3px;
  }

  .btn.upload:hover {
    background: var(--surface-raised);
  }

  .trail__body[hidden],
  .evidence__body[hidden] {
    display: none;
  }

  /* ── Состояния ───────────────────────────────────────────────────────────── */

  .case {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  .case__actions {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: var(--s2);
    width: 100%;
    max-width: var(--maxw-measure);
  }

  .case__label {
    margin-top: var(--s2);
    font-weight: 500;
    color: var(--ink-2);
  }

  .btn.example {
    justify-content: flex-start;
    text-align: left;
    white-space: normal;
    line-height: var(--lh-dense);
    min-height: 44px;
    height: auto;
    padding-block: var(--s2);
  }

  .notice-row {
    display: flex;
    align-items: flex-start;
    gap: var(--s2);
  }

  .notice-row :global(.notice) {
    flex: 1 1 auto;
  }

  /* ── След прохода ────────────────────────────────────────────────────── */

  .trail {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  .trail__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--s4);
    flex-wrap: wrap;
  }

  .trail__title {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    min-width: 0;
  }

  .trail__title .h4 {
    display: flex;
    align-items: center;
    gap: var(--s2);
  }

  .trail__title .micro {
    display: flex;
    gap: var(--s2);
    flex-wrap: wrap;
  }

  .trail__meter {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: var(--s1);
    flex: none;
  }

  .trail__elapsed {
    font-family: var(--font-data);
    font-size: var(--t-h3);
    font-weight: 600;
    line-height: 1.1;
    font-variant-numeric: tabular-nums;
    letter-spacing: var(--tr-head);
  }

  .trail__body {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    padding-top: var(--s3);
    border-top: 1px solid var(--line);
  }

  .trail__honest {
    display: flex;
    align-items: flex-start;
    gap: var(--s2);
    color: var(--ink-2);
    max-width: var(--maxw-measure);
  }

  .trail__waiting {
    display: flex;
    align-items: center;
    gap: var(--s2);
    font-size: var(--t-small);
    color: var(--ink-3);
  }

  .stage {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    padding: var(--s4);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    background: var(--surface);
    box-shadow: var(--shadow-soft);
  }

  .stage__head {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .stage__head .small {
    font-weight: 600;
    color: var(--ink);
    font-size: var(--t-body);
  }

  .stage__head .micro {
    flex: 1 1 200px;
  }

  .stage__steps {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .step {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    padding: var(--s2) var(--s3);
    border-radius: var(--r-sm);
    background: var(--surface-sunk);
    flex-wrap: wrap;
  }

  .step[data-state='failed'] {
    background: var(--disputed-wash);
  }

  .step[data-state='revised'] {
    background: var(--hypothesis-wash);
  }

  .step__idx {
    color: var(--ink-4);
    flex: none;
  }

  .step__body {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    flex-wrap: wrap;
    min-width: 0;
    flex: 1 1 260px;
  }

  .step__agent {
    font-weight: 600;
    font-size: var(--t-small);
  }

  .step__msg {
    font-size: var(--t-micro);
    color: var(--ink-3);
    line-height: var(--lh-dense);
    overflow-wrap: anywhere;
  }

  .step__note {
    flex: none;
    font-size: var(--t-micro);
    color: var(--ink-3);
  }

  .stage__kv {
    max-width: var(--maxw-measure);
  }

  /* Фильтр плана без единого покрытия — факт, а не оформление. */
  .no-coverage {
    background: var(--disputed-wash);
  }

  .stage__kv dd {
    font-family: var(--font-data);
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
  }

  /* Статические чипы плана: длинная сущность не должна расшивать экран
     по горизонтали, поэтому переносим текст внутри чипа. */
  .chips .chip--static {
    white-space: normal;
    max-width: 100%;
    overflow-wrap: anywhere;
  }

  .evidence__kv dd,
  .stage__kv dd,
  .paper__kv dd {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .stage__obs {
    display: grid;
    gap: var(--s3);
    grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr));
  }

  .obs {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
    padding: var(--s3) var(--s4);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-md);
    background: var(--surface-sunk);
  }

  .obs__head {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
  }

  .obs p {
    overflow-wrap: anywhere;
  }

  /* ── Бумага ответа ───────────────────────────────────────────────────── */

  /* Лист лежит на всю ширину (wrap--bleed): плотность даёт таблица и шкала,
     а читается текст в своей мере, поэтому она ограничена внутри колонок. */
  .paper {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--s5);
    padding: clamp(var(--s4), 2.4vw, var(--s7));
    border: 1px solid var(--line);
    border-radius: var(--r-xl);
    background: var(--paper);
    box-shadow: var(--shadow-lift);
  }

  .paper__head {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  .summary {
    margin-top: var(--s2);
    max-width: var(--maxw-measure);
    font-size: var(--t-lead);
    line-height: 1.5;
    color: var(--ink-2);
    white-space: pre-line;
    text-wrap: pretty;
  }

  .paper__guide {
    align-self: flex-start;
    width: 100%;
    max-width: var(--maxw-measure);
  }

  .paper__guide-list {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    margin: 0;
    padding-inline-start: var(--s4);
  }

  .paper__guide-list li {
    text-wrap: pretty;
  }

  .paper__bar {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--s4);
    flex-wrap: wrap;
    padding: var(--s4);
    border: 1px solid var(--line);
    border-radius: var(--r-md);
    background: var(--surface-raised);
  }

  .paper__kv {
    flex: 1 1 320px;
    min-width: 0;
  }

  .paper__export {
    display: flex;
    align-items: center;
    gap: var(--s2);
    flex-wrap: wrap;
    flex: none;
  }

  .paper__degraded {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  /* Служебное имя или идентификатор — только подписью при человекочитаемом
     названии: шрифт данных, размер и цвет младше основного текста. */
  .tech {
    font-size: var(--t-micro);
    font-weight: 400;
    color: var(--ink-4);
    overflow-wrap: anywhere;
  }

  .paper__id {
    color: var(--ink-3);
  }

  /* Режим сборки и само-оценка — сведения, а не измеренные данные: им не место
     в моно-шкале метрик. */
  .paper__mode {
    display: flex;
    align-items: center;
    gap: var(--s2);
    max-width: var(--maxw-measure);
    color: var(--hypothesis);
  }

  .paper__confidence {
    flex: 1 1 100%;
    margin: 0;
    color: var(--ink-3);
  }

  .paper__body {
    display: grid;
    gap: var(--s5);
    grid-template-columns: minmax(260px, 340px) minmax(0, 1fr);
    align-items: start;
  }

  @media (max-width: 1119px) {
    .paper__body {
      grid-template-columns: minmax(0, 1fr);
    }
  }

  /* ── Шкала интервалов ────────────────────────────────────────────────── */

  .scale {
    position: sticky;
    top: calc(var(--topbar-h) + var(--s4));
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    max-height: calc(100vh - var(--topbar-h) - var(--s7));
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-width: thin;
    padding: var(--s4);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    background: var(--surface-sunk);
  }

  @media (max-width: 1119px) {
    .scale {
      position: static;
      max-height: none;
      overflow: visible;
    }
  }

  .scale__head {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
  }

  .scale__head .small {
    font-weight: 600;
    font-size: var(--t-body);
  }

  .scale__head .micro {
    flex: 1 1 100px;
  }

  .btn.scale__toggle,
  .btn.trail__toggle {
    flex: none;
    min-height: 44px;
  }

  .scale__group {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    margin-top: var(--s3);
  }

  .scale__unit {
    display: flex;
    justify-content: space-between;
    gap: var(--s2);
    font-weight: 500;
    color: var(--ink-2);
  }

  .tick {
    display: flex;
    align-items: flex-start;
    gap: var(--s3);
    width: 100%;
    padding: var(--s3);
    border: 1px solid var(--line);
    border-radius: var(--r-md);
    background: var(--surface);
    color: var(--ink-2);
    text-align: left;
    cursor: pointer;
    transition: border-color var(--dur-fast) var(--ease-soft),
      background var(--dur-fast) var(--ease-soft);
  }

  .tick:hover {
    border-color: var(--line-strong);
    background: var(--surface-raised);
  }

  .tick[aria-current='true'] {
    border-color: var(--action-deep);
    background: var(--peach-wash);
    box-shadow: var(--shadow-soft);
  }

  .tick__idx {
    font-size: var(--t-micro);
    color: var(--ink-4);
    flex: none;
  }

  .tick__body {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
    min-width: 0;
    flex: 1 1 auto;
  }

  .tick__code {
    font-size: var(--t-micro);
    color: var(--ink-3);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .tick__label {
    font-size: var(--t-small);
    line-height: var(--lh-dense);
    color: var(--ink);
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    overflow: hidden;
  }

  /* app.css не знает полосы «гипотезы»: доопределяем единственный недостающий
     вариант тем же токеном статуса. */
  .bar__fill[data-status='hypothesis'] {
    background: var(--hypothesis);
  }

  /* ── Тезисы ──────────────────────────────────────────────────────────── */

  .theses {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    min-width: 0;
  }

  .thesis {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    padding: var(--s4) var(--s5);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-lg);
    background: var(--surface);
  }

  .thesis--open {
    border-color: var(--line-strong);
    box-shadow: var(--shadow-soft);
  }

  .thesis__head {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  .thesis__marks {
    display: flex;
    align-items: center;
    gap: var(--s2);
    flex-wrap: wrap;
  }

  .thesis__statement {
    max-width: var(--maxw-measure);
    font-size: var(--t-h4);
    font-weight: 600;
    line-height: var(--lh-head);
    letter-spacing: var(--tr-body);
    text-wrap: pretty;
    overflow-wrap: anywhere;
  }

  .thesis__spo {
    display: flex;
    gap: var(--s2);
    flex-wrap: wrap;
    overflow-wrap: anywhere;
  }

  .thesis__toggle {
    align-self: flex-start;
  }

  .thesis__closed,
  .thesis__bare,
  .thesis__note {
    margin-top: var(--s2);
    max-width: var(--maxw-measure);
    overflow-wrap: anywhere;
  }

  .thesis__bare {
    font-family: var(--font-data);
    font-size: var(--t-micro);
    color: var(--hypothesis);
  }

  /* ── Числовые наблюдения ─────────────────────────────────────────────── */

  .measures {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    margin-top: var(--s4);
  }

  .measures__label {
    font-weight: 500;
    color: var(--ink-2);
  }

  .measure {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    padding: var(--s3) var(--s4);
    border-radius: var(--r-md);
    background: var(--surface-sunk);
  }

  .measure__top {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .measure__top code {
    font-family: var(--font-data);
    color: var(--ink-2);
  }

  .measure__value {
    font-size: var(--t-h4);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  /* Мерка лежит на утопленном поле: трек светлее фона и не сливается с ним,
     метки условий выходят за полосу. */
  .measure__track {
    overflow: visible;
    background: var(--surface-raised);
    box-shadow: inset 0 0 0 1px var(--line);
  }

  .measure__limit {
    position: absolute;
    top: -3px;
    bottom: -3px;
    width: 2px;
    border-radius: var(--r-pill);
    background: var(--ink-3);
    transform: translateX(-1px);
  }

  .measure__scale,
  .measure__limits,
  .measure__filter {
    display: flex;
    gap: var(--s2);
    flex-wrap: wrap;
  }

  .measure__filter {
    color: var(--ink-3);
  }

  .measure__filter.outside {
    color: var(--disputed);
    font-weight: 500;
  }

  /* ── Расхождение ─────────────────────────────────────────────────────── */

  .rivals {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    margin-top: var(--s4);
    padding: var(--s4);
    border: 1px dashed var(--line-strong);
    border-radius: var(--r-md);
    background: var(--disputed-wash);
  }

  .rivals__label {
    font-weight: 600;
    color: var(--disputed);
  }

  .rivals__row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .rivals__row[data-self] {
    font-weight: 600;
  }

  /* ── Доказательства ──────────────────────────────────────────────────── */

  .trace {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    margin-top: var(--s4);
    padding-left: var(--s4);
    border-left: 1px solid var(--coral-mist);
  }

  .trace__label {
    font-weight: 500;
    color: var(--ink-2);
  }

  .evidence {
    border: 1px solid var(--line-soft);
    border-radius: var(--r-md);
    background: var(--surface-raised);
    overflow: hidden;
  }

  .evidence--open {
    border-color: var(--action);
    box-shadow: var(--shadow-soft);
  }

  .evidence__head {
    display: flex;
    align-items: center;
    gap: var(--s3);
    width: 100%;
    padding: var(--s3) var(--s4);
    min-height: 44px;
    border: 0;
    background: none;
    color: inherit;
    text-align: left;
    cursor: pointer;
    flex-wrap: wrap;
    transition: background var(--dur-fast) var(--ease-soft);
  }

  .evidence__head:hover {
    background: var(--peach-wash);
  }

  .evidence__head:focus-visible {
    outline: 2px solid var(--action-ink);
    outline-offset: -3px;
  }

  .evidence__src {
    display: flex;
    align-items: center;
    gap: var(--s2);
    min-width: 0;
    flex: 1 1 200px;
    font-size: var(--t-small);
  }

  .evidence__src b {
    font-weight: 600;
    overflow-wrap: anywhere;
  }

  .evidence__head .locator {
    flex-wrap: wrap;
    font-family: var(--font-data);
    flex: 1 1 220px;
    min-width: 0;
  }

  .locator__sep {
    color: var(--line-strong);
  }

  .evidence__body {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    padding: 0 var(--s4) var(--s4);
  }

  .evidence__kv {
    max-width: var(--maxw-measure);
  }

  /* ── Правка и история версий ─────────────────────────────────────────── */

  .thesis__actions {
    margin-top: var(--s4);
  }

  .correction {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    max-width: var(--maxw-narrow);
    padding: var(--s4);
    border: 1px solid var(--lavender-deep);
    border-radius: var(--r-md);
    background: var(--lavender);
  }

  .history {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    margin-top: var(--s4);
    padding-top: var(--s3);
    border-top: 1px solid var(--line-soft);
  }

  .history__label {
    font-weight: 500;
    color: var(--ink-2);
  }

  .version {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
    padding: var(--s3);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-sm);
    background: var(--surface-sunk);
  }

  .version p {
    overflow-wrap: anywhere;
  }

  .version :global(.status) {
    vertical-align: middle;
  }

  /* ── Блоки расхождений и отзыва ──────────────────────────────────────── */

  .block {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    padding: var(--s5);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-lg);
    background: var(--surface-sunk);
  }

  .block__head {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
  }

  .block .micro {
    max-width: var(--maxw-measure);
  }

  .line {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    flex-wrap: wrap;
    padding: var(--s3) 0;
    border-top: 1px solid var(--line);
  }

  .line p {
    flex: 1 1 320px;
    text-wrap: pretty;
    overflow-wrap: anywhere;
  }

  .verdicts {
    display: flex;
    align-items: center;
    gap: var(--s2);
    flex-wrap: wrap;
  }

  .gap {
    color: var(--ink-2);
    text-wrap: pretty;
  }

  .paper__empty,
  .paper__empty .case__actions {
    max-width: none;
  }
</style>
