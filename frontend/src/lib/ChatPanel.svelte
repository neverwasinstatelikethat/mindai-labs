<script lang="ts">
  /**
   * Чат с ответом и проверяемыми источниками.
   * Компонент ничего не запрашивает сам: все вызовы живут в маршруте
   * `(app)/research` и приходят колбэками, а право на действие читается из
   * подтверждённой сессии (`session.can`), а не из выбора на клиенте.
   */
  import { tick } from 'svelte';
  import { page } from '$app/state';
  import { countOf, num, plural } from '$lib/format';
  import { navLabel } from '$lib/nav';
  import { boundsOf, gapBetween } from '$lib/numbers';
  import { session } from '$lib/sessionStore.svelte';
  import {
    ANSWER_HEAD,
    ASK_ACTIONS,
    ASK_EMPTY,
    ASK_NO_MATCH,
    ASK_SOURCE,
    ASK_VERDICT,
    DEGRADED,
    OPERATOR_SYMBOL,
    OPERATOR_WORD,
    PREDICATE_LABELS,
    PROPERTY_LABELS,
    SHEET_LABELS,
    STATUS_PHRASE,
    SUBJECT_LABELS,
    TERM_FALLBACKS,
    describeScope,
    describeValue,
    knownTerm,
    moreThesesText,
  } from '$lib/terms';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Mascot from '$lib/ui/Mascot.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import PromptInput from '$lib/ui/PromptInput.svelte';
  import Sheet from '$lib/ui/Sheet.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';
  import VerdictSheet from '$lib/ui/VerdictSheet.svelte';
  import type {
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
    error: RunFailure | null;
    openClaimId: string | null;
    focusKey: string | null;
    busy: 'feedback' | 'export' | 'import' | null;
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
    onexport: (format: 'markdown' | 'json-ld') => Promise<boolean>;
    onimport: (file: File) => Promise<boolean>;
    onnotice: (notice: SheetNotice | null) => void;
    onhistory: (claimId: string) => Promise<HistoryResult>;
  }

  const {
    question,
    answer,
    running,
    error,
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
    onexport,
    onimport,
    onnotice,
    onhistory,
  }: Props = $props();

  // ── Имена реальных значений контракта ─────────────────────────────────────
  // Словарь меток один и живёт в `terms.ts` (зона RS): стадии сборки ответа,
  // инструменты, исходы шага и перевод строк рабочего процесса. Узел потока
  // попадает в стадию через `stageOfNode`: узла нет в перечне, шаг идёт в
  // «Другие шаги» и не получает выдуманного имени.

  // ── Локальное состояние экрана ───────────────────────────────────────────

  let flow = $state<HTMLDivElement | null>(null);
  // Набранный вопрос живёт в поле до ответа: отказ не должен стоить набранного
  // текста, поэтому поле очищает только пришедший ответ (см. $effect ниже).
  let draft = $state('');
  let seeded = false;
  let lastAsked = '';
  // Правка тезиса — тот же контракт отзыва, что и вердикт по ответу: над
  // разбором открывается шторка вердикта с finding_id этого тезиса, и после
  // закрытия человек возвращается к тому же разбору.
  let thesisVerdict = $state<Finding | null>(null);
  // Вердикт по ответу открывается на месте: отдельного раздела отзыва нет.
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


  // Разбор тезиса — шторка (DESIGN.md называет это подписью продукта): открыта
  // максимум для одного тезиса. Выбор тезиса (подсветка в шкале и на карточке)
  // живёт отдельно в openClaimId, поэтому ответ прогона подсвечивает первый
  // тезис, но шторку сам не распахивает.
  let sheetClaimId = $state<string | null>(null);

  const canAsk = $derived(session.can('query:ask'));
  const canExport = $derived(session.can('export:run'));
  const canSupersede = $derived(session.can('restricted:read'));

  // Пока новый ответ собирается, предыдущий остаётся на экране; после замены
  // к нему можно вернуться из серверной истории чатов.
  const staleAnswer = $derived(
    !!answer && (answer.question ?? '').trim() !== (question ?? '').trim(),
  );
  let staleDismissed = $state(false);
  // Бумага на экране: ответ есть и он либо свежий, либо его не убрали.
  const showPaper = $derived(!!answer && !(staleAnswer && staleDismissed));

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

  // Новый ответ не наследует правки, вердикты и истории прежнего; с ним же
  // снимается и решение убрать прежний ответ с экрана.
  const runKey = $derived(`${answer?.query_id ?? 'нет'}|${running ? 'идёт' : 'стоп'}`);
  $effect(() => {
    void runKey;
    staleDismissed = false;
    thesisVerdict = null;
    histories = {};
    thesesLimit = THESES_PAGE;
  });

  const visibleFindings = $derived((answer?.findings ?? []).slice(0, thesesLimit));
  const findingsHidden = $derived((answer?.findings ?? []).length - visibleFindings.length);
  // ── Форматирование ──────────────────────────────────────────────────────

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
  ): { label: string } {
    if (!key) return { label: whenMissing };
    return { label: knownTerm(map, key) ?? whenMissing };
  }

  function propertyTerm(name: string): { label: string } {
    return termPair(PROPERTY_LABELS, name, TERM_FALLBACKS.property);
  }

  function subjectTerm(finding: Finding): { label: string } {
    return termPair(SUBJECT_LABELS, finding.subject, TERM_FALLBACKS.subject);
  }

  function predicateTerm(finding: Finding): { label: string } {
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
    return finding.evidence[0]?.source_title || 'исходный материал';
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
      ? `${evidence.source_title || 'исходный материал'}, ${place}`
      : evidence.source_title || 'исходный материал';
  }

  // Место в источнике человекочитаемыми словами.
  function locParts(evidence: Evidence): string[] {
    const parts: string[] = [];
    if (evidence.page != null) parts.push(`стр. ${evidence.page}`);
    if (evidence.sheet) parts.push(`лист ${evidence.sheet}`);
    if (evidence.cell_range) parts.push(`ячейки ${evidence.cell_range}`);
    return parts.length ? parts : [SHEET_LABELS.noLocator];
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

  // Коридор значения берётся из арифметики раздела «Числа»: два экрана не вправе
  // решать, какие числа считаются расхождением, по-разному. Парой здесь остаётся
  // только потому, что местная разметка оперирует парами.
  function envelope(observation: NumericObservation): [number, number] | null {
    const bounds = boundsOf(observation);
    return bounds === null ? null : [bounds.lo, bounds.hi];
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
      // Расхождение и его величина берутся из одного места: экран ответа и раздел
      // «Числа» обязаны называть одно и то же расхождение одним и тем же числом.
      const gap = gapBetween({ lo: own[0], hi: own[1] }, { lo: range[0], hi: range[1] });
      if (gap === null) continue;
      out.push({ finding: other, delta: gap });
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

  // Журнал шагов чтения корпуса на экран не выходит: имена инструментов, их
  // ── Фокус и прокрутка  // ── Фокус и прокрутка ───────────────────────────────────────────────────

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

  // ── Действия ────────────────────────────────────────────────────────────

  // Набранный вопрос из поля не убирается: он нужен, если сборка не удалась, и
  // поле очищает только пришедший ответ (см. $effect выше).
  function ask(value: string): void {
    const trimmed = value.trim();
    if (!trimmed || running || !canAsk) return;
    lastAsked = trimmed;
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

  // Правка тезиса не имеет своей формы: она открывает шторку вердикта, которая
  // отправляет отзыв с verdict='correct', finding_id и correction.
  function startCorrection(finding: Finding): void {
    onnotice(null);
    thesisVerdict = finding;
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

{#snippet composerTools()}
  <label class="btn btn--quiet btn--sm upload">
    <Icon name="doc" size={16} />
    Добавить файл
    <input type="file" accept=".pdf,.docx,.xlsx,.json,.txt" onchange={pickFile} />
  </label>
  {#if running}
    <Button variant="quiet" size="sm" onclick={onstop}>Остановить</Button>
  {/if}
{/snippet}

<!-- Правила чтения одного набора: они же на пустом экране, они же в ответе.
     Разделённое состояние раскрытия — подсказка нужна ровно один раз на экран. -->
<div class="ask" class:ask--empty={answer === null && !running}>
  <div class="ask__flow" bind:this={flow}>
    {#if busy === 'import'}
      <p class="micro ask__importing" role="status" aria-live="polite">
        {ASK_ACTIONS.importingNote}
      </p>
    {/if}

    {#if !showPaper && !running && !error}
      <div class="chat__welcome">
        <Mascot size={64} label="StormIdea" />
        <h2 class="h3">{ASK_EMPTY.title}</h2>
        <p class="small muted">Задайте вопрос по вашим материалам.</p>
      </div>
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

    {#if running}
      <p class="chat__loading" role="status">Ищу ответ в материалах…</p>
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
          <p class="chat__question">{payload.question || question}</p>
          <div class="prose">
            <p class="summary">{payload.summary || ANSWER_HEAD.summaryMissing}</p>
          </div>
          <div class="paper__bar">
            {#if canExport}
              <div class="paper__export">
                <Button
                  variant="quiet"
                  size="sm"
                  icon="download"
                  busy={busy === 'export'}
                  disabled={busy === 'export'}
                  onclick={() => void onexport('markdown')}
                >
                  Скачать ответ
                </Button>
              </div>
            {/if}
          </div>
        </header>

        {#if payload.degradation_reasons.length}
            <!-- Если ответ неполный, рядом указана причина. -->
          <div class="paper__degraded">
            <Notice tone="warn" title={DEGRADED.title}>{DEGRADED.hint}</Notice>
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
            <div class="theses">
              {#each visibleFindings as finding (finding.id)}
                {@const selected = finding.id === openClaimId}
                {@const subject = subjectTerm(finding)}
                {@const predicate = predicateTerm(finding)}
                <article class="thesis" class:thesis--selected={selected} data-claim={finding.id}>
                  <div class="thesis__head">
                    <div class="thesis__marks">
                      <StatusPill status={finding.status} label={STATUS_PHRASE[finding.status]} />
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

                  <p class="small thesis__closed">{valueSummary(finding)}</p>
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
          <details class="chat__more">
            <summary>Дополнительные сведения</summary>
            {#each [...payload.conflicts, ...payload.knowledge_gaps, ...payload.recommendations] as item, index (index)}
              <p class="small">{item}</p>
            {/each}
            <Button href="/numbers" variant="quiet" size="sm">Сравнить числа</Button>
          </details>
        {/if}
      </article>
    {/if}
  </div>

  <!-- ── 4. КОМПОЗЕР: прилип ко дну экрана, маскот показывает состояние сборки -->
  <div class="ask__composer">
    <PromptInput
      bind:value={draft}
      variant="docked"
      name="ask"
      label="Сообщение"
      placeholder="Напишите сообщение…"
      busy={running}
      disabled={!canAsk}
      tools={composerTools}
      onsubmit={ask}
    />
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

{#if thesisVerdict && answer}
  <!-- Правка конкретного тезиса: та же шторка вердикта, что и у ответа целиком,
       только с finding_id этого тезиса. Закрыли — вернулись к тому же разбору. -->
  <VerdictSheet
    subject={thesisVerdict.statement}
    subjectNote={sourceOf(thesisVerdict)}
    queryId={answer.query_id}
    findingId={thesisVerdict.id}
    correction={thesisVerdict.statement}
    candidates={[thesisVerdict]}
    gate={canSupersede ? '' : ASK_VERDICT.thesisGate}
    onclose={() => (thesisVerdict = null)}
    onsent={(result) =>
      onnotice(
        result.superseded
          ? {
              kind: 'ok',
              title: 'Правка принята',
              detail: `Тезис получил версию ${result.superseded.version}, прежняя осталась в истории. Отменить правку отсюда нельзя.`,
            }
          : {
              kind: 'ok',
              title: 'Правка записана',
              detail: result.proposal
                ? 'Новая версия появится после подтверждения: предложение на проверку поставлено.'
                : 'Новая версия появится после подтверждения: модель предложение не сформировала, записано только решение.',
            },
      )}
  />
{/if}

{#snippet thesisTrace(finding: Finding)}
  <div class="sheet__thesis">
    <div class="thesis__marks">
      <StatusPill status={finding.status} label={STATUS_PHRASE[finding.status]} />
    </div>
    <h3 class="h4">{finding.statement}</h3>
    <p class="micro muted sheet__facts">
      <span>{sourceOf(finding)}</span>
      <span>{scopeText(finding.scope)}</span>
      <span>{countOf(finding.evidence.length, 'цитата', 'цитаты', 'цитат')}</span>
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
        <Button href="/numbers" variant="quiet" size="sm" icon="conflict">
          Раздел «{navLabel('/numbers')}»
        </Button>
      </div>
    </div>
  {/if}

  <!-- Каждое доказательство раскрывается с клавиатуры -->
  <div class="trace">
    <p class="micro trace__label">
      {SHEET_LABELS.traceLabel}
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
              <dd>{evidence.source_title || 'исходный материал'}</dd>
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
      <Button
        variant="quiet"
        size="sm"
        icon="plus"
        disabled={running}
        onclick={() => startCorrection(finding)}
      >
        Исправить этот тезис
      </Button>
    </div>
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

{/snippet}

<style>
  .ask {
    display: flex;
    flex-direction: column;
    gap: var(--s5);
    min-height: 100%;
  }

  .ask__flow {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    gap: var(--s5);
    min-width: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding-inline: clamp(var(--s2), 1.5vw, var(--s4));
  }

  .chat__welcome {
    display: grid;
    flex: 1;
    align-content: center;
    justify-items: center;
    gap: var(--s3);
    min-height: 15rem;
    padding: var(--s6) var(--s4);
    text-align: center;
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
    padding-block: var(--s2);
  }

  .ask__note {
    margin: 0;
    padding-inline: var(--s4);
    font-size: var(--t-micro);
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

    /* Пока ответа нет, прилипшее поле съедало треть окна и прятало половину
       пустого состояния под собой: на узком экране композер встаёт в конец
       колонки и скроллится вместе с ней. С ответом прилипание нужно — к полю
       возвращаются за новым вопросом, не прокручивая разбор до конца. */
    .ask--empty .ask__composer {
      position: static;
    }

    /* Поле внутри композера прилипает само по себе: если оставить его sticky
       в статичном композере, оно ляжет на строку показаний корпуса. */
    .ask--empty .ask__composer :global(.prompt) {
      position: static;
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

  /* Класс приходит на корень Button, поэтому селектор один: парное `.btn.example`
     не совпадало ни с чем, и пример вопроса оставался нестилизованным контролом
     высотой 26 px вместо задуманных 44. */
  .example {
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

  /* ── Бумага ответа ───────────────────────────────────────────────────── */

  /* Лист лежит на всю ширину (wrap--bleed): плотность даёт таблица и шкала,
     а читается текст в своей мере, поэтому она ограничена внутри колонок. */
  .paper {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--s4);
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

  .chat__question {
    align-self: flex-end;
    max-width: min(80%, 62ch);
    margin: 0;
    padding: var(--s3) var(--s4);
    border-radius: var(--r-lg);
    background: var(--coral-mist);
    color: var(--ink);
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

  .chat__more {
    max-width: var(--maxw-measure);
    border-top: 1px solid var(--line);
    padding-top: var(--s3);
  }

  .chat__more summary {
    width: fit-content;
    color: var(--ink-2);
    cursor: pointer;
  }

  .chat__more p {
    margin-block: var(--s3);
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





  .paper__service,
  .sheet__service {
    margin-top: var(--s3);
  }

  .paper__body {
    display: block;
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


  /* Пока вердикт не выбран, подсказка стоит рядом с кнопкой, а не вместо неё. */
  .block__note {
    color: var(--ink-2);
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
