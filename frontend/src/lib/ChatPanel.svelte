<script lang="ts">
  /**
   * Рабочая поверхность запроса: вопрос › след прохода › ответ-бумага.
   * Компонент ничего не запрашивает сам: все вызовы живут в маршруте
   * `(app)/research` и приходят колбэками, а право на действие читается из
   * подтверждённой сессии (`session.can`), а не из выбора на клиенте.
   */
  import { tick } from 'svelte';
  import { page } from '$app/state';
  import { countOf, duration, num, plural } from '$lib/format';
  import { navLabel } from '$lib/nav';
  import { bandOf, groupIntervals } from '$lib/rail';
  import { scrollRegion } from '$lib/scroll-region';
  import { session } from '$lib/sessionStore.svelte';
  import {
    ANSWER_HEAD,
    ASK_ACTIONS,
    ASK_EMPTY,
    ASK_FEEDBACK,
    ASK_NO_MATCH,
    ASK_REVIEW,
    ASK_SOURCE,
    ASK_TRAIL,
    DEGRADED,
    INTENT_LABELS,
    MODEL_MODE_LABELS,
    OPERATOR_SYMBOL,
    OPERATOR_WORD,
    PREDICATE_LABELS,
    PROPERTY_LABELS,
    READING_GUIDE,
    RUN_OUTCOMES,
    RUN_STAGES,
    RUN_STRINGS,
    RUN_TOOLS,
    SCALE_LABELS,
    SHEET_LABELS,
    STEP_STATE_LABELS,
    STATUS_PHRASE,
    STATUS_SUPERSEDED,
    SUBJECT_LABELS,
    TERM_FALLBACKS,
    TRUST_PANEL,
    type RunStage,
    degradationOf,
    describeScope,
    describeValue,
    hopsText,
    knownTerm,
    linksToText,
    moreThesesText,
    stageOfNode,
    termOf,
    thesesShown,
    tracePhraseOf,
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
  import Sheet from '$lib/ui/Sheet.svelte';
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
    // Вопрос, пришедший со входа в раздел: подставляется в поле и сразу
    // запускается — человек уже отправил его с входной страницы. Пока сессия
    // не подтверждена, ждём: права на проход приходят с сервера.
    seed: string;
    onask: (question: string) => void;
    // Прежний ответ уходит с экрана только по явному действию человека:
    // серверного перечня прошлых ответов нет, и молча терять собранный ответ
    // нельзя — его нечем восстановить.
    onclear: () => void;
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
    onclear,
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
  // Словарь меток один и живёт в `terms.ts` (зона RS): стадии сборки ответа,
  // инструменты, исходы шага и перевод строк рабочего процесса. Узел потока
  // попадает в стадию через `stageOfNode`: узла нет в перечне, шаг идёт в
  // «Другие шаги» и не получает выдуманного имени.

  type StageKey = RunStage;

  // Цвет плашки шага: «готово» это состояние по умолчанию, а не успех, поэтому
  // плашка нейтральная; заметно только отклонение.
  function stepPill(status: AgentEvent['status']): 'off' | 'hypothesis' | 'disputed' {
    if (status === 'failed') return 'disputed';
    if (status === 'revised') return 'hypothesis';
    return 'off';
  }

  // ── Локальное состояние экрана ───────────────────────────────────────────

  let flow = $state<HTMLDivElement | null>(null);
  let rail = $state<HTMLElement | null>(null);
  let narrow = $state(false);
  // Набранный вопрос живёт в поле до ответа: отказ не должен стоить набранного
  // текста, поэтому поле очищает только пришедший ответ (см. $effect ниже).
  let draft = $state('');
  let seeded = false;
  let lastAsked = '';
  // Подсказка «Как читать ответ» на пустом экране раскрыта, в готовом ответе
  // свёрнута: новичок обязан увидеть правила до первого ответа, а готовый ответ
  // не начинается с lesson.
  let guideOpen = $state(true);
  // Раскрытие «Почему ответу можно верить»: режим сборки, время, узлы, глубина
  // поиска и длительность шагов.
  let trustOpen = $state(false);
  let correctionFor = $state<string | null>(null);
  let correctionComment = $state('');
  let correctionText = $state('');
  let feedbackVerdict = $state<'accept' | 'reject' | null>(null);
  let feedbackComment = $state('');
  // Список тезисов с потолком: 500 тезисов не должны превращать экран в 500
  // карточек и 500 тиков шкалы.
  const THESES_PAGE = 20;
  let thesesLimit = $state(THESES_PAGE);

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

  // Разбор тезиса — шторка (DESIGN.md называет это подписью продукта): открыта
  // максимум для одного тезиса. Выбор тезиса (подсветка в шкале и на карточке)
  // живёт отдельно в openClaimId, поэтому ответ прогона подсвечивает первый
  // тезис, но шторку сам не распахивает.
  let sheetClaimId = $state<string | null>(null);

  const canAsk = $derived(session.can('query:ask'));
  const canExport = $derived(session.can('export:run'));
  const canFeedback = $derived(session.can('feedback:give'));
  const canSupersede = $derived(session.can('restricted:read'));

  const draftValid = $derived(draft.trim().length >= 3);
  const corpusEmpty = $derived(corpus ? corpus.documents === 0 : null);
  // Ответ уже лёг в лист, а проход ещё открыт: это реальный момент, когда
  // маскот «говорит», а не выдуманное состояние.
  const answerArriving = $derived(running && answer !== null);
  // Прежний ответ остаётся на экране, пока новый не пришёл или пока человек
  // не убрал его сам: серверного перечня прошлых ответов нет, и молча потерять
  // собранный ответ — значит потерять работу без возможности вернуться.
  const staleAnswer = $derived(
    !!answer && (answer.question ?? '').trim() !== (question ?? '').trim(),
  );
  let staleDismissed = $state(false);
  // Бумага на экране: ответ есть и он либо свежий, либо его не убрали.
  const showPaper = $derived(!!answer && !(staleAnswer && staleDismissed));

  // Телеметрия сборки уходит под раскрытие (см. TRUST_PANEL): узлы, длительность
  // шагов и глубина поиска — показатели контура, а не вывод для аналитика.
  const nodesRead = $derived(
    (answer?.tool_observations ?? []).reduce(
      (sum, observation) => sum + (observation.graph_node_ids?.length ?? 0),
      0,
    ),
  );
  const elapsedKnown = $derived(
    typeof elapsedMs === 'number' && Number.isFinite(elapsedMs) && elapsedMs > 0,
  );

  // Тезис, чей разбор открыт в шторке: ищется в текущем ответе, поэтому новый
  // ответ закрывает шторку сам — прежнего тезиса на экране больше нет.
  const sheetFinding = $derived(
    answer?.findings.find((finding) => finding.id === sheetClaimId) ?? null,
  );

  // Вопрос со входа в раздел подставляется в поле один раз и сразу запускается:
  // человек уже отправил его с входной страницы, повторять нажатие не нужно.
  // Пока сессия не подтверждена, ждём: права приходят с сервера вместе с аккаунтом.
  $effect.pre(() => {
    const value = seed.trim();
    if (seeded || !value || !canAsk) return;
    seeded = true;
    draft = value;
    lastAsked = value;
    queueMicrotask(() => onask(value));
  });

  // Поле очищается только когда ответ на этот вопрос пришёл: при отказе, при
  // остановке и во время сборки набранный текст остаётся видимым.
  $effect(() => {
    const answered = (answer?.question ?? '').trim();
    if (!running && answered && answered === lastAsked && draft === lastAsked) {
      draft = '';
      lastAsked = '';
    }
  });

  // След сворачивается сам, когда сборка закончилась, но решение человека
  // всегда перекрывает автоматическое состояние.
  const trailOpen = $derived(trailUser ?? running);
  const scaleOpen = $derived(scaleUser ?? !narrow);

  // Узкий экран: одна вертикальная композиция, шкала и след убираются в
  // раскрытия. $effect.pre, чтобы не мелькать развёрнутым списком на мобильном.
  // 900px это контрактная точка компоновки из DESIGN.md (диапазон 640–900
  // планшет): JS-переключатель обязан совпадать с media-правилами стилей ниже,
  // иначе шкала свёрнута, а её сетка ещё двухколоночная.
  $effect.pre(() => {
    const query = window.matchMedia('(max-width: 900px)');
    const sync = () => {
      narrow = query.matches;
    };
    sync();
    query.addEventListener('change', sync);
    return () => query.removeEventListener('change', sync);
  });

  // Новый ответ не наследует правки, вердикты и истории прежнего; с ним же
  // снимается и решение убрать прежний ответ с экрана.
  const runKey = $derived(`${answer?.query_id ?? 'нет'}|${running ? 'идёт' : 'стоп'}`);
  $effect(() => {
    void runKey;
    staleDismissed = false;
    correctionFor = null;
    correctionComment = '';
    correctionText = '';
    feedbackVerdict = null;
    feedbackComment = '';
    histories = {};
    thesesLimit = THESES_PAGE;
    // На пустом экране правила чтения раскрыты, в готовом ответе свёрнуты.
    guideOpen = answer === null && !running;
  });

  const visibleFindings = $derived((answer?.findings ?? []).slice(0, thesesLimit));
  const findingsHidden = $derived((answer?.findings ?? []).length - visibleFindings.length);
  const railGroups = $derived(groupIntervals(visibleFindings));
  const trailSteps = $derived<TrailStep[]>(
    steps.length ? steps : (answer?.trace ?? []).map((step) => ({ ...step })),
  );

  function stageOf(agent: string | null): StageKey {
    return agent ? (stageOfNode(agent)?.key ?? 'other') : 'other';
  }

  function stepsFor(key: StageKey): TrailStep[] {
    return trailSteps.filter((step) => stageOf(step.agent) === key);
  }

  const stageStrip = $derived(
    RUN_STAGES.filter((stage) => stage.key !== 'other' && stepsFor(stage.key).length > 0).map(
      (stage) => ({ label: stage.label, count: stepsFor(stage.key).length }),
    ),
  );

  const evidenceTotal = $derived(
    answer ? answer.findings.reduce((sum, finding) => sum + finding.evidence.length, 0) : 0,
  );

  // Телеметрия сборки уходит под раскрытие «Почему ответу можно верить»: узлы,
  // глубина и длительность шагов это показатели контура, а не вывод аналитика.
  // Вывод (статус тезиса, класс доступа и версия, цитата с местом в источнике,
  // расхождения и пробелы) остаётся на виду.
  const trustRows = $derived.by(() => {
    if (!answer) return [];
    const rows: { label: string; value: string; note?: string }[] = [
      { label: TRUST_PANEL.modeLabel, value: MODEL_MODE_LABELS[answer.model_mode] },
      {
        label: TRUST_PANEL.timeLabel,
        value: elapsedKnown ? duration(elapsedMs) : TRUST_PANEL.timeMissing,
      },
      { label: TRUST_PANEL.stepsLabel, value: String(trailSteps.length) },
      { label: TRUST_PANEL.nodesLabel, value: num(nodesRead) },
      { label: TRUST_PANEL.depthLabel, value: hopsText(planView?.hops ?? null) },
      { label: TRUST_PANEL.filtersLabel, value: String(answer.query_plan.numeric_filters.length) },
      {
        label: TRUST_PANEL.confidenceLabel,
        value: `${num(Math.round(answer.confidence * 100))} %`,
        note: TRUST_PANEL.confidenceNote,
      },
    ];
    return rows;
  });

  // Служебные данные ответа: одно раскрытие на весь ответ (А10). Идентификатор
  // ответа, имена узлов, длительность шагов и сырые причины неполного ответа:
  // всё, что нужно для сверки с сервером и чего не должно быть в тексте вывода.
  const serviceRows = $derived.by(() => {
    const rows: { label: string; value: string }[] = [];
    if (answer) {
      rows.push({ label: ANSWER_HEAD.queryIdLabel, value: answer.query_id });
    }
    trailSteps.forEach((step, index) => {
      rows.push({
        label: `шаг ${String(index + 1).padStart(2, '0')}`,
        value: `${step.agent ?? RUN_STRINGS.stepFallback}, ${
          step.duration_ms == null ? RUN_STRINGS.noTime : duration(step.duration_ms)
        }`,
      });
    });
    for (const observation of obsView) {
      rows.push({
        label: `узел ${observation.name}`,
        value: observation.toolCode
          ? `${observation.nodes} узлов корпуса, ключ: ${observation.toolCode}`
          : `${observation.nodes} узлов корпуса`,
      });
    }
    answer?.degradation_reasons.forEach((reason, index) => {
      rows.push({ label: `причина неполного ответа ${index + 1}`, value: reason });
    });
    return rows;
  });

  // ── Форматирование ──────────────────────────────────────────────────────
  // Длительности читает общий `duration` из `$lib/format`, как на остальных
  // экранах: до минуты десятые доли секунды, дальше минуты. Идентификаторы
  // целиком уходят в «Служебные данные», поэтому сокращать их до восьми
  // символов в интерфейсе больше нечего.

  function keyOf(finding: Finding, index: number): string {
    return `${finding.id}#${index}`;
  }

  function prefersReduced(): boolean {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  // Полоса интервала: цвет несёт статус, поэтому варианты consensus/disputed
  // берут классы из `app.css`; «гипотеза» доопределена ниже единственной строкой.
  function bandClass(status: Finding['status']): string {
    if (status === 'disputed') return 'bar__fill--disputed';
    if (status === 'consensus') return 'bar__fill--consensus';
    return '';
  }

  function signOf(operator: string): string {
    if (operator === 'range') return OPERATOR_SYMBOL.between;
    return OPERATOR_SYMBOL[operator as NumericObservation['operator']] ?? operator;
  }

  function labelOf(operator: string): string {
    // «range» — синоним «between» из плана, но ключа в словаре у него нет:
    // сырым английским словом он бы и остался.
    if (operator === 'range') return OPERATOR_WORD.between;
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
    return termPair(PROPERTY_LABELS, name, TERM_FALLBACKS.property);
  }

  function subjectTerm(finding: Finding): { label: string; code: string | null } {
    return termPair(SUBJECT_LABELS, finding.subject, TERM_FALLBACKS.subject);
  }

  function predicateTerm(finding: Finding): { label: string; code: string | null } {
    return termPair(PREDICATE_LABELS, finding.predicate, TERM_FALLBACKS.predicate);
  }

  function filterText(filter: {
    operator: string;
    value?: number;
    min_value?: number;
    max_value?: number;
    unit: string;
  }): string {
    // Поля фильтра опциональны: отсутствующее значение — «—», а не фиктивный
    // ноль, который выглядел бы настоящим пределом плана.
    if (filter.operator === 'between' || filter.operator === 'range') {
      return `${num(filter.min_value)}–${num(filter.max_value)} ${filter.unit}`;
    }
    return `${signOf(filter.operator)} ${num(filter.value)} ${filter.unit}`;
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
    return place
      ? `${evidence.source_title || 'запись корпуса'}, ${place}`
      : evidence.source_title || 'запись корпуса';
  }

  // Место в источнике человекочитаемыми словами. Символьные offsets это
  // служебные данные: они ушли под «Служебные данные» этого разбора.
  function locParts(evidence: Evidence): string[] {
    const parts: string[] = [];
    if (evidence.page != null) parts.push(`стр. ${evidence.page}`);
    if (evidence.sheet) parts.push(`лист ${evidence.sheet}`);
    if (evidence.cell_range) parts.push(`ячейки ${evidence.cell_range}`);
    return parts.length ? parts : [SHEET_LABELS.noLocator];
  }

  /** Служебные строки одного доказательства: символы и идентификатор записи. */
  function evidenceServiceRows(finding: Finding): { label: string; value: string }[] {
    return finding.evidence.map((evidence, index) => {
      const from = evidence.char_start;
      const to = evidence.char_end ?? evidence.char_start;
      return {
        label: `${SHEET_LABELS.source} ${index + 1}, ${SHEET_LABELS.chars}`,
        value: from == null ? 'нет' : `${from}–${to}`,
      };
    });
  }

  /** Служебные данные разбора: адрес тезиса, символы цитат, идентификаторы
   *  записей и автор правки. Для сверки с сервером, для чтения тезиса не нужно. */
  function sheetServiceRows(finding: Finding): { label: string; value: string }[] {
    const rows: { label: string; value: string }[] = [
      { label: SHEET_LABELS.thesisId, value: finding.id },
    ];
    // Ключ онтологии вне словаря: в тексте наблюдения его показывать нельзя,
    // он остаётся здесь, для сверки с сервером.
    measuresOf(finding).forEach((measure, index) => {
      if (measure.propertyCode) {
        rows.push({
          label: `${SHEET_LABELS.propertyCode} ${String(index + 1).padStart(2, '0')}`,
          value: measure.propertyCode,
        });
      }
    });
    rows.push(...evidenceServiceRows(finding));
    const doc = finding.evidence[0]?.document_id;
    if (doc) rows.push({ label: SHEET_LABELS.documentId, value: doc });
    if (finding.superseded_by) {
      rows.push({ label: SHEET_LABELS.supersededById, value: finding.superseded_by });
    }
    for (const version of historyState(finding.id).data?.versions ?? []) {
      if (version.reviewer_id) {
        rows.push({
          label: `${SHEET_LABELS.reviewerId}, ${SHEET_LABELS.versionLabel} ${version.version}`,
          value: version.reviewer_id,
        });
      }
    }
    return rows;
  }

  // Ссылки под доказательством: их ровно две, и обе ведут туда, где адрес
  // параметра читают. «Находки» берут срез по утверждению (`claim`) и по
  // документу (`document`) и сами объясняют, если записи в их окне нет; карта
  // связей открывает узел по `claim`. Обещать переход, который никуда не ведёт,
  // нельзя: третьей ссылки здесь нет.
  function findingsHref(finding: Finding): string {
    const params = new URLSearchParams();
    params.set('claim', finding.id);
    const doc = finding.evidence[0]?.document_id;
    if (doc) params.set('document', doc);
    return `/findings?${params.toString()}`;
  }

  function graphHref(finding: Finding): string {
    const params = new URLSearchParams();
    params.set('claim', finding.id);
    return `/graph?${params.toString()}`;
  }

  function scopeText(scope: Record<string, string> | undefined): string {
    const conditions = describeScope(scope);
    return conditions.length ? conditions.join(', ') : 'условия не заданы';
  }

  function reviewDate(value: string | null): string {
    if (!value) return 'нет даты';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('ru-RU');
  }

  /** Период вопроса словами: прочерк зарезервирован за пустым значением, а
   *  «2019–» читалось бы как оборванное число. */
  function periodText(from: number | null | undefined, to: number | null | undefined): string {
    if (from != null && to != null) return `${from}–${to}`;
    if (from != null) return `с ${from} года`;
    if (to != null) return `по ${to} год`;
    return 'не задан';
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
    // Пустое значение печатает `num`: прочерк закреплён за ним по всему интерфейсу.
    return num(observation.normalized_value ?? observation.value);
  }

  function niceCeil(value: number): number {
    if (!Number.isFinite(value) || value === 0) return 1;
    const magnitude = 10 ** Math.floor(Math.log10(Math.abs(value)));
    return (Math.ceil((value / magnitude) * 4) / 4) * magnitude;
  }

  type Filter = AnswerPayload['query_plan']['numeric_filters'][number];

  // Пределы фильтра для шкалы и покрытия. Частично заданный диапазон интервала
  // не образует, а точка без значения — не ноль: такой фильтр предела не даёт
  // вовсе, и выдумывать за планом его нечего.
  function filterRange(filter: Filter): [number, number] | null {
    if (filter.operator === 'between' || filter.operator === 'range') {
      return filter.min_value != null && filter.max_value != null
        ? [filter.min_value, filter.max_value]
        : null;
    }
    if (filter.value == null) return null;
    if (filter.operator === 'lte' || filter.operator === 'lt') return [-Infinity, filter.value];
    if (filter.operator === 'gte' || filter.operator === 'gt') return [filter.value, Infinity];
    return [filter.value, filter.value];
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
        ? `${SHEET_LABELS.planCondition}: ${labelOf(comparable.operator)} ${filterText(comparable)}`
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
    return finding.observations.map(describeValue).join(', ');
  }

  // Фильтр вовсе без значений не показывает строку в условиях: «равно —» или
  // «в диапазоне —–—» условия не задают. Без вычислимых пределов нет и покрытия —
  // вместо счётчика честно стоит «—».
  const filterRows = $derived(
    (answer?.query_plan.numeric_filters ?? [])
      .filter(
        (filter) =>
          filter.value != null || filter.min_value != null || filter.max_value != null,
      )
      .map((filter) => {
        const range = filterRange(filter);
        const covered = range
          ? (answer?.findings ?? [])
              .flatMap((finding) => finding.observations)
              .filter((observation) => {
                const bounds = envelope(observation);
                if (
                  !bounds ||
                  observation.property_name.toLowerCase() !== filter.property_name.toLowerCase()
                ) {
                  return false;
                }
                return bounds[1] >= range[0] && bounds[0] <= range[1];
              }).length
          : null;
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
        ? intent.secondary.map((code) => termOf(INTENT_LABELS, code)).join(', ')
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
      period: periodText(plan.year_from, plan.year_to),
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
      const tool = knownTerm(RUN_TOOLS, observation.tool);
      return {
        key: `${observation.action_id}#${index}`,
        name: tool ?? RUN_STRINGS.planTool,
        toolCode: tool ? null : observation.tool,
        status: observation.status,
        statusLabel: knownTerm(RUN_OUTCOMES, observation.status) ?? RUN_STRINGS.noOutcome,
        summary: observation.summary,
        findings: observation.finding_ids?.length ?? 0,
        nodes: observation.graph_node_ids?.length ?? 0,
      };
    }),
  );

  // Стадия видна, когда на неё пришли события или когда у неё есть данные.
  // До ответа стадии остаются в каркасе с честной подписью «придёт с ответом»:
  // по ним аналитик читает ход сборки.
  const stageBlocks = $derived.by(() => {
    const waiting = !answer;
    return RUN_STAGES.map((stage) => {
      const items = stepsFor(stage.key);
      const data =
        stage.key === 'question'
          ? intentView !== null || planView !== null
          : stage.key === 'search'
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

  // Набранный вопрос из поля не убирается: он нужен, если сборка не удалась, и
  // поле очищает только пришедший ответ (см. $effect выше).
  function ask(value: string): void {
    const trimmed = value.trim();
    if (trimmed.length < 3 || running || !canAsk) return;
    lastAsked = trimmed;
    correctionFor = null;
    feedbackVerdict = null;
    onnotice(null);
    onask(trimmed);
  }

  function showMoreTheses(): void {
    thesesLimit += THESES_PAGE;
  }

  // Открытие разбора: тезис остаётся выбранным (подсветка шкалы и карточки),
  // а полный разбор с цитатами, местом в источнике и историей уходит в шторку.
  function openClaim(id: string): void {
    onfocus(null);
    onselect(id);
    sheetClaimId = id;
  }

  function closeClaim(): void {
    // Esc закрывает и шторку, и раскрытую цитату сразу: сначала сворачиваем
    // цитату, иначе одно нажатие отнимает весь разбор.
    if (focusKey) {
      onfocus(null);
      return;
    }
    sheetClaimId = null;
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
            : 'Проверьте соединение и отправьте правку снова: текст сохранён.',
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
            : 'Проверьте соединение и отправьте отзыв снова: текст сохранён.',
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

<!-- Правила чтения одного набора: они же на пустом экране, они же в ответе.
     Разделённое состояние раскрытия — подсказка нужна ровно один раз на экран. -->
{#snippet readingGuide(id: string)}
  <div class="acc ask__guide">
    <button
      class="acc__head"
      type="button"
      aria-expanded={guideOpen}
      aria-controls={id}
      onclick={() => (guideOpen = !guideOpen)}
    >
      <span>{READING_GUIDE.title}</span>
      <Icon name="plus" size={16} class="acc__icon" />
    </button>
    <div id={id} class="acc__body" hidden={!guideOpen}>
      <ul class="paper__guide-list">
        {#each READING_GUIDE.items as item (item)}
          <li>{item}</li>
        {/each}
      </ul>
    </div>
  </div>
{/snippet}

<div class="ask">
  <div class="ask__flow" bind:this={flow}>
    <!-- ── 1. СОСТОЯНИЯ ДО ОТВЕТА ────────────────────────────────────────── -->
    {#if busy === 'import'}
      <p class="micro ask__importing" role="status" aria-live="polite">
        {ASK_ACTIONS.importingNote}
      </p>
    {/if}

    {#if !showPaper && !running && !error}
      {#if corpusEmpty === true}
        <Panel tone="sunk">
          <div class="case">
            <Notice tone="warn" title={ASK_EMPTY.corpusEmptyTitle}>
              <p>{ASK_EMPTY.corpusEmptyBody}</p>
              <p class="micro">{ASK_EMPTY.corpusEmptyNext}</p>
            </Notice>
            <div class="row">
              <label class="btn btn--action btn--sm upload">
                <Icon name="upload" size={16} />
                {busy === 'import' ? ASK_ACTIONS.importing : ASK_ACTIONS.attach}
                <input
                  type="file"
                  accept=".pdf,.docx,.xlsx,.json,.txt"
                  disabled={busy === 'import'}
                  onchange={pickFile}
                />
              </label>
              <Button href="/dashboard" variant="quiet" size="sm" icon="gauge">
                {navLabel('/dashboard')}
              </Button>
            </div>
          </div>
        </Panel>
      {:else}
        <!-- Одно главное действие: вопрос в поле композера ниже. Готовые
             вопросы и файл корпуса второстепенны: они помогают вопросу, а не
             спорят с ним за внимание, поэтому у пустого состояния нет своей
             крупной кнопки. -->
        <Empty icon="compass" title={ASK_EMPTY.title} body={ASK_EMPTY.body}>
          {#snippet action()}
            <div class="case__actions">
              {#if corpusEmpty === false}
                <p class="micro">{ASK_EMPTY.corpusReading}</p>
              {:else if corpusState === 'loading'}
                <p class="micro">{ASK_EMPTY.corpusLoading}</p>
              {:else if corpusState === 'error'}
                <p class="micro">{ASK_EMPTY.corpusError}</p>
              {/if}
              {#if examplesState === 'loading'}
                <p class="micro">{ASK_EMPTY.examplesLoading}</p>
              {:else if examplesState === 'error'}
                <Notice tone="error" title={ASK_EMPTY.examplesError}>
                  <p>{ASK_EMPTY.examplesErrorHint}</p>
                  <div class="row">
                    <Button variant="quiet" size="sm" icon="refresh" onclick={onexamples}>
                      {ASK_EMPTY.examplesRetry}
                    </Button>
                  </div>
                </Notice>
              {:else if examples.length}
                <p class="micro case__label">{ASK_EMPTY.examplesLabel}</p>
                <div class="examples">
                  {#each examples as example (example)}
                    <button class="example" type="button" onclick={() => ask(example)}>
                      {example}
                    </button>
                  {/each}
                </div>
              {:else}
                <p class="micro">{ASK_EMPTY.examplesNone}</p>
              {/if}
              <!-- Тот же «Как читать ответ», что и в ответе: новичок обязан
                   увидеть правила до первого ответа, а не после него. -->
              {@render readingGuide('guide-empty')}
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
          <p class="micro">{error.recovery}</p>
          {#if staleAnswer && answer}
            <!-- Сбой не притворяется пустотой, а пустота не притворяется сбоем:
                 прежний ответ остаётся на экране и назван как прежний. -->
            <p class="micro">{ANSWER_HEAD.stale.replace('{question}', `«${answer.question}»`)}</p>
          {/if}
          {#if error.question}
            <div class="row">
              <Button variant="ink" size="sm" icon="refresh" onclick={() => ask(error.question)}>
                {ASK_ACTIONS.retry}
              </Button>
              {#if error.login}
                <!-- По гайдлайну 401 вход возвращает на этот же экран, а не на
                     страницу по умолчанию: вопрос здесь остался нетронутым. -->
                <a class="small" href={`/login?next=${encodeURIComponent(page.url.pathname)}`}
                  >Войти заново</a
                >
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

    <!-- ── 2. КАК СОБИРАЛСЯ ОТВЕТ: стадии на утопленном листе ────────────── -->
    {#if running || trailSteps.length}
      <Panel tone="sunk">
        <section class="trail" aria-labelledby="trail-title">
          <div class="trail__head">
            <div class="trail__title">
              <h2 class="h4" id="trail-title">
                {#if running}
                  <Mascot mood={answerArriving ? 'say' : 'think'} size={32} label={ASK_TRAIL.running} />
                  {answerArriving ? ASK_TRAIL.arriving : ASK_TRAIL.running}
                {:else}
                  {ASK_TRAIL.title}
                {/if}
              </h2>
              <p class="micro">
                {#if stageStrip.length}
                  {#each stageStrip as stage (stage.label)}
                    <span class="tag"><span>{stage.label}</span><b class="num">{stage.count}</b></span>
                  {/each}
                {:else}
                  <span class="tag">{ASK_TRAIL.noSteps}</span>
                {/if}
              </p>
            </div>
            <div class="trail__meter">
              <!-- Отсчёт живёт только пока идёт сборка: после неё секундомер —
                  телеметрия, и она ушла под «Почему ответу можно верить». -->
              {#if running}<span class="trail__elapsed">{duration(elapsedMs)}</span>{/if}
              <!-- Живая область вне сворачиваемого тела: объявление доходит и
                   тогда, когда след закрыт (так он закрыт по умолчанию).
                   Внутри — только счётчик шагов: тикающие каждую секунду
                   сотые доли объявляли бы себя каждые 100 мс. -->
              <p class="micro trail__count" role="status" aria-live="polite" aria-atomic="true">
                {ASK_TRAIL.stepsGathered} {countOf(trailSteps.length, 'шаг', 'шага', 'шагов')}
              </p>
              {#if running}
                <!-- Слово «идёт» держит признак живого отсчёта: сами сотые доли
                     читают глазами, и две строки с одним числом не нужны. -->
                <span class="micro trail__tick">{ASK_TRAIL.live}</span>
              {/if}
              <Button
                variant="quiet"
                size="sm"
                class="trail__toggle"
                expanded={trailOpen}
                controls="trail-body"
                onclick={() => (trailUser = !trailOpen)}
              >
                {trailOpen ? ASK_TRAIL.collapse : ASK_TRAIL.expand}
              </Button>
            </div>
          </div>

          <div id="trail-body" class="trail__body" hidden={!trailOpen}>
            <p class="micro trail__honest">
              <Icon name="info" size={14} />
              {ASK_TRAIL.honest}
            </p>

            {#if running && !trailSteps.length}
              <p class="trail__waiting">
                {ASK_TRAIL.waiting}
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
                      {@const phrase = step.message ? tracePhraseOf(step.message) : ''}
                      <li class="step" data-state={step.status ?? 'unknown'}>
                        <span class="step__idx num">{String(index + 1).padStart(2, '0')}</span>
                        <span class="step__body">
                          <span class="step__msg">{phrase || RUN_STRINGS.stepFallback}</span>
                        </span>
                        <!-- Служебное имя узла и длительность шага лежат под
                             «Служебными данными» ответа: здесь читается ход
                             сборки, а не журнал. -->
                        <StatusPill
                          status={stepPill(step.status ?? 'completed')}
                          label={step.status
                            ? STEP_STATE_LABELS[step.status]
                            : RUN_STRINGS.stepStateFallback}
                        />
                      </li>
                    {/each}
                  </ol>
                {:else}
                  <p class="micro">{ASK_TRAIL.stageNoSteps}</p>
                {/if}

                {#if block.key === 'question'}
                  {#if intentView || planView}
                    {#if intentView}
                      <dl class="kv stage__kv">
                        <dt>{ASK_TRAIL.intentPrimary}</dt>
                        <dd>{intentView.label}</dd>
                        <dt>{ASK_TRAIL.intentSecondary}</dt>
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
                          <span class="chip chip--static">{RUN_STRINGS.noEntities}</span>
                        {/if}
                        {#each planView.countries as country (country)}
                          <span class="chip chip--static"><Icon name="pin" size={13} />{country}</span>
                        {/each}
                      </div>
                      <!-- Глубину поиска и число условий вопроса видно под
                           «Почему ответу можно верить»: это показатели контура.
                           Период остаётся — его человек задаёт сам. -->
                      <dl class="kv stage__kv">
                        <dt>{ASK_TRAIL.period}</dt>
                        <dd class="num">{planView.period}</dd>
                      </dl>
                      {#if filterRows.length}
                        <div class="table-wrap" role="region" aria-label={ASK_TRAIL.filtersTitle} use:scrollRegion>
                          <table class="table">
                            <caption class="micro">{ASK_TRAIL.filtersTitle}</caption>
                            <thead>
                              <tr>
                                <th>{ASK_TRAIL.colProperty}</th>
                                <th>{ASK_TRAIL.colCondition}</th>
                                <th>{ASK_TRAIL.colValue}</th>
                                <th>{ASK_TRAIL.colCoverage}</th>
                              </tr>
                            </thead>
                            <tbody>
                              {#each filterRows as row (row.filter.property_name + row.filter.operator)}
                                {@const property = propertyTerm(row.filter.property_name)}
                                {@const isRange =
                                  row.filter.operator === 'between' || row.filter.operator === 'range'}
                                <tr class:no-coverage={row.covered === 0}>
                                  <!-- Русское имя свойства; ключ живёт в «Служебных данных». -->
                                  <td>{property.label}</td>
                                  <td>
                                    {labelOf(row.filter.operator)}
                                    {#if !isRange}<span class="num">{signOf(row.filter.operator)}</span>{/if}
                                  </td>
                                  <td class="num">{filterText(row.filter)}</td>
                                  <td class="num">
                                    {#if row.covered != null}
                                      {row.covered}
                                      {plural(row.covered, 'наблюдение', 'наблюдения', 'наблюдений')}
                                    {:else}
                                      нет
                                    {/if}
                                  </td>
                                </tr>
                              {/each}
                            </tbody>
                          </table>
                        </div>
                        <p class="micro">{RUN_STRINGS.coverageNote}</p>
                      {/if}
                    {/if}
                  {:else}
                    <p class="micro">{ASK_TRAIL.noPlan}</p>
                  {/if}
                {/if}

                {#if block.key === 'search'}
                  {#if obsView.length}
                    <div class="stage__obs">
                      {#each obsView as observation (observation.key)}
                        <div class="obs">
                          <div class="obs__head">
                            <span class="small">{observation.name}</span>
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
                          <!-- Счётчики утверждений и узлов это телеметрия сборки:
                              она ушла под «Служебные данные» ответа. -->
                        </div>
                      {/each}
                    </div>
                  {:else if !answer}
                    <p class="micro">{ASK_TRAIL.noObserve}</p>
                  {/if}
                {/if}
              </div>
            {/each}
          </div>
        </section>
      </Panel>
    {/if}

    <!-- ── 3. ОТВЕТ = БУМАГА ─────────────────────────────────────────────── -->
    {#if showPaper && answer}
      {@const payload = answer}
      <article class="paper">
        <header class="paper__head">
          {#if staleAnswer}
            <!-- Новый вопрос ещё без ответа: прежний не стирается молча, он
                 назван прежним и остаётся до явного действия человека. -->
            <div class="paper__stale">
              <Notice tone="info" title={ANSWER_HEAD.staleTitle}>
                <p>{ANSWER_HEAD.stale.replace('{question}', `«${answer.question}»`)}</p>
              </Notice>
              <Button variant="quiet" size="sm" onclick={() => (staleDismissed = true)}>
                {ANSWER_HEAD.discard}
              </Button>
            </div>
          {/if}
          <p class="eyebrow">
            <Icon name="doc" size={16} />
            {ANSWER_HEAD.eyebrow}
          </p>
          <h2 class="h3">{payload.question || question}</h2>
          <!-- Режим сборки меняет доверие к ответу: ответ без модели читается
               иначе, чем ответ модели. -->
          {#if payload.model_mode !== 'gigachat'}
            <p class="micro paper__mode">
              <Icon name="alert" size={14} />
              {MODEL_MODE_LABELS[payload.model_mode]}
            </p>
          {/if}
          <div class="prose">
            <p class="summary">{payload.summary || ANSWER_HEAD.summaryMissing}</p>
          </div>
          <div class="paper__bar">
            <dl class="kv paper__kv">
              <dt>{ANSWER_HEAD.thesesLabel}</dt>
              <dd class="num">{payload.findings.length}</dd>
              <dt>{ANSWER_HEAD.linksLabel}</dt>
              <dd class="num">{evidenceTotal}</dd>
              <dt>{ANSWER_HEAD.divergencesLabel}</dt>
              <dd class="num">{payload.conflicts.length}</dd>
              <dt>{ANSWER_HEAD.gapsLabel}</dt>
              <dd class="num">{payload.knowledge_gaps.length}</dd>
            </dl>
            {#if canExport}
              <div class="paper__export">
                <span class="micro">{ANSWER_HEAD.exportLabel}</span>
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
          </div>
          <!-- Правила чтения одинаковы на пустом экране и в ответе; порядок
               чтения важнее пояснений, поэтому в ответе они свёрнуты. -->
          {@render readingGuide('guide-answer')}
          <!-- Телеметрия сборки живёт под тем же раскрытием, что объясняет
               доверие: в тексте ответа ей не место. -->
          <div class="acc paper__trust">
            <button
              class="acc__head"
              type="button"
              aria-expanded={trustOpen}
              aria-controls="paper-trust"
              onclick={() => (trustOpen = !trustOpen)}
            >
              <span>{TRUST_PANEL.title}</span>
              <Icon name="plus" size={16} class="acc__icon" />
            </button>
            <div id="paper-trust" class="acc__body" hidden={!trustOpen}>
              <p class="micro">{TRUST_PANEL.hint}</p>
              <dl class="kv paper__kv trust__kv">
                {#each trustRows as row (row.label)}
                  <dt>{row.label}</dt>
                  <dd>
                    {row.value}
                    {#if row.note}<span class="micro trust__note">{row.note}</span>{/if}
                  </dd>
                {/each}
              </dl>
            </div>
          </div>
        </header>

        {#if payload.degradation_reasons.length}
          <!-- Неполный ответ назван причиной, а не молчаливой обрезкой: сырая
               строка сервиса ушла в «Служебные данные» в конце ответа. -->
          <div class="paper__degraded">
            <Notice tone="warn" title={DEGRADED.title}>
              {#each payload.degradation_reasons as reason (reason)}
                <p>{degradationOf(reason)}</p>
              {/each}
            </Notice>
            <p class="micro">{DEGRADED.hint}</p>
          </div>
        {/if}

        {#if !payload.findings.length}
          <div class="paper__empty">
            <Empty icon="search" title={ASK_NO_MATCH.title} body={ASK_NO_MATCH.body}>
              {#snippet action()}
                <div class="case__actions">
                  {#if payload.knowledge_gaps.length}
                    <p class="micro case__label">{ASK_NO_MATCH.gapsLabel}</p>
                    {#each payload.knowledge_gaps.slice(0, 3) as gap, position (position)}
                      <p class="small gap">{gap}</p>
                    {/each}
                  {/if}
                  <p class="micro">{ASK_NO_MATCH.hint}</p>
                  {#if payload.conflicts.length || payload.recommendations.length}
                    <p class="micro">{ASK_NO_MATCH.aside}</p>
                  {/if}
                </div>
              {/snippet}
            </Empty>
          </div>
        {:else}
          <div class="paper__body">
            <!-- Шкала интервалов: клавиатурный путь к тому же содержимому,
                 что и список тезисов, и общий масштаб по единице. -->
            <nav class="scale" bind:this={rail} aria-label={SCALE_LABELS.aria}>
              <div class="scale__head">
                <h3 class="small">{SCALE_LABELS.title}</h3>
                <span class="micro">
                  {countOf(payload.findings.length, 'тезис', 'тезиса', 'тезисов')}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  class="scale__toggle"
                  expanded={scaleOpen}
                  controls="scale-body"
                  onclick={() => (scaleUser = !scaleOpen)}
                >
                  {scaleOpen ? SCALE_LABELS.collapse : SCALE_LABELS.expand}
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
                          <span>{SCALE_LABELS.noNumbers}</span>
                        {/if}
                      </p>
                      {#each group.rows as row (row.item.id)}
                        {@const band = bandOf(row, group)}
                        <button
                          class="tick"
                          type="button"
                          tabindex="0"
                          data-tick={row.item.id}
                          aria-current={row.item.id === openClaimId ? 'true' : undefined}
                          onkeydown={onRowKeys}
                          onclick={() => openClaim(row.item.id)}
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
                            <span class="micro tick__meta">
                              {#if row.bounds}
                                <b class="num">{num(row.bounds[0])}–{num(row.bounds[1])} {group.unit}</b>
                              {:else}
                                <b>{SCALE_LABELS.withoutNumber}</b>
                              {/if}
                              <!-- Ссылки на фрагменты: одна формулировка со
                                   словаря, число склоняется в ней же. -->
                              <span>{linksToText(row.item.evidence.length)}</span>
                            </span>
                          </span>
                        </button>
                      {/each}
                    </div>
                  {/each}
                  {#if findingsHidden > 0}
                    <p class="micro">{thesesShown(visibleFindings.length, payload.findings.length)}</p>
                  {/if}
                  <p class="micro">{SCALE_LABELS.unitNote}</p>
                {:else}
                  <p class="micro">{SCALE_LABELS.noBands}</p>
                {/if}
              </div>
            </nav>

            <div class="theses">
              {#each visibleFindings as finding (finding.id)}
                {@const selected = finding.id === openClaimId}
                {@const subject = subjectTerm(finding)}
                {@const predicate = predicateTerm(finding)}
                <article class="thesis" class:thesis--selected={selected} data-claim={finding.id}>
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
                      <span class="tag">
                        {SHEET_LABELS.versionLabel}
                        <b class="num">{finding.version}</b>
                      </span>
                      {#if finding.superseded_by}
                        <span class="tag">{STATUS_SUPERSEDED}</span>
                      {/if}
                    </div>
                    <h3 class="thesis__statement">{finding.statement}</h3>
                    <!-- Только русские имена: ключи онтологии живут в разборе
                         тезиса под «Служебными данными». -->
                    <p class="micro thesis__spo">
                      <span class="thesis__spo-subject">{subject.label}</span>
                      <span>{predicate.label}</span>
                      <span>{scopeText(finding.scope)}</span>
                    </p>
                    <Button
                      variant="quiet"
                      size="sm"
                      class="thesis__toggle"
                      onclick={() => openClaim(finding.id)}
                    >
                      {SHEET_LABELS.open}
                    </Button>
                  </div>

                  <p class="micro thesis__closed">
                    <span>{valueSummary(finding)}</span>
                    <span>
                      {linksToText(finding.evidence.length)}: цитаты, место в источнике и версии
                      показывает разбор
                    </span>
                  </p>
                </article>
              {/each}
              {#if findingsHidden > 0}
                <Button variant="quiet" size="sm" icon="chevronDown" onclick={showMoreTheses}>
                  {moreThesesText(findingsHidden)}
                </Button>
              {/if}
            </div>
          </div>
        {/if}

        {#if payload.conflicts.length || payload.knowledge_gaps.length || payload.recommendations.length}
          <section class="block">
            <div class="block__head">
              <h3 class="h4">{ASK_REVIEW.title}</h3>
              <p class="micro">{ASK_REVIEW.note}</p>
            </div>
            {#each payload.conflicts as item, index (index)}
              <div class="line">
                <span class="tag">{ASK_REVIEW.conflictTag} {String(index + 1).padStart(2, '0')}</span>
                <p class="small">{item}</p>
              </div>
            {/each}
            {#each payload.knowledge_gaps as item, index (index)}
              <div class="line">
                <span class="tag">{ASK_REVIEW.gapTag} {String(index + 1).padStart(2, '0')}</span>
                <p class="small">{item}</p>
              </div>
            {/each}
            {#each payload.recommendations as item, index (index)}
              <div class="line">
                <span class="tag">{ASK_REVIEW.adviceTag} {String(index + 1).padStart(2, '0')}</span>
                <p class="small">{item}</p>
              </div>
            {/each}
            <div class="row">
              <Button href="/conflicts" variant="quiet" size="sm" icon="conflict">
                Раздел «{navLabel('/conflicts')}»
              </Button>
            </div>
          </section>
        {/if}

        {#if canFeedback}
          <section class="block">
            <div class="block__head">
              <h3 class="h4">{ASK_FEEDBACK.title}</h3>
              <p class="micro">{ASK_FEEDBACK.note}</p>
            </div>
            <div class="verdicts">
              <span class="micro">{ASK_FEEDBACK.verdictLabel}</span>
              <Chip pressed={feedbackVerdict === 'accept'} onclick={() => (feedbackVerdict = 'accept')}>
                {ASK_FEEDBACK.useful}
              </Chip>
              <Chip pressed={feedbackVerdict === 'reject'} onclick={() => (feedbackVerdict = 'reject')}>
                {ASK_FEEDBACK.distrust}
              </Chip>
            </div>
            <Field
              label={ASK_FEEDBACK.commentLabel}
              name="answer-feedback"
              type="textarea"
              rows={2}
              placeholder={ASK_FEEDBACK.commentPlaceholder}
              hint={ASK_FEEDBACK.commentHint}
              bind:value={feedbackComment}
            />
            {#if !feedbackVerdict}
              <p class="micro block__note">{ASK_FEEDBACK.needVerdict}</p>
            {/if}
            <Button
              variant="action"
              size="sm"
              busy={busy === 'feedback'}
              disabled={busy === 'feedback' || !feedbackVerdict || feedbackComment.trim().length < 3}
              onclick={() => void sendFeedback()}
            >
              {ASK_FEEDBACK.send}
            </Button>
          </section>
        {:else}
          <p class="micro">{ASK_FEEDBACK.noRight}</p>
        {/if}

        {#if serviceRows.length}
          <!-- Служебные данные ответа: одно раскрытие на весь ответ. -->
          <details class="svc paper__service">
            <summary class="micro">{TRUST_PANEL.serviceTitle}</summary>
            <p class="micro">{TRUST_PANEL.serviceHint}</p>
            <ul class="svc__list">
              {#each serviceRows as row (row.label)}
                <li>
                  <span>{row.label}</span>
                  <code class="code tech">{row.value}</code>
                </li>
              {/each}
            </ul>
          </details>
        {/if}
      </article>
    {/if}
  </div>

  <!-- ── 4. КОМПОЗЕР: прилип ко дну экрана, маскот показывает состояние сборки -->
  <div class="ask__composer">
    <!-- Показания корпуса, остановку сборки и загрузку документа вынесли своей
         строкой над полем: ряд инструментов композера на узком экране уходит в
         скрытый горизонтальный скролл, а эти действия обязательные. -->
    <div class="ask__meta">
      {#if corpusState === 'loading'}
        <span class="tag ask__corpus-note">{ASK_EMPTY.corpusLoading}</span>
      {:else if !corpus}
        <span class="tag ask__corpus-note">{ASK_EMPTY.corpusError}</span>
      {:else}
        <dl class="ask__corpus">
          <div class="ask__corpus-item">
            <dt>документов</dt>
            <dd class="num">{corpus.documents}</dd>
          </div>
          <div class="ask__corpus-item">
            <dt>утверждений</dt>
            <dd class="num">{corpus.claims}</dd>
          </div>
          <div class="ask__corpus-item">
            <dt>фрагментов</dt>
            <dd class="num">{corpus.chunks}</dd>
          </div>
          <div class="ask__corpus-item">
            <dt>сущностей</dt>
            <dd class="num">{corpus.entities}</dd>
          </div>
        </dl>
      {/if}
      {#if running}
        <Button variant="quiet" size="sm" icon="close" onclick={onstop}>{ASK_ACTIONS.stopRun}</Button>
      {/if}
      <label class="btn btn--sm btn--quiet upload ask__attach">
        <Icon name="upload" size={16} />
        {busy === 'import' ? ASK_ACTIONS.importing : ASK_ACTIONS.attach}
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
      label="Вопрос к корпусу"
      placeholder="Какие методы обессоливания подходят при сульфатах и хлоридах 200–300 мг/л?"
      busy={running}
      disabled={!canAsk}
      hint={draft && !draftValid
        ? 'минимум три символа'
        : 'Enter отправляет вопрос, Shift + Enter добавляет строку'}
      onsubmit={ask}
    />

    {#if draft && !draftValid}
      <p class="field__error ask__note">
        Вопрос короче трёх символов: добавьте формулировку, иначе ответа не будет.
      </p>
    {/if}
  </div>
</div>

{#if sheetFinding}
  <Sheet
    title={SHEET_LABELS.title}
    description={SHEET_LABELS.description}
    width="840px"
    onclose={closeClaim}
  >
    {#snippet footer()}
      <Button variant="quiet" onclick={closeClaim}>{SHEET_LABELS.close}</Button>
    {/snippet}
    {@render thesisTrace(sheetFinding)}
  </Sheet>
{/if}

{#snippet thesisTrace(finding: Finding)}
  <div class="sheet__thesis">
    <div class="thesis__marks">
      <StatusPill status={finding.status} label={STATUS_PHRASE[finding.status]} />
      <span class="tag">{DATA_CLASS_LABELS[finding.data_class]}</span>
      <span class="tag">{SHEET_LABELS.versionLabel} <b class="num">{finding.version}</b></span>
    </div>
    <h3 class="h4">{finding.statement}</h3>
    <p class="micro muted sheet__facts">
      <span>{sourceOf(finding)}</span>
      <span>{scopeText(finding.scope)}</span>
      <span>{linksToText(finding.evidence.length)}</span>
    </p>
  </div>
  {@const rivals = rivalsOf(finding)}
  {#if finding.observations.length}
    <div class="measures">
      <p class="micro measures__label">{SHEET_LABELS.measures}</p>
      {#each measuresOf(finding) as measure (measure.key)}
        <div class="measure">
          <div class="measure__top">
            <span class="micro">
              {measure.property}
              {measure.operatorLabel}
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
            <span>
              {SHEET_LABELS.scale}
              <span class="num">{measure.scaleFrom}–{measure.scaleTo} {measure.normalizedUnit}</span>
            </span>
            <span>
              {SHEET_LABELS.normalized}
              <span class="num">{measure.normalizedText} {measure.normalizedUnit}</span>
            </span>
          </p>
          {#if measure.limits.length}
            <p class="micro measure__limits">
              {#each measure.limits as limit, position (position)}
                <span class="tag num">{limit.label}</span>
              {/each}
            </p>
          {/if}
          <p class="micro">{SHEET_LABELS.source}: «{measure.raw}»</p>
          {#if measure.filterNote}
            <p class="micro measure__filter" class:outside={measure.outsideFilter}>
              {measure.filterNote}
              {#if measure.outsideFilter}<span>{SHEET_LABELS.outsideFilter}</span>{/if}
            </p>
          {/if}
        </div>
      {/each}
    </div>
  {:else}
    <p class="thesis__bare">{SHEET_LABELS.bare}</p>
  {/if}

  {#if rivals.length}
    <div class="rivals">
      <p class="micro rivals__label">
        <span>{SHEET_LABELS.rivalsLabel}</span>
        <span>{subjectTerm(finding).label}</span>
        <span>{predicateTerm(finding).label}</span>
      </p>
      <div class="rivals__row" data-self>
        <span class="small">{sourceOf(finding)}, {SHEET_LABELS.rivalsSelf}</span>
        <span class="num">{valueSummary(finding)}</span>
      </div>
      {#each rivals as rival (rival.finding.id)}
        <div class="rivals__row">
          <span class="small">{sourceOf(rival.finding)}</span>
          <span class="num">{valueSummary(rival.finding)}</span>
        </div>
        <p class="micro">
          {SHEET_LABELS.rivalsDelta}
          <b class="num">{num(rival.delta)}</b>
          {finding.observations[0]?.normalized_unit ?? ''}
        </p>
      {/each}
      <p class="micro">{SHEET_LABELS.rivalsNote}</p>
      <div class="row">
        <Button href="/conflicts" variant="quiet" size="sm" icon="conflict">
          Раздел «{navLabel('/conflicts')}»
        </Button>
      </div>
    </div>
  {/if}

  <!-- Каждое доказательство раскрывается с клавиатуры -->
  <div class="trace">
    <p class="micro trace__label">
      {SHEET_LABELS.traceLabel}, {linksToText(finding.evidence.length)}
    </p>
    {#if !finding.evidence.length}
      <Notice tone="error" title={SHEET_LABELS.untrackedTitle}>
        <p>{SHEET_LABELS.untrackedBody}</p>
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
              {#each locParts(evidence) as part (part)}
                <span>{part}</span>
              {/each}
            </span>
            <span class="micro">{expanded ? ASK_SOURCE.closeQuote : ASK_SOURCE.openQuote}</span>
          </button>
          <div
            class="evidence__body"
            id={`body-${itemKey.replace('#', '-')}`}
            hidden={!expanded}
          >
            <p class="quote">«{evidence.quote}»</p>
            <dl class="kv evidence__kv">
              <dt>{SHEET_LABELS.documentLabel}</dt>
              <dd>{evidence.source_title || 'запись корпуса'}</dd>
              {#if evidence.page != null}
                <dt>{SHEET_LABELS.page}</dt><dd class="num">{evidence.page}</dd>
              {/if}
              {#if evidence.sheet}
                <dt>{SHEET_LABELS.sheet}</dt><dd>{evidence.sheet}</dd>
              {/if}
              {#if evidence.cell_range}
                <dt>{SHEET_LABELS.cells}</dt><dd class="num">{evidence.cell_range}</dd>
              {/if}
            </dl>
            <div class="row evidence__links">
              <!-- Ровно две ссылки под доказательством: к находке с её
                   источником и к тому же узлу на карте связей. -->
              <a class="small" href={findingsHref(finding)}>{ASK_SOURCE.findings}</a>
              <a class="small" href={graphHref(finding)}>{ASK_SOURCE.graph}</a>
              <span class="micro">{ASK_SOURCE.esc}</span>
            </div>
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
            hint="Минимум три символа: иначе правку не принять."
            bind:value={correctionComment}
          />
          <Field
            label="Исправленный тезис"
            name={`correction-text-${finding.id}`}
            type="textarea"
            rows={2}
            hint="Минимум три символа: текст станет новой версией тезиса."
            bind:value={correctionText}
          />
          <Notice tone="info" title="Правка относится к этому тезису">
            <p>
              Она создаёт новую версию тезиса и оставляет прежнюю в истории связей,
              ответ целиком не меняется.
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
          Исправить этот тезис
        </Button>
      {/if}
    </div>
  {:else if canFeedback}
    <p class="micro thesis__note">
      Исправление тезиса доступно эксперту, право выдаёт администратор сервиса.
      Отзыв по ответу целиком находится ниже.
    </p>
  {/if}

  <!-- История версий открытого тезиса: состояние своё у каждого тезиса,
       отказ тоже свой. -->
  {@const hist = historyState(finding.id)}
  <div class="history">
    <p class="micro history__label">{SHEET_LABELS.historyLabel}</p>
    <div class="row">
      <Button
        variant="quiet"
        size="sm"
        icon="clock"
        busy={hist.busy}
        disabled={hist.busy}
        onclick={() => void loadHistory(finding.id)}
      >
        {hist.busy ? SHEET_LABELS.historyBusy : hist.data ? SHEET_LABELS.historyRefresh : SHEET_LABELS.historyAsk}
      </Button>
    </div>
    {#if hist.error}
      <Notice tone="error" title={SHEET_LABELS.historyError}>
        <p>{hist.error}</p>
        <div class="row">
          <Button
            variant="quiet"
            size="sm"
            icon="refresh"
            onclick={() => void loadHistory(finding.id)}
          >
            {SHEET_LABELS.historyRetry}
          </Button>
        </div>
      </Notice>
    {:else if hist.data}
      {#if !hist.data.versions.length}
        <p class="micro">{SHEET_LABELS.historySingle}</p>
      {:else}
        {#each hist.data.versions as version (version.finding_id + version.version)}
          <div class="version">
            <p class="small">{version.statement}</p>
            <p class="micro version__meta">
              <span>{SHEET_LABELS.versionLabel} <b class="num">{version.version}</b></span>
              <StatusPill
                status={version.status}
                label={STATUS_PHRASE[version.status]}
              />
              {#if version.review_date}
                <time datetime={version.review_date}>{reviewDate(version.review_date)}</time>
              {:else}
                <span>{SHEET_LABELS.reviewNone}</span>
              {/if}
            </p>
            {#if version.review_reason}<p class="micro">{version.review_reason}</p>{/if}
          </div>
        {/each}
      {/if}
    {/if}
  </div>

  <!-- Служебные данные разбора: адрес тезиса, символы цитат и идентификаторы. -->
  <details class="svc sheet__service">
    <summary class="micro">{TRUST_PANEL.serviceTitle}</summary>
    <p class="micro">{SHEET_LABELS.serviceCodes}</p>
    <ul class="svc__list">
      {#each sheetServiceRows(finding) as row (row.label)}
        <li>
          <span>{row.label}</span>
          <code class="code tech">{row.value}</code>
        </li>
      {/each}
    </ul>
  </details>
{/snippet}

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

  /* Ряд над полем: показания корпуса, остановку сборки и загрузку документа
     переносит, а не уходит в скрытый скролл. */
  .ask__meta {
    display: flex;
    align-items: center;
    gap: var(--s2);
    flex-wrap: wrap;
    padding-inline: var(--s4);
  }

  /* Каждый счётчик корпуса подписан своей строкой: числа не склеиваются в одну
     подпись делимитером. */
  .ask__corpus {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    flex-wrap: wrap;
    min-width: 0;
    margin: 0;
    padding: 2px var(--s3);
    border-radius: var(--r-pill);
    background: var(--surface-sunk);
    font-size: var(--t-micro);
  }

  .ask__corpus-item {
    display: inline-flex;
    align-items: baseline;
    gap: var(--s1);
  }

  .ask__corpus dt {
    color: var(--ink-3);
  }

  .ask__corpus dd {
    margin: 0;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  .ask__corpus-note {
    color: var(--ink-3);
  }

  .ask__importing {
    margin: 0;
    padding-inline: var(--s4);
    color: var(--ink-2);
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

  .step__msg {
    font-size: var(--t-small);
    color: var(--ink-2);
    line-height: var(--lh-dense);
    overflow-wrap: anywhere;
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

  /* Служебное имя или идентификатор читаются подписью при человекочитаемом
     названии. Саму подпись задаёт `.tech` из `app.css`: локальное правило с
     `--ink-4` дублировало его и роняло контраст текста. */
  .paper__mode {
    display: flex;
    align-items: center;
    gap: var(--s2);
    max-width: var(--maxw-measure);
    color: var(--hypothesis);
  }

  /* Одно раскрытие служебных данных на весь ответ и на каждый разбор. */
  .svc {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    padding: var(--s3) var(--s4);
    border: 1px dashed var(--line-strong);
    border-radius: var(--r-md);
    background: var(--surface-sunk);
  }

  .svc summary {
    cursor: pointer;
    font-weight: 500;
    color: var(--ink-2);
  }

  .svc__list {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .svc__list li {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--s3);
    flex-wrap: wrap;
    font-size: var(--t-micro);
    color: var(--ink-3);
  }

  .paper__service,
  .sheet__service {
    margin-top: var(--s3);
  }

  .paper__body {
    display: grid;
    gap: var(--s5);
    /* Колонка шкалы относительная: на средних экранах 340px выталкивали бы
       тезисы, min() держит её пропорциональной ширине окна. */
    grid-template-columns: minmax(min(340px, 30vw), 380px) minmax(0, 1fr);
    align-items: start;
  }

  @media (max-width: 900px) {
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

  @media (max-width: 900px) {
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

  .thesis--selected {
    border-color: var(--line-strong);
    box-shadow: var(--shadow-soft);
  }

  /* Шапка разбора в шторке: тот же тезис, что и на листе, но в роли заголовка
     слоя — без карточки и без рамки. */
  .sheet__thesis {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
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

  /* Пока вердикт не выбран, подсказка стоит рядом с кнопкой, а не вместо неё. */
  .block__note {
    color: var(--ink-2);
  }

  /* Число уверенности и пояснение к нему — два элемента, а не склейка. */
  .trust__note {
    display: block;
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
