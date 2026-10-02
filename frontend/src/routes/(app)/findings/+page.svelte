<script lang="ts">
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import { tick } from 'svelte';
  import { ApiError, api } from '$lib/api';
  import { navLabel } from '$lib/nav';
  import { countOf, num, pct } from '$lib/format';
  import { observeReveals } from '$lib/reveal';
  import { session } from '$lib/sessionStore.svelte';
  import {
    FINDINGS_ACTION,
    FINDINGS_EMPTY,
    FINDINGS_STATE,
    FINDINGS_TERM_NAMES,
    OPERATOR_SYMBOL,
    PREDICATE_LABELS,
    PROPERTY_LABELS,
    STATUS_SHORT,
    STATUS_SUPERSEDED,
    SUBJECT_LABELS,
    describeScope,
    describeValue,
    findingsLinkMiss,
    findingsNotLoadedBody,
    findingsPartialNote,
    findingsScaleGapNote,
    findingsScaleSpread,
    findingsShownOf,
    findingsTerm,
    headingTerm,
  } from '$lib/terms';
  import {
    DATA_CLASS_LABELS,
    type ClaimHistory,
    type DocumentReceipt,
    type Evidence,
    type FindingApiStatus,
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
  import Sheet from '$lib/ui/Sheet.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';

  // ── Находки: отбор, список, импорт
  // PAGE_SIZE раскрывает список по частям на экране, SERVER_PAGE — сколько находок
  // приходит одним запросом: сервер режет список страницей, и полное число стоит
  // в заголовке ответа, а не в длине массива.
  const PAGE_SIZE = 25;
  const SERVER_PAGE = 200;
  // Список субъектов — тоже список: у него свой потолок и «Показать все».
  const SUBJECTS_CAP = 12;

  const STATUS_ORDER: FindingApiStatus[] = ['consensus', 'disputed', 'hypothesis'];

  const ACCEPT_ATTR = '.pdf,.docx,.xlsx,.json,.txt';
  const ACCEPT_NOTE = 'Подходят PDF, DOCX, XLSX, JSON и TXT.';

  // Возврат после входа — на этот же экран, а не в раздел по умолчанию:
  // сессия прервалась посреди отбора, и сбрасывать его незачем.
  const LOGIN_HREF = $derived(`/login?next=${encodeURIComponent(page.url.pathname)}`);

  type Failure = {
    kind: 'session' | 'forbidden' | 'model' | 'backend' | 'other';
    text: string;
  };
  type HistoryState = { loading: boolean; data: ClaimHistory | null; error: string };
  type Scale = { min: number; max: number; count: number };
  type Measure = {
    raw: string;
    scale: Scale | null;
    band: { left: number; width: number } | null;
    limits: { at: number; label: string }[];
    note: string;
  };
  type UploadItem = {
    key: string;
    name: string;
    size: number;
    state: 'queued' | 'uploading' | 'done' | 'error';
    receipt: DocumentReceipt | null;
    failure: Failure | null;
  };

  // Отказ входа, отказ по доступу и технический отказ — три разных факта и три
  // разных действия на экране; сводить их к «что-то не загрузилось» нельзя.
  function classify(reason: unknown): Failure {
    if (reason instanceof ApiError && reason.status === 401) {
      return { kind: 'session', text: reason.message };
    }
    if (reason instanceof ApiError && reason.status === 403) {
      return { kind: 'forbidden', text: reason.message };
    }
    const text = reason instanceof Error ? reason.message : String(reason);
    if (/LLM не настроен|GigaChat/i.test(text)) return { kind: 'model', text };
    if (/Бэкенд недоступен|Бэкенд не ответил|HTTP 50[23]/i.test(text)) return { kind: 'backend', text };
    return { kind: 'other', text };
  }

  // На экран выходит только человеческая формулировка: что не получилось и что
  // с этим сделать. Служебный текст отказа остаётся в классификации.
  function guidanceOf(failure: Failure | null): string {
    if (failure?.kind === 'session') {
      return 'Вход не подтверждён: войдите заново и повторите запрос.';
    }
    if (failure?.kind === 'forbidden') {
      return FINDINGS_STATE.deniedBody;
    }
    return FINDINGS_STATE.failedBody;
  }

  // Отказ истории версий называется своей строкой: тот же классификатор, но про
  // то, что именно сейчас не загрузилось.
  function historyErrorOf(reason: unknown): string {
    const failure = classify(reason);
    if (failure.kind === 'session') return 'Вход не подтверждён: войдите заново и повторите запрос.';
    if (failure.kind === 'forbidden') return 'Версии этого утверждения открыты при доступе к корпусу.';
    return 'Не удалось загрузить версии утверждения. Повторите запрос.';
  }

  function ruDate(iso: string): string {
    const date = new Date(iso);
    return Number.isNaN(date.getTime())
      ? iso
      : date.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
  }

  function statusLabel(value: FindingApiStatus): string {
    return STATUS_SHORT[value];
  }

  // Имя ключа онтологии: сначала словарь, потом человекочитимая заглушка. Сырой
  // ключ заголовком не становится: он выходит в «Служебных данных» (поле `code`).
  function subjectTerm(key: string | null | undefined): { label: string; code: string | null } {
    return findingsTerm(SUBJECT_LABELS, key, {
      absent: FINDINGS_TERM_NAMES.noSubject,
      unknown: FINDINGS_TERM_NAMES.subject,
    });
  }

  function predicateTerm(key: string | null | undefined): { label: string; code: string | null } {
    return findingsTerm(PREDICATE_LABELS, key, {
      absent: FINDINGS_TERM_NAMES.noPredicate,
      unknown: FINDINGS_TERM_NAMES.predicate,
    });
  }

  function propertyTerm(key: string | null | undefined): { label: string; code: string | null } {
    return findingsTerm(PROPERTY_LABELS, key, {
      absent: FINDINGS_TERM_NAMES.property,
      unknown: FINDINGS_TERM_NAMES.property,
    });
  }

  function bandClass(status: FindingApiStatus): string {
    if (status === 'consensus') return 'bar__fill--consensus';
    if (status === 'disputed') return 'bar__fill--disputed';
    return '';
  }

  function bytesLabel(value: number): string {
    if (value >= 1024 * 1024) return `${num(Math.round((value / (1024 * 1024)) * 100) / 100)} МБ`;
    if (value >= 1024) return `${num(Math.round((value / 1024) * 10) / 10)} КБ`;
    return `${num(value)} байт`;
  }

  function normValues(obs: NumericObservation): number[] {
    if (obs.operator === 'between') {
      return [obs.normalized_min, obs.normalized_max].filter((v): v is number => typeof v === 'number');
    }
    return typeof obs.normalized_value === 'number' ? [obs.normalized_value] : [];
  }

  function hasNumber(obs: NumericObservation): boolean {
    return (
      obs.normalized_value != null ||
      obs.value != null ||
      obs.normalized_min != null ||
      obs.min_value != null ||
      obs.normalized_max != null ||
      obs.max_value != null
    );
  }

  // Число наблюдения собирает словарь `describeValue` («не меньше 95 %»,
  // «95–97 %»): экран не пересобирает формат и не печатает десятичную точку.
  function valueLabel(obs: NumericObservation): string {
    return hasNumber(obs) ? describeValue(obs) : FINDINGS_STATE.noNumber;
  }

  // Место в источнике для строки списка: название документа и первые две подписи
  // (страница, лист, диапазон ячеек). Полное трассирование и фрагмент открывает
  // шторка по этому же клику.
  function placeOf(finding: FindingListItem): { title: string; parts: string[] } | null {
    const ev = finding.evidence[0];
    if (!ev) return null;
    return { title: ev.source_title, parts: locatorParts(ev).slice(0, 2) };
  }

  // Место в источнике называется словами и отдельными подписями: страница, лист
  // или диапазон ячеек. Без номера страницы остаётся только название источника,
  // прочерк вместо места не печатается. Символовые оффсеты уходят под раскрытие
  // «Служебные данные».
  function locatorParts(ev: Evidence): string[] {
    const parts: string[] = [];
    if (ev.page != null) parts.push(`страница ${num(ev.page)}`);
    if (ev.sheet) parts.push(`лист ${ev.sheet}`);
    if (ev.cell_range) parts.push(`ячейки ${ev.cell_range}`);
    return parts;
  }

  function scaleKey(property: string, unit: string): string {
    return `${property} || ${unit}`;
  }

  const canRead = $derived(session.can('knowledge:read'));

  const blocked = $derived.by(() => {
    if (session.state === 'anonymous') return 'session';
    if (session.state === 'authenticated' && !canRead) return 'permission';
    return '';
  });

  // Список живёт страницами сервера: `index` несёт страницу корпуса без отбора,
  // `rows` — страницу применённого отбора. Полное число подходящих находок приходит
  // заголовком ответа, и пока оно больше загруженного, список не читается как
  // «все находки корпуса». По `index` считают чипы статуса и список субъектов,
  // чтобы отбор не оставлял себя без вариантов выбора.
  let index = $state<FindingListItem[]>([]);
  let rows = $state<FindingListItem[]>([]);
  let indexTotal = $state<number | null>(null);
  let rowsTotal = $state<number | null>(null);
  // X-Window-Note: явная оговорка сервера о неполном списке. Пустая строка значит
  // «оговорки не было», а не «она пустая».
  let serverNote = $state('');
  // Черновик поля субъекта: применённое условие живёт в `appliedSubject`, чтобы
  // видно было и набранное, и выбранное из списка.
  let subjectDraft = $state('');
  let appliedSubject = $state('');
  let appliedStatus = $state<'' | FindingApiStatus>('');
  let phase = $state<'loading' | 'ready' | 'failed'>('loading');
  let querying = $state(false);
  let loadingMore = $state(false);
  let moreFailure = $state('');
  // Пустая следующая страница при известном остатке: смещение не сдвинулось,
  // добавлять нечего — кнопка тогда звала бы в никуда.
  let loadStalled = $state(false);
  // Сколько записей сервер уже отдал по текущему отбору: по этому числу и
  // запрашивается следующая страница. Длина загруженного списка не годится —
  // страницы могут перекрываться, и смещение перестало бы двигаться.
  let servedCount = 0;
  // Порядок дозагрузок: старый ответ не имеет права примешиваться к новому срезу
  // после смены отбора или повтора запроса.
  let loadSeq = 0;
  let failure = $state<Failure | null>(null);
  let shown = $state(PAGE_SIZE);
  let subjectsOpen = $state(false);
  let subjectQuery = $state('');
  let subjectsShown = $state(SUBJECTS_CAP);
  let active = $state<string | null>(null);
  let histories = $state<Record<string, HistoryState>>({});

  // Глубокая ссылка (`?document=` / `?claim=`) — единственный способ попасть на
  // этот экран из ответа или с карты. Документ сервер не фильтрует, поэтому отбор
  // по нему клиентский и живёт рядом с применёнными условиями.
  let appliedDocument = $state('');
  let linkNote = $state('');
  let linkHandled = false;

  let importOpen = $state(false);
  let uploading = $state(false);
  let uploads = $state<UploadItem[]>([]);
  let dragDepth = $state(0);

  // Числа у чипов статуса берутся из списка без отбора и подписаны так же, как
  // список субъектов: они относятся к показанным находкам, а не ко всему корпусу.
  const facets = $derived(
    STATUS_ORDER.map((key) => ({
      key,
      label: STATUS_SHORT[key],
      count: index.filter((finding) => finding.status === key).length,
    })),
  );

  const subjectIndex = $derived.by(() => {
    const groups = new Map<string, FindingListItem[]>();
    for (const finding of index) {
      const key = finding.subject ?? '';
      const bucket = groups.get(key);
      if (bucket) bucket.push(finding);
      else groups.set(key, [finding]);
    }
    return [...groups.entries()].sort((a, b) => {
      if (!a[0]) return 1;
      if (!b[0]) return -1;
      // Порядок по имени субъекта: список читается как словарь.
      return subjectTerm(a[0]).label.localeCompare(subjectTerm(b[0]).label, 'ru');
    });
  });

  // Список субъектов — тоже список: у него есть потолок и «Показать все», иначе
  // при сотнях субъектов панель превращается в стену кнопок.
  const subjectChoices = $derived.by(() => {
    const query = subjectQuery.trim().toLowerCase();
    const matched = query
      ? subjectIndex.filter(
          ([name]) =>
            subjectTerm(name).label.toLowerCase().includes(query) ||
            name.toLowerCase().includes(query),
        )
      : subjectIndex;
    return { all: matched.length, rows: matched.slice(0, subjectsShown) };
  });

  // Шкала показателя строится по загруженному списку: ни один предел не берётся
  // из головы, границы — реальные min/max найденных чисел.
  const scales = $derived.by(() => {
    const map = new Map<string, Scale>();
    for (const finding of index) {
      for (const obs of finding.observations) {
        const key = scaleKey(obs.property_name, obs.normalized_unit);
        for (const value of normValues(obs)) {
          const current = map.get(key);
          if (!current) map.set(key, { min: value, max: value, count: 1 });
          else {
            current.min = Math.min(current.min, value);
            current.max = Math.max(current.max, value);
            current.count += 1;
          }
        }
      }
    }
    return map;
  });

  const listed = $derived(
    appliedDocument
      ? rows.filter((finding) =>
          finding.evidence.some((ev) => ev.document_id === appliedDocument),
        )
      : rows,
  );

  // Название источника для подписи фильтра берётся из самого доказательства,
  // а не из адреса: в URL лежит служебный код, который показывать нечего.
  const documentName = $derived.by(() => {
    if (!appliedDocument) return '';
    for (const source of [...rows, ...index]) {
      const ev = source.evidence.find((e) => e.document_id === appliedDocument);
      if (ev?.source_title) return ev.source_title;
    }
    return 'источник из адреса';
  });

  const visible = $derived(listed.slice(0, shown));
  const filtered = $derived(Boolean(appliedSubject || appliedStatus || appliedDocument));
  const activeFinding = $derived(rows.find((finding) => finding.id === active) ?? null);

  // Каким списком владеет экран: отбор даёт свою страницу и своё полное число, без
  // отбора список несёт страницу корпуса (rows тогда равен index).
  const viewFiltered = $derived(Boolean(appliedSubject || appliedStatus));
  const loadedCount = $derived(viewFiltered ? rows.length : index.length);
  const loadedTotal = $derived(viewFiltered ? rowsTotal : indexTotal);
  // Остаток за загруженной страницей: null — показаний нет, и это не то же, что ноль.
  const leftOff = $derived(loadedTotal === null ? null : Math.max(loadedTotal - loadedCount, 0));
  // Следующую страницу предлагаем, когда сервер называет остаток либо когда
  // страница заполнена до конца, а полного числа мы не получили.
  const canLoadMore = $derived(
    !loadingMore && (leftOff === null ? loadedCount >= SERVER_PAGE : leftOff > 0),
  );
  // Одна кнопка «Показать ещё»: сначала раскрывает то, что уже пришло, потом
  // догружает следующую страницу тем же нажатием.
  const canRevealMore = $derived(listed.length > shown);
  const showMoreVisible = $derived(
    (canRevealMore || (canLoadMore && !loadStalled)) && listed.length > 0,
  );

  // Отказ опроса — состояние списка, а не «пустой результат»: строка обязана
  // называть его, иначе сбой читается как законная пустота.
  const listFailed = $derived(Boolean(failure) && !querying && phase === 'ready');
  // Знаменатель счётчика — та область, на которую смотрит человек: весь список,
  // отбор или один источник из ссылки.
  const scopeQualifier = $derived(
    appliedDocument ? ' этого источника' : viewFiltered ? ' по этому отбору' : '',
  );
  const scopeTotal = $derived(appliedDocument ? listed.length : loadedTotal);
  const listNote = $derived.by(() => {
    if (querying) return FINDINGS_STATE.searching;
    if (phase === 'loading') return FINDINGS_STATE.loading;
    if (failure) return FINDINGS_STATE.failedTitle;
    return findingsShownOf(visible.length, scopeTotal, scopeQualifier);
  });
  // Неполнота называется один раз и только когда за показанным точно что-то
  // осталось: известное число, нераскрытая часть списка или обещанная страница.
  const hiddenLeft = $derived(
    Math.max(listed.length - visible.length, 0) +
      (appliedDocument ? 0 : leftOff === null ? (canLoadMore ? 1 : 0) : leftOff),
  );
  const partialNote = $derived(findingsPartialNote(hiddenLeft));

  // Разбивка по статусам вместо декоративных точек: числа настоящие, ничего не
  // обрезается и не прячется от скринридера.
  function statusCounts(group: FindingListItem[]): { label: string; count: number }[] {
    return STATUS_ORDER.map((key) => ({
      label: STATUS_SHORT[key],
      count: group.filter((item) => item.status === key).length,
    }));
  }

  // Применённое условие: именем остаётся человекочитимое название (или заглушка),
  // а служебное имя уходит под раскрытие «Служебные данные отбора»: в строке
  // отбора сырой ключ онтологии и код источника не становятся именем условия.
  type AppliedPart = {
    key: string;
    name: string;
    value: string;
    tech: string | null;
    techName: string;
    clear: () => void;
  };

  const appliedParts = $derived.by((): AppliedPart[] => {
    const parts: AppliedPart[] = [];
    if (appliedSubject) {
      const subject = subjectTerm(appliedSubject);
      parts.push({
        key: 'subject',
        name: 'субъект',
        value: subject.label,
        tech: subject.code,
        techName: 'служебное имя субъекта',
        clear: clearSubject,
      });
    }
    if (appliedStatus) {
      parts.push({
        key: 'status',
        name: 'статус',
        value: STATUS_SHORT[appliedStatus],
        tech: null,
        techName: '',
        clear: clearStatus,
      });
    }
    if (appliedDocument) {
      parts.push({
        key: 'document',
        name: 'источник',
        value: documentName,
        tech: appliedDocument,
        techName: 'код источника',
        clear: clearDocument,
      });
    }
    return parts;
  });

  // Служебные имена применённых условий: чем отсеян список, читает тот, кто
  // сверяет запись с сервером, а не тот, кто смотрит на отбор.
  const appliedTechRows = $derived(
    appliedParts.filter((part): part is AppliedPart & { tech: string } => Boolean(part.tech)),
  );

  function measureOf(obs: NumericObservation): Measure {
    const scale = scales.get(scaleKey(obs.property_name, obs.normalized_unit)) ?? null;
    const sign = OPERATOR_SYMBOL[obs.operator];
    const raw = valueLabel(obs);

    const values = normValues(obs);
    if (!scale || scale.max <= scale.min || values.length === 0) {
      // Полосы нет, и экран называет причину: у показателя нет приведённых чисел,
      // у этой строки нет числа либо значение встречалось в списке одно.
      const reason: 'absent' | 'noValue' | 'single' = !scale
        ? 'absent'
        : values.length === 0
          ? 'noValue'
          : 'single';
      return { raw, scale, band: null, limits: [], note: findingsScaleGapNote(reason) };
    }

    const span = scale.max - scale.min;
    const pos = (value: number) => ((value - scale.min) / span) * 100;
    const limits: Measure['limits'] = [];
    let band: Measure['band'] = null;

    if (obs.operator === 'between') {
      const left = pos(obs.normalized_min ?? scale.min);
      const right = pos(obs.normalized_max ?? scale.max);
      band = { left, width: Math.max(right - left, 0) };
      // Подписи пределов читаются теми же числами, что и диапазон в строке:
      // приведённое значение приоритетнее исходного, иначе полоса расходилась бы
      // с текстом наблюдения.
      limits.push({ at: left, label: num(obs.normalized_min ?? obs.min_value ?? scale.min) });
      limits.push({ at: right, label: num(obs.normalized_max ?? obs.max_value ?? scale.max) });
    } else if (obs.normalized_value != null) {
      const at = pos(obs.normalized_value);
      band =
        obs.operator === 'lt' || obs.operator === 'lte'
          ? { left: 0, width: at }
          : obs.operator === 'gt' || obs.operator === 'gte'
            ? { left: at, width: 100 - at }
            : { left: at, width: 0 };
      limits.push({
        at,
        label: `${sign === '=' ? '' : `${sign} `}${num(obs.normalized_value ?? obs.value)}`,
      });
    }

    return { raw, scale, band, limits, note: '' };
  }

  function stateOf(id: string): HistoryState | null {
    return histories[id] ?? null;
  }

  function chainOf(id: string): ClaimHistory | null {
    return histories[id]?.data ?? null;
  }

  async function loadHistory(id: string): Promise<void> {
    histories = { ...histories, [id]: { loading: true, data: null, error: '' } };
    try {
      const data = await api.claimHistory(id);
      histories = { ...histories, [id]: { loading: false, data, error: '' } };
    } catch (reason) {
      histories = { ...histories, [id]: { loading: false, data: null, error: historyErrorOf(reason) } };
    }
  }

  // «Открыть источник» — шторка с полным трассированием; при первом открытии
  // версии запрашивают сразу, не заставляя искать их отдельной кнопкой. Открытие
  // шторки снимает напоминание о ссылке: экран уже показал, что просили.
  // Строка, с которой ушли в шторку, помнит своё место прокрутки: список и отбор
  // не пересобираются, и возврат обязан оставить аналитика там же, где он читал.
  let listScrollY = 0;

  function openSource(id: string): void {
    if (browser && !active) listScrollY = window.scrollY;
    active = id;
    linkNote = '';
    if (!histories[id]) void loadHistory(id);
  }

  async function closeSource(): Promise<void> {
    active = null;
    if (!browser) return;
    await tick();
    // Шторка блокировала прокрутку документа и возвращает фокус на строку:
    // положение чтения восстанавливаем поверх этого, иначе список подпрыгнет.
    window.scrollTo({ top: listScrollY });
  }

  // Находки стоят над отбором: после применения условия или открытия формы
  // возвращаем чтение к нужному блоку, иначе действие уводит вниз.
  async function revealNode(selector: string): Promise<void> {
    if (!browser) return;
    await tick();
    document.querySelector(selector)?.scrollIntoView({ block: 'start' });
  }

  // Субъекты — раскрытие в строке отбора: при сотнях субъектов панель не должна
  // становиться стеной кнопок, поэтому у неё поиск и потолок показа.
  function toggleSubjects(): void {
    subjectsOpen = !subjectsOpen;
    subjectsShown = SUBJECTS_CAP;
    if (subjectsOpen) void revealNode('.findings__subjects');
  }

  function showAllSubjects(): void {
    subjectsShown = subjectChoices.all;
  }

  // Импорт больше не спорит с главным действием экрана в шапке: он открывается
  // кнопкой — из пустого корпуса или из спокойной строки под списком.
  function toggleImport(): void {
    importOpen = !importOpen;
    if (importOpen) void revealNode('.findings__import');
  }

  // Ссылка раскрывается только после ответа сервера: до него нечего открывать и
  // нечем фильтровать. Одна попытка на заход — повторный список после импорта не
  // должен возвращать снятый отбор.
  function applyDeepLink(): void {
    if (linkHandled) return;
    linkHandled = true;
    const params = page.url.searchParams;
    const document = params.get('document')?.trim() ?? '';
    const claim = params.get('claim')?.trim() ?? '';
    if (document) appliedDocument = document;
    if (!claim) return;
    if (index.some((finding) => finding.id === claim)) {
      openSource(claim);
    } else {
      // Ссылка из устаревшего ответа, из закрытого доступа либо вне загруженной
      // части списка: причина называется словами, а не молчаливой пустотой.
      linkNote = findingsLinkMiss();
    }
  }

  // После чтения ссылки адрес перестаёт быть источником истины: сняли отбор —
  // параметр уходит из строки, иначе обновлённая страница вернула бы убранное.
  function dropLinkParams(): void {
    if (!browser) return;
    const url = new URL(page.url);
    url.searchParams.delete('document');
    url.searchParams.delete('claim');
    window.history.replaceState(window.history.state, '', url);
  }

  function clearDocument(): void {
    appliedDocument = '';
    dropLinkParams();
  }

  // Страницы сервера могут перекрываться, если корпус между запросами изменился, а
  // ключ each идёт по коду утверждения: повтор в список не добавляется.
  function mergePages(current: FindingListItem[], incoming: FindingListItem[]): FindingListItem[] {
    const seen = new Set(current.map((finding) => finding.id));
    return [...current, ...incoming.filter((finding) => !seen.has(finding.id))];
  }

  // Приёмка документа не должна стирать отбор аналитика: при preserveFilters
  // черновики, применённые условия и глубина показа выживают, а находки
  // запрашиваются по прежним условиям.
  async function loadIndex(preserveFilters = false): Promise<void> {
    phase = 'loading';
    failure = null;
    moreFailure = '';
    // Список читается заново с нуля: прежний тупик дозагрузки к нему не относится.
    loadStalled = false;
    serverNote = '';
    loadSeq += 1;
    loadingMore = false;
    servedCount = 0;
    try {
      const corpus = await api.findings(undefined, undefined, SERVER_PAGE, 0);
      index = mergePages([], corpus.items);
      indexTotal = corpus.total;
      serverNote = corpus.windowNote;
      servedCount = corpus.items.length;
      if (preserveFilters && (appliedSubject || appliedStatus)) {
        const next = await api.findings(
          appliedSubject || undefined,
          appliedStatus || undefined,
          SERVER_PAGE,
          0,
        );
        rows = mergePages([], next.items);
        rowsTotal = next.total;
        // Оговорка относится к тому списку, который сейчас на экране.
        serverNote = next.windowNote;
        // Дальше добавляем по этому срезу, а не по списку корпуса.
        servedCount = next.items.length;
      } else {
        rows = index;
        rowsTotal = indexTotal;
        if (!preserveFilters) {
          subjectDraft = '';
          appliedSubject = '';
          appliedStatus = '';
          shown = PAGE_SIZE;
        }
      }
      active = null;
      phase = 'ready';
      applyDeepLink();
    } catch (reason) {
      failure = classify(reason);
      phase = 'failed';
    }
  }

  async function runQuery(nextSubject: string, nextStatus: '' | FindingApiStatus): Promise<void> {
    querying = true;
    failure = null;
    moreFailure = '';
    loadStalled = false;
    serverNote = '';
    loadSeq += 1;
    loadingMore = false;
    servedCount = 0;
    appliedSubject = nextSubject;
    appliedStatus = nextStatus;
    shown = PAGE_SIZE;
    active = null;
    try {
      const next = await api.findings(
        nextSubject || undefined,
        nextStatus || undefined,
        SERVER_PAGE,
        0,
      );
      const items = mergePages([], next.items);
      rows = items;
      rowsTotal = next.total;
      serverNote = next.windowNote;
      servedCount = next.items.length;
      // Без отбора список несёт страницу корпуса: строки и полное число одни на
      // весь экран, иначе чипы статуса и счётчик разошлись бы.
      if (!nextSubject && !nextStatus) {
        index = items;
        indexTotal = next.total;
      }
      phase = 'ready';
    } catch (reason) {
      // Строки прежнего отбора не остаются на экране под новым фильтром: их нет
      // в ответе. Отказ владеет списком, а не превращается в «пусто».
      rows = [];
      rowsTotal = null;
      failure = classify(reason);
      phase = index.length > 0 ? 'ready' : 'failed';
    } finally {
      querying = false;
      await revealNode('.findings__list');
    }
  }

  // Дозагрузка следующей страницы: показанное остаётся на экране, отказ называется
  // своей строкой и не стирает список. Счётчик `loadSeq` отсекает ответ запоздавшей
  // страницы: смена отбора или повтор сбрасывают дозагрузку, иначе в список
  // примешался бы прежний срез.
  async function loadMore(): Promise<void> {
    if (!canLoadMore) return;
    const call = ++loadSeq;
    const offset = servedCount;
    loadingMore = true;
    moreFailure = '';
    try {
      const next = await api.findings(
        appliedSubject || undefined,
        appliedStatus || undefined,
        SERVER_PAGE,
        offset,
      );
      if (call !== loadSeq) return;
      // Смещение идёт по тому, что прислал сервер, а не по длине загруженного
      // списка: страницы могут перекрываться после изменения корпуса, и сдвиг по
      // своему счётчику молча застрял бы на одной и той же странице.
      servedCount = offset + next.items.length;
      // Дозагрузка встала: сервер отдал пустую страницу, смещение не ушло.
      loadStalled = next.items.length === 0;
      if (viewFiltered) {
        rows = mergePages(rows, next.items);
        if (next.total !== null) rowsTotal = next.total;
      } else {
        index = mergePages(index, next.items);
        rows = index;
        if (next.total !== null) indexTotal = next.total;
      }
      if (next.windowNote) serverNote = next.windowNote;
    } catch {
      if (call === loadSeq) moreFailure = FINDINGS_STATE.moreFailed;
    } finally {
      if (call === loadSeq) loadingMore = false;
    }
  }

  // Одна кнопка «Показать ещё»: сначала раскрывает то, что уже пришло, потом тем же
  // нажатием догружает следующую страницу сервера.
  function showMore(): void {
    if (canRevealMore) {
      shown += PAGE_SIZE;
      return;
    }
    void loadMore();
  }

  // Повтор ровно тех же условий, а не «загрузить всё»: намерение аналитика
  // сохраняется.
  function retryList(): void {
    void runQuery(appliedSubject, appliedStatus);
  }

  // Поле субъекта отправляет набранное, статус задают только чипы: второго
  // контрола над тем же условием на экране нет.
  function submitFilters(event: SubmitEvent): void {
    event.preventDefault();
    void runQuery(subjectDraft.trim(), appliedStatus);
  }

  function resetAll(): void {
    subjectDraft = '';
    subjectQuery = '';
    subjectsShown = SUBJECTS_CAP;
    appliedDocument = '';
    linkNote = '';
    dropLinkParams();
    void runQuery('', '');
  }

  function clearStatus(): void {
    void runQuery(appliedSubject, '');
  }

  function clearSubject(): void {
    subjectDraft = '';
    void runQuery('', appliedStatus);
  }

  function pickFacet(value: FindingApiStatus): void {
    void runQuery(appliedSubject, appliedStatus === value ? '' : value);
  }

  // Выбор субъекта из списка берёт его служебный ключ для запроса, но не подставляет
  // ключ в поле: сырое имя онтологии на экран не выходит, условие видно чипом отбора.
  function pickSubject(name: string): void {
    void runQuery(name, appliedStatus);
  }

  function patchUpload(key: string, patch: Partial<UploadItem>): void {
    uploads = uploads.map((item) => (item.key === key ? { ...item, ...patch } : item));
  }

  function dismissUpload(key: string): void {
    uploads = uploads.filter((item) => item.key !== key);
  }

  // multipart-загрузка по одному файлу: у каждого — свой ответ сервера, поэтому
  // частичный успех пачки виден как частичный успех, а не как «импорт упал».
  async function ingest(files: File[]): Promise<void> {
    if (files.length === 0) return;
    const items: UploadItem[] = files.map((file, position) => ({
      key: `${Date.now()}-${position}-${file.name}`,
      name: file.name,
      size: file.size,
      state: 'queued',
      receipt: null,
      failure: null,
    }));
    uploads = [...items, ...uploads];
    uploading = true;
    let accepted = false;
    for (const [position, file] of files.entries()) {
      const item = items[position];
      patchUpload(item.key, { state: 'uploading' });
      try {
        const receipt = await api.upload(file);
        accepted = true;
        patchUpload(item.key, { state: 'done', receipt });
      } catch (reason) {
        patchUpload(item.key, { state: 'error', failure: classify(reason) });
      }
    }
    uploading = false;
    if (accepted) await loadIndex(true);
  }

  function pickDocuments(event: Event): void {
    const field = event.currentTarget as HTMLInputElement;
    const files = Array.from(field.files ?? []);
    field.value = '';
    void ingest(files);
  }

  function onDrop(event: DragEvent): void {
    event.preventDefault();
    dragDepth = 0;
    void ingest(Array.from(event.dataTransfer?.files ?? []));
  }

  // Право и сам факт входа приходят с сервера: до /auth/me находки не
  // запрашиваем, иначе экран показал бы «пусто» там, где ещё нет сессии.
  $effect(() => {
    const state = session.state;
    const allowed = canRead;
    if (state === 'unknown') return;
    if (state === 'authenticated' && allowed) {
      void loadIndex();
      return;
    }
    index = [];
    rows = [];
    indexTotal = null;
    rowsTotal = null;
    serverNote = '';
    moreFailure = '';
    loadStalled = false;
    active = null;
    phase = 'ready';
  });

  // Блоки, которые появляются после ответа сервера, тоже нуждаются в
  // reveal-наблюдателе: без повторного скана они остались бы невидимыми.
  $effect(() => {
    void phase;
    void subjectsOpen;
    void importOpen;
    void blocked;
    if (browser) void tick().then(() => observeReveals());
  });

  // Новый текст в поиске субъектов снимает раскрытый потолок: под коротким запросом
  // не должно оставаться длинный хвост прежнего списка.
  $effect(() => {
    void subjectQuery;
    subjectsShown = SUBJECTS_CAP;
  });
</script>

<svelte:head>
  <title>Находки корпуса: Научный Клубок</title>
  <meta
    name="description"
    content="Числа из документов корпуса: находка с доказательством, где именно в источнике, с условиями применения и версиями утверждения."
  />
</svelte:head>

<div class="page findings">
  <div class="wrap">
    <SectionHead
      level="1"
      eyebrow="Корпус"
      title="Находки корпуса"
      lead="Найдите число в документе и откройте доказательство: страницу, лист или диапазон ячеек."
    >
      <p class="micro findings__count" role="status" aria-live="polite">{listNote}</p>
    </SectionHead>

    {#if linkNote}
      <!-- Глубокая ссылка не доехала до утверждения: об этом говорится сразу,
           а не оставляется пользователю догадываться по пустому экрану. -->
      <Panel class="findings__state">
        <Notice tone="warn" title="Ссылка ведёт на недоступное утверждение">
          {linkNote}
          <div class="row">
            <Button variant="quiet" size="sm" onclick={() => (linkNote = '')}>К списку находок</Button>
          </div>
        </Notice>
      </Panel>
    {/if}

    {#if importOpen}
      <Panel tone="sunk" class="findings__import">
        <div>
          <div class="panel__head">
            <div class="grow">
              <h2 class="h4">Новый источник</h2>
              <p class="micro">Пришлите документ, и его находки появятся в этом же списке.</p>
            </div>
          </div>

          <div
            class="dropzone"
            role="group"
            aria-label="Перетащите документы сюда или выберите файлы"
            data-over={dragDepth > 0 ? 'true' : undefined}
            ondragenter={() => (dragDepth += 1)}
            ondragleave={() => (dragDepth = Math.max(0, dragDepth - 1))}
            ondragover={(event) => event.preventDefault()}
            ondrop={onDrop}
          >
            <input
              id="findings-file"
              class="dropzone__input"
              type="file"
              accept={ACCEPT_ATTR}
              multiple
              disabled={uploading}
              onchange={pickDocuments}
            />
            <label class="dropzone__face" for="findings-file">
              <strong class="h4">
                {uploading ? 'Загружаем документы в корпус…' : 'Перетащите документы сюда или выберите файлы'}
              </strong>
              <span class="micro">{ACCEPT_NOTE}</span>
            </label>
          </div>

          {#if uploads.length > 0}
            <ul class="uploads" aria-label="Загруженные документы">
              {#each uploads as item (item.key)}
                <li class="upload" data-state={item.state}>
                  <span class="upload__mark" aria-hidden="true">
                    {#if item.state === 'queued' || item.state === 'uploading'}
                      <span class="spinner spinner--quiet"></span>
                    {:else if item.state === 'done'}
                      <Icon name="checkCircle" size={19} />
                    {:else}
                      <Icon name="alert" size={19} />
                    {/if}
                  </span>
                  <div class="grow">
                    <p class="small"><strong>{item.name}</strong></p>
                    <p class="micro muted">размер {bytesLabel(item.size)}</p>
                    {#if item.state === 'queued'}
                      <p class="micro">Документ в очереди, ждёт своей загрузки.</p>
                    {:else if item.state === 'uploading'}
                      <p class="micro">Отправляем документ в корпус…</p>
                    {:else if item.receipt}
                      <p class="micro upload__ok">
                        {item.receipt.status === 'duplicate'
                          ? 'Дубликат: документ уже в корпусе'
                          : 'Документ принят'}
                      </p>
                      <p class="micro upload__ok">
                        Извлечено{' '}
                        {countOf(item.receipt.extracted_claims, 'утверждение', 'утверждения', 'утверждений')}
                      </p>
                      <details class="svc">
                        <summary class="micro">Служебные данные</summary>
                        <p class="micro svc__row">
                          код документа <code class="code">{item.receipt.document_id}</code>
                        </p>
                        <p class="micro svc__row">
                          контрольная сумма <code class="code">{item.receipt.checksum}</code>
                        </p>
                        {#if item.receipt.prompt_truncated}
                          <p class="micro svc__row">
                            не разобрано символов: <span class="num">{num(item.receipt.omitted_characters)}</span>
                          </p>
                        {/if}
                      </details>
                      {#if item.receipt.prompt_truncated}
                        <!-- Хвост документа за бюджетом разбора не означает «в
                             корпусе такого нет»: число утверждений из неполного
                             разбора обязано быть помечено. -->
                        <p class="micro">Разбор дошёл не до конца: часть текста источника осталась без внимания.</p>
                      {/if}
                    {:else if item.failure}
                      <p class="micro upload__err">
                        {#if item.failure.kind === 'session'}
                          Вход не подтверждён: войдите заново и повторите загрузку.
                        {:else if item.failure.kind === 'forbidden'}
                          Документ не принят: доступ к корпусу этому аккаунту не открыт. Право
                          выдаёт администратор сервиса.
                        {:else}
                          Документ не принят. Проверьте соединение и повторите загрузку.
                        {/if}
                      </p>
                    {/if}
                  </div>
                  <Button variant="link" onclick={() => dismissUpload(item.key)}>
                    Убрать из списка
                  </Button>
                </li>
              {/each}
            </ul>
          {/if}
        </div>
      </Panel>
    {/if}

    {#if blocked === 'session'}
      <Panel class="findings__state">
        <Empty
          icon="lock"
          title="Нужен вход в аккаунт"
          body="Находки корпуса это рабочие данные: без входа в аккаунт они не открываются."
        >
          {#snippet action()}
            <!-- Второго действия здесь нет: повторный запрос с неподтверждённым
                 входом даст тот же отказ, поэтому ведёт только одна кнопка. -->
            <div class="row">
              <Button href={LOGIN_HREF} variant="action">Войти заново</Button>
            </div>
          {/snippet}
        </Empty>
      </Panel>
    {:else if blocked === 'permission'}
      <Panel class="findings__state">
        <Empty
          icon="shield"
          title="Корпус для этого аккаунта закрыт"
          body="Находки, карта связей и сравнение открываются при доступе к корпусу. Право выдаёт администратор сервиса, на экране его не включить."
        >
          {#snippet action()}
            <!-- Одно действие и здесь: повторный запрос ничего не меняет, а
                 переход в панель состояния не открывает доступ. -->
            <div class="row">
              <Button href="/account" variant="quiet">Что открыто моему аккаунту</Button>
            </div>
          {/snippet}
        </Empty>
      </Panel>
    {:else if phase === 'loading'}
      <Panel class="findings__state">
        <div class="row" role="status">
          <span class="spinner"></span>
          <p class="small">Загружаем находки корпуса…</p>
        </div>
        <div class="stack" style="--gap: var(--s3)">
          {#each [0, 1, 2, 3] as line (line)}
            <span class="skeleton findings__skel"></span>
          {/each}
        </div>
      </Panel>
    {:else if phase === 'failed'}
      <Panel class="findings__state">
        <Notice tone="error" title={FINDINGS_STATE.failedTitle}>
          {guidanceOf(failure)}
        </Notice>
        {#if failure?.kind === 'session'}
          <div class="row">
            <Button href={LOGIN_HREF} variant="action">Войти заново</Button>
          </div>
        {:else}
          <div class="row">
            <Button variant="action" onclick={() => void loadIndex()}>{FINDINGS_ACTION.retry}</Button>
          </div>
        {/if}
      </Panel>
    {:else}
      <!-- Точка входа в отбор стоит над списком: сузить находки нужно до того, как
           человек начал их читать. Порядок состояний один для всего экрана. -->
      {#if querying}
        <Panel class="findings__state">
          <div class="row" role="status">
            <span class="spinner"></span>
            <p class="small">Ищем находки по отбору…</p>
          </div>
          <div class="stack" style="--gap: var(--s3)">
            {#each [0, 1, 2] as line (line)}
              <span class="skeleton findings__skel"></span>
            {/each}
          </div>
        </Panel>
      {:else}
        <!-- Над списком остаётся только отбор: оговорка сервиса о неполном списке
             живёт под списком, рядом со счётчиком, и не вытесняет точку входа в
             отбор за первый экран. -->
        <section class="findings__bar reveal" aria-label="Отбор находок">
          <form class="findings__form" onsubmit={submitFilters}>
            <Field
              label="Субъект"
              name="subject"
              type="search"
              placeholder="часть имени субъекта"
              hint="Готовые имена даёт кнопка «Субъекты корпуса»."
              bind:value={subjectDraft}
              disabled={querying}
            />
            <div class="row">
              <Button type="submit" variant="action" busy={querying}>
                {FINDINGS_ACTION.applyFilter}
              </Button>
              <Button variant="quiet" onclick={resetAll} disabled={querying || !filtered}>
                {FINDINGS_ACTION.resetFilter}
              </Button>
              <Button variant="ghost" icon="list" expanded={subjectsOpen} onclick={toggleSubjects}>
                Субъекты корпуса
              </Button>
            </div>
          </form>

          <div class="findings__statuses" role="group" aria-label="Статус находок">
            <p class="micro findings__status-label">
              Статус: <b>{appliedStatus ? STATUS_SHORT[appliedStatus] : FINDINGS_ACTION.anyStatus}</b>
            </p>
            {#each facets as facet (facet.key)}
              <Chip
                pressed={appliedStatus === facet.key}
                disabled={querying}
                onclick={() => pickFacet(facet.key)}
              >
                {facet.label} <span class="num">{num(facet.count)}</span>
              </Chip>
            {/each}
            <p class="micro muted">{FINDINGS_STATE.countedOver}</p>
          </div>

          {#if appliedParts.length > 0}
            <div class="findings__applied">
              {#each appliedParts as part (part.key)}
                <span class="findings__applied-item">
                  <span class="micro muted">{part.name}</span>
                  <strong class="small findings__applied-value">{part.value}</strong>
                  <Button variant="link" size="sm" onclick={part.clear}>снять</Button>
                </span>
              {/each}
            </div>
            {#if appliedTechRows.length > 0}
              <!-- В строке отбора остаётся человекочитимое название условия, а
                   сырой ключ онтологии и код источника читают под раскрытием. -->
              <details class="svc findings__applied-svc">
                <summary class="micro">Служебные данные отбора</summary>
                {#each appliedTechRows as part (part.key)}
                  <p class="micro svc__row">
                    {part.techName} <code class="code">{part.tech}</code>
                  </p>
                {/each}
              </details>
            {/if}
          {/if}
        </section>

        {#if subjectsOpen && subjectIndex.length > 0}
          <Panel tag="aside" class="findings__subjects reveal">
            <div class="panel__head">
              <div class="grow">
                <h2 class="h4">Субъекты корпуса</h2>
                <!-- Счётчики отдельными подписями: склеенная строка читается как
                     одно число, а их здесь два. -->
                <div class="findings__tally">
                  <p class="micro muted">
                    {countOf(index.length, 'находка', 'находки', 'находок')}
                  </p>
                  <p class="micro muted">
                    {countOf(subjectIndex.length, 'субъект', 'субъекта', 'субъектов')}
                  </p>
                </div>
                <p class="micro muted">{FINDINGS_STATE.countedOver}</p>
              </div>
            </div>

            <Field
              label="Поиск субъекта"
              name="subjectQuery"
              type="search"
              placeholder="имя субъекта или его часть"
              bind:value={subjectQuery}
            />

            {#if subjectChoices.rows.length > 0}
              <div class="subjects">
                {#each subjectChoices.rows as [name, group] (name)}
                  {@const subject = subjectTerm(name)}
                  <button
                    class="subject"
                    type="button"
                    aria-current={appliedSubject === name ? 'true' : undefined}
                    disabled={querying}
                    onclick={() => pickSubject(name)}
                  >
                    <span class="grow">
                      <span class="subject__label">{headingTerm(subject.label)}</span>
                      <!-- Именем строки служебный ключ не становится: он подписью
                           под человеческим названием, чтобы субъекты без имени в
                           словаре различались между собой. -->
                      {#if subject.code}
                        <span class="micro tech">служебное имя: {subject.code}</span>
                      {/if}
                      <span class="micro muted">
                        {countOf(group.length, 'находка', 'находки', 'находок')}
                      </span>
                      <span class="subject__status">
                        {#each statusCounts(group) as status (status.label)}
                          <span class="micro">
                            {status.label} <span class="num">{num(status.count)}</span>
                          </span>
                        {/each}
                      </span>
                    </span>
                  </button>
                {/each}
              </div>

              {#if subjectChoices.all > subjectChoices.rows.length}
                <div class="row findings__more">
                  <Button variant="quiet" size="sm" onclick={showAllSubjects}>
                    {FINDINGS_ACTION.allSubjects}
                  </Button>
                </div>
              {:else if subjectsShown > SUBJECTS_CAP}
                <div class="row findings__more">
                  <Button
                    variant="ghost"
                    size="sm"
                    onclick={() => (subjectsShown = SUBJECTS_CAP)}
                  >
                    {FINDINGS_ACTION.fewSubjects}
                  </Button>
                </div>
              {/if}
            {:else}
              <p class="micro muted">Под это имя субъекта в списке ничего нет.</p>
            {/if}
          </Panel>
        {/if}

        {#if listFailed}
          <!-- Отказ владеет списком: под ним нет ни карточек, ни «пусто», иначе
               сбой прочитался бы как законный пустой результат. Применённый отбор
               виден строкой выше, поэтому здесь остаётся одно действие. -->
          <Panel class="findings__state">
            <Notice tone="error" title={FINDINGS_STATE.failedTitle}>{guidanceOf(failure)}</Notice>
            {#if failure?.kind === 'session'}
              <!-- Повтор запроса с неподтверждённым входом даст тот же отказ:
                   двигает решение только повторный вход. -->
              <div class="row">
                <Button href={LOGIN_HREF} variant="action">Войти заново</Button>
              </div>
            {:else if failure?.kind === 'forbidden'}
              <!-- Повтор отказа по доступу даёт тот же отказ: здесь только то,
                   что действительно двигает решение. -->
              <div class="row">
                <Button href="/account" variant="quiet">Что открыто моему аккаунту</Button>
              </div>
            {:else}
              <div class="row">
                <Button variant="action" onclick={retryList}>{FINDINGS_ACTION.retry}</Button>
              </div>
            {/if}
          </Panel>
        {:else if listed.length > 0}
          <div class="stack findings__list" style="--gap: var(--s4)">
            {#each visible as finding (finding.id)}
              {@const subject = subjectTerm(finding.subject)}
              {@const place = placeOf(finding)}
              <!-- Строка целиком и есть действие: клик, Tab и Enter открывают
                   доказательство с местом в источнике. Кнопка лежит на заголовке
                   утверждения, поэтому доступное имя строки читается как текст
                   находки, а не как «кнопка» без смысла. -->
              <article class="card-note finding">
                <p class="micro finding__subject">
                  {headingTerm(subject.label)}
                  <StatusPill status={finding.status} label={STATUS_SHORT[finding.status]} />
                </p>
                <h3 class="h4 finding__title">
                  <button class="finding__open" type="button" onclick={() => openSource(finding.id)}>
                    {finding.statement}
                  </button>
                </h3>
                <!-- Место в источнике видно самой строкой: где лежит доказательство,
                   видно до открытия шторки. Полное трассирование и фрагмент в ней. -->
                <p class="finding__trail">
                  {#if place}
                    <span class="finding__source">{place.title}</span>
                    {#each place.parts as part (part)}
                      <span class="locator"><Icon name="pin" size={14} /> {part}</span>
                    {/each}
                  {:else}
                    <span class="finding__noev">Доказательств нет</span>
                  {/if}
                  <span class="finding__cta">
                    {FINDINGS_ACTION.openSource}
                    <Icon name="arrowRight" size={16} />
                  </span>
                </p>
              </article>
            {/each}
          </div>

          <!-- Счётчик «Показано N из M» звучит один раз, над списком. Под списком
               остаётся то, чего ещё не видно, и сама кнопка: повторять то же число
               в двух местах нечем. -->
          {#if showMoreVisible || partialNote}
            <div class="pager">
              <div class="pager__note">
                {#if partialNote && !serverNote}
                  <p class="micro muted">{partialNote}</p>
                {/if}
              </div>
              <div class="row">
                {#if showMoreVisible}
                  <!-- Одна кнопка «Показать ещё»: раскрывает пришедшее, а затем тем же
                       нажатием догружает следующую страницу сервера. -->
                  <Button
                    variant="quiet"
                    size="sm"
                    busy={loadingMore}
                    disabled={loadingMore}
                    onclick={showMore}
                  >
                    {FINDINGS_STATE.showMore}
                  </Button>
                {/if}
              </div>
            </div>
          {/if}

          {#if serverNote}
            <!-- Оговорка сервиса о неполном списке живёт под списком: она поясняет
                 счётчик и кнопку сразу над ней, а не вытесняет отбор за первый
                 экран. Данные пришли, но не целиком: это не сбой и не пустой
                 результат, поэтому тон у неё свой, warn. -->
            <div class="findings__note reveal">
              <Notice tone="warn" title={FINDINGS_STATE.incomplete}>
                <div class="stack" style="--gap: var(--s2)">
                  <p class="micro">{FINDINGS_STATE.incompleteBody}</p>
                  <details class="svc">
                    <summary class="micro">Служебные данные списка</summary>
                    <p class="micro svc__row">оговорка сервиса: {serverNote}</p>
                  </details>
                </div>
              </Notice>
            </div>
          {/if}

          {#if loadStalled}
            <!-- Остаток есть, а страница пустая: «Показать ещё» убрана, иначе клик
                 был бы пустым. Действие переезжает сюда, иначе состояние осталось
                 бы без способа сдвинуться с места. -->
            <div class="findings__morefail">
              <Notice tone="warn" title={FINDINGS_STATE.stalledTitle}>
                {FINDINGS_STATE.stalled}
                <div class="row">
                  <Button variant="quiet" size="sm" onclick={retryList}>
                    {FINDINGS_ACTION.retry}
                  </Button>
                </div>
              </Notice>
            </div>
          {/if}

          {#if moreFailure}
            <!-- Отказ дозагрузки не стирает показанное и не выглядит как конец
                 списка: строка под пагинатором называет его отдельно. -->
            <div class="findings__morefail">
              <Notice tone="error" title={FINDINGS_STATE.failedTitle}>{moreFailure}</Notice>
            </div>
          {/if}

          <!-- Импорт ушёл из шапки: на непустом корпусе это фоновая работа, а не
               второе лицо экрана. -->
          <div class="row findings__more">
            <Button variant="ghost" size="sm" icon="upload" expanded={importOpen} onclick={toggleImport}>
              {importOpen ? FINDINGS_ACTION.hideImport : FINDINGS_ACTION.importDocs}
            </Button>
          </div>

          {#if appliedParts.length > 0}
            <!-- Применённый отбор дублируется под списком: видно, чем отсеян
                 результат, и действие «снять» остаётся под рукой. -->
            <div class="findings__applied findings__applied--below reveal">
              {#each appliedParts as part (part.key)}
                <span class="findings__applied-item">
                  <span class="micro muted">{part.name}</span>
                  <strong class="small findings__applied-value">{part.value}</strong>
                  <Button variant="link" size="sm" onclick={part.clear}>снять</Button>
                </span>
              {/each}
            </div>
            {#if appliedTechRows.length > 0}
              <details class="svc findings__applied-svc">
                <summary class="micro">Служебные данные отбора</summary>
                {#each appliedTechRows as part (part.key)}
                  <p class="micro svc__row">
                    {part.techName} <code class="code">{part.tech}</code>
                  </p>
                {/each}
              </details>
            {/if}
          {/if}
        {:else if filtered}
          <Empty
            icon="filter"
            title={FINDINGS_EMPTY.filteredTitle}
            body={FINDINGS_EMPTY.filteredBody}
          >
            {#snippet action()}
              <div class="row">
                <Button variant="action" onclick={resetAll}>{FINDINGS_ACTION.resetFilter}</Button>
              </div>
            {/snippet}
          </Empty>
        {:else if indexTotal === 0}
          <!-- Подтверждённый ноль: корпус пуст честно, и главное действие одно. -->
          <Empty
            icon="layers"
            title={FINDINGS_EMPTY.noCorpusTitle}
            body={FINDINGS_EMPTY.noCorpusBody}
          >
            {#snippet action()}
              <div class="row">
                <Button variant="action" icon="upload" expanded={importOpen} onclick={toggleImport}>
                  {FINDINGS_ACTION.importDocs}
                </Button>
              </div>
            {/snippet}
          </Empty>
        {:else}
          <!-- Ноль без подтверждения и непрогруженный список это сбой загрузки, а
               не пустой корпус: экран обязан различать эти два факта. -->
          <Empty
            icon="alert"
            title={FINDINGS_STATE.failedTitle}
            body={findingsNotLoadedBody(indexTotal)}
          >
            {#snippet action()}
              <div class="row">
                <Button variant="action" onclick={() => void loadIndex()}>{FINDINGS_ACTION.retry}</Button>
              </div>
            {/snippet}
          </Empty>
        {/if}
      {/if}
    {/if}
  </div>
</div>

{#snippet interval(finding: FindingListItem)}
  {@const history = stateOf(finding.id)}
  {@const chain = chainOf(finding.id)}
  {@const subject = subjectTerm(finding.subject)}
  {@const predicate = predicateTerm(finding.predicate)}

  <h3 class="h4">{finding.statement}</h3>

  <dl class="kv">
    <dt>Субъект</dt>
    <dd>{subject.label}</dd>
    <dt>Связь</dt>
    <dd>{predicate.label}</dd>
    <dt>Статус</dt>
    <dd><StatusPill status={finding.status} label={STATUS_SHORT[finding.status]} /></dd>
    <dt>Доступ</dt>
    <dd>{DATA_CLASS_LABELS[finding.data_class]}</dd>
  </dl>

  <!-- Версия извлечения, сырая уверенность и идентификаторы нужны, когда сверяешь
       запись с сервером: они под раскрытием, человеческие имена на виду. -->
  <details class="svc">
    <summary class="micro">Служебные данные</summary>
    <p class="micro svc__row">версия <span class="num">{num(finding.version)}</span></p>
    <p class="micro svc__row">
      уверенность извлечения <span class="num">{pct(finding.confidence)}</span>
    </p>
    <p class="micro svc__row">код утверждения <code class="code">{finding.id}</code></p>
    {#if subject.code}
      <p class="micro svc__row">служебное имя субъекта <code class="code">{subject.code}</code></p>
    {/if}
    {#if predicate.code}
      <p class="micro svc__row">служебное имя связи <code class="code">{predicate.code}</code></p>
    {/if}
  </details>

  {#if describeScope(finding.scope).length > 0}
    <section class="interval__block">
      <p class="field__label">Условия применения</p>
      <ul class="scope">
        {#each describeScope(finding.scope) as line (line)}
          <li>{line}</li>
        {/each}
      </ul>
    </section>
  {/if}

  <section class="interval__block">
    <p class="field__label">
      Числовые наблюдения
      <span class="tag">{countOf(finding.observations.length, 'наблюдение', 'наблюдения', 'наблюдений')}</span>
    </p>
    {#if finding.observations.length > 0}
      {#each finding.observations as obs, i (`${finding.id}-obs-${i}`)}
        {@const m = measureOf(obs)}
        {@const property = propertyTerm(obs.property_name)}
        <div class="measure">
          <div class="measure__caption">
            <span class="measure__prop">{headingTerm(property.label)}</span>
            <!-- Единица уже внутри `m.raw`: словарь собирает «не меньше 95 %» и
                 «95–97 %» целиком, второй подписью единицу не дублируем. -->
            <span class="measure__value">{m.raw}</span>
          </div>
          {#if m.band && m.scale}
            <div class="measure__track">
              <span class="bar">
                <span
                  class="bar__fill {bandClass(finding.status)}"
                  style="left:{m.band.left}%;width:{m.band.width}%"
                ></span>
              </span>
              {#each m.limits as limit, li (`${finding.id}-lim-${li}`)}
                <span
                  class="measure__limit"
                  style="left:{limit.at}%"
                  data-at={limit.label}
                  aria-hidden="true"></span>
              {/each}
            </div>
            <p class="micro measure__scale">
              <span class="num">{num(m.scale.min)}</span>
              <span>{findingsScaleSpread(obs.normalized_unit, m.scale.count)}</span>
              <span class="num">{num(m.scale.max)}</span>
            </p>
          {:else}
            <p class="micro measure__note">{m.note}</p>
          {/if}
          <!-- Сырой текст источника и служебное имя показателя относятся к записи,
               а не к решению: их читают под раскрытием. -->
          <details class="svc">
            <summary class="micro">Служебные данные наблюдения</summary>
            {#if property.code}
              <p class="micro svc__row">
                служебное имя показателя <code class="code">{property.code}</code>
              </p>
            {/if}
            <!-- Приведённая единица та, по которой построена шкала; исходная
                 остаётся для сверки с документом. -->
            {#if obs.unit && obs.unit !== obs.normalized_unit}
              <p class="micro svc__row">единица в источнике <code class="code">{obs.unit}</code></p>
            {/if}
            <p class="micro svc__row">в источнике записано <code class="code">{obs.raw_text}</code></p>
          </details>
        </div>
      {/each}
    {:else}
      <p class="micro">Числовых наблюдений у этого утверждения нет.</p>
    {/if}
  </section>

  <section class="interval__block">
    <p class="field__label">Доказательства: где именно в источнике</p>
    {#if finding.evidence.length > 0}
      {#each finding.evidence as ev, position (`${finding.id}-ev-${position}`)}
        <figure class="evidence">
          <blockquote class="quote">{ev.quote}</blockquote>
          <figcaption class="evidence__loc">
            <b class="small">{ev.source_title}</b>
            {#each locatorParts(ev) as place (place)}
              <span class="locator"><Icon name="pin" size={14} /> {place}</span>
            {/each}
          </figcaption>
        </figure>
      {/each}
      <!-- Коды источников и позиции в тексте остаются доступными для сверки, но
           не спорят с доказательством: они под одним раскрытием. -->
      <details class="svc">
        <summary class="micro">Служебные данные доказательств</summary>
        {#each finding.evidence as ev, position (`${finding.id}-svc-${position}`)}
          <p class="micro svc__row">код источника <code class="code">{ev.document_id}</code></p>
          {#if ev.char_start != null && ev.char_end != null}
            <p class="micro svc__row">
              позиция в тексте: от <span class="num">{num(ev.char_start)}</span> до{' '}
              <span class="num">{num(ev.char_end)}</span> символов
            </p>
          {/if}
        {/each}
      </details>
    {:else}
      <Notice tone="warn" title="Доказательств нет">
        Утверждение не трассируется до источника, поэтому как подтверждённое число оно не
        считается. Оформите правку в разделе «{navLabel('/feedback')}» или дополните корпус
        документом, где этот показатель есть.
      </Notice>
    {/if}
  </section>

  <section class="interval__block">
    <div class="history__head">
      <p class="field__label">История версий утверждения</p>
      <Button
        size="sm"
        variant="quiet"
        disabled={history?.loading}
        onclick={() => void loadHistory(finding.id)}
      >
        {history?.loading ? 'Загружаем историю версий…' : 'Показать историю версий'}
      </Button>
    </div>

    {#if !history}
      <p class="micro">Историю версий этого утверждения ещё не открывали.</p>
    {:else if history.loading}
      <p class="row micro" role="status">
        <span class="spinner spinner--quiet"></span>
        Загружаем историю версий этого утверждения…
      </p>
    {:else if history.error}
      <Notice tone="error" title="История версий не загрузилась">{history.error}</Notice>
    {:else if !chain || chain.versions.length === 0}
      <!-- Пустая история это отдельный факт, а не «версия одна»: иначе ноль
           прочитался бы как подтверждённое отсутствие замен. -->
      <p class="micro">Версий этого утверждения не найдено.</p>
    {:else if chain.versions.length === 1}
      <p class="micro">
        Версия одна (<span class="num">{num(chain.versions[0].version)}</span>), экспертных
        замен этого утверждения ещё не было.
      </p>
    {:else}
      <div class="table-wrap">
        <table class="table">
          <caption class="findings__caption">
            Версий <span class="num">{num(chain.versions.length)}</span>
          </caption>
          <thead>
            <tr>
              <th>версия</th>
              <th>утверждение</th>
              <th>статус</th>
              <th>проверка</th>
            </tr>
          </thead>
          <tbody>
            {#each chain.versions as version (version.finding_id)}
              <tr data-flag={version.superseded_by ? 'out' : undefined}>
                <td class="num">{num(version.version)}</td>
                <td>
                  {version.statement}
                  {#if version.review_reason}
                    <p class="micro">Основание: {version.review_reason}</p>
                  {/if}
                </td>
                <td>
                  <StatusPill status={version.status} label={statusLabel(version.status)} />
                  {#if version.superseded_by}
                    <StatusPill status="superseded" label={STATUS_SUPERSEDED} />
                  {/if}
                </td>
                <td>
                  {#if version.reviewer_id}
                    экспертом
                    {#if version.review_date}
                      <p class="micro">
                        <time datetime={version.review_date}>{ruDate(version.review_date)}</time>
                      </p>
                    {/if}
                  {:else}
                    извлечено из документа
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <details class="svc">
        <summary class="micro">Служебные данные версий</summary>
        <p class="micro svc__row">код исходного утверждения <code class="code">{chain.claim_id}</code></p>
        {#each chain.versions as version (version.finding_id)}
          <p class="micro svc__row">
            версия <span class="num">{num(version.version)}</span>
          </p>
          <p class="micro svc__row">
            код утверждения <code class="code">{version.finding_id}</code>
          </p>
          {#if version.reviewer_id}
            <p class="micro svc__row">
              проверил <code class="code">{version.reviewer_id}</code>
            </p>
          {/if}
          {#if version.superseded_by}
            <p class="micro svc__row">
              заменено на <code class="code">{version.superseded_by}</code>
            </p>
          {/if}
        {/each}
      </details>
    {/if}
  </section>
{/snippet}

{#if activeFinding}
  <Sheet
    title="Источник находки"
    description="Доказательство с местом в источнике и версии этого утверждения."
    width="840px"
    onclose={closeSource}
  >
    {#snippet footer()}
      <Button variant="quiet" onclick={closeSource}>Закрыть</Button>
    {/snippet}

    {@render interval(activeFinding)}
  </Sheet>
{/if}

<style>
  /* (app)-layout уже отступил на высоту pill-навигации: верх не удваиваем. */
  .page.findings {
    padding-top: var(--s5);
  }

  .findings__count {
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
  }

  .findings__import {
    margin-bottom: var(--s6);
  }

  .findings__state {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    margin-bottom: var(--s6);
  }

  .findings__skel {
    display: block;
    height: 74px;
    border-radius: var(--r-lg);
  }

  /* ── Импорт: drop-zone и список принятых документов ────────────────────── */

  .dropzone {
    position: relative;
    border: 1px dashed var(--line-strong);
    border-radius: var(--r-lg);
    background: var(--surface-raised);
    transition: border-color var(--dur-fast) var(--ease-soft),
      background var(--dur-fast) var(--ease-soft);
  }

  .dropzone:hover,
  .dropzone[data-over='true'] {
    border-color: var(--action-deep);
    background: var(--peach-wash);
  }

  .dropzone__input {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    opacity: 0;
    cursor: pointer;
  }

  .dropzone__input:disabled {
    cursor: progress;
  }

  .dropzone__face {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--s2);
    padding: clamp(var(--s6), 6vw, var(--s8)) var(--s5);
    text-align: center;
    pointer-events: none;
  }

  .dropzone__input:focus-visible ~ .dropzone__face {
    outline: 2px solid var(--action-ink);
    outline-offset: 3px;
    border-radius: var(--r-lg);
  }

  .uploads {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    margin: var(--s5) 0 0;
    padding: 0;
  }

  .upload {
    display: flex;
    align-items: flex-start;
    gap: var(--s4);
    padding: var(--s4);
    border: 1px solid var(--line);
    border-radius: var(--r-md);
    background: var(--surface);
  }

  /* Приёмка: статус плашкой-фоном и цветом знака — как у статусных меток в
     app.css, производных цветов в странице не заводим. */
  .upload[data-state='done'] {
    background: var(--consensus-wash);
  }

  .upload[data-state='error'] {
    background: var(--disputed-wash);
  }

  .upload[data-state='done'] .upload__mark {
    color: var(--consensus);
  }

  .upload[data-state='error'] .upload__mark {
    color: var(--disputed);
  }

  .upload__mark {
    display: grid;
    place-items: center;
    width: 26px;
    height: 26px;
    flex: none;
    color: var(--ink-3);
  }

  .upload__ok {
    color: var(--consensus);
  }

  .upload__err {
    color: var(--disputed);
  }

  /* ── Строка отбора над списком и применённые условия ─────────────────── */

  .findings__note {
    margin-bottom: var(--s4);
  }

  .findings__bar {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    margin-bottom: var(--s5);
    padding: var(--s4) var(--s5);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-lg);
    background: var(--surface-sunk);
  }

  .findings__form {
    display: flex;
    align-items: flex-end;
    gap: var(--s4);
    flex-wrap: wrap;
  }

  /* Поле субъекта тянется, кнопки держат свою ширину: на 1280 строка остаётся
     одной строкой, а не уезжает под список. */
  .findings__form :global(.field) {
    flex: 1 1 260px;
    min-width: 0;
  }

  .findings__statuses {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
    padding-top: var(--s4);
    border-top: 1px solid var(--line-soft);
  }

  .findings__status-label {
    color: var(--ink-3);
    font-weight: 600;
  }

  .findings__status-label b {
    color: var(--ink);
    font-weight: 600;
  }

  /* Каждое применённое условие это отдельная подпись со своим действием, а не
     склейка условий в одну строку. */
  .findings__applied {
    display: flex;
    align-items: center;
    gap: var(--s4);
    flex-wrap: wrap;
  }

  .findings__applied--below {
    margin-top: var(--s5);
    padding-top: var(--s4);
    border-top: 1px solid var(--line);
  }

  .findings__applied-item {
    display: inline-flex;
    align-items: baseline;
    gap: var(--s2);
    min-width: 0;
  }

  /* Значение условия: именем остаётся человекочитимая подпись, а служебное имя
     уходит под раскрытие ниже. */
  .findings__applied-value {
    min-width: 0;
    text-align: left;
  }

  .findings__applied-svc {
    margin-top: var(--s3);
  }

  .findings__applied-item strong {
    font-weight: 600;
    color: var(--ink);
    overflow-wrap: anywhere;
  }

  /* ── Список находок и перечень субъектов ───────────────────────────── */

  /* К списку и к отбору возвращают действие «Показать находки» и раскрытие
     субъектов: отступ учитываем, иначе строка уходит под навигацию. */
  .findings__list,
  .findings__bar,
  .findings__subjects {
    scroll-margin-top: calc(var(--topbar-h) + var(--s4));
  }

  .findings__subjects {
    margin-bottom: var(--s5);
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  /* Счётчики панели субъектов: каждое число со своей подписью, а не склейка
     двух чисел одной строкой. */
  .findings__tally {
    display: flex;
    align-items: baseline;
    gap: var(--s4);
    flex-wrap: wrap;
  }

  .findings__more {
    justify-content: flex-start;
    margin-top: var(--s2);
  }

  /* Список субъектов растёт вместе с панелью: единственным скроллом остаётся
     страница, второй прокрутки внутри панели не заводим. */
  .subjects {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  .subject {
    display: flex;
    align-items: flex-start;
    gap: var(--s3);
    width: 100%;
    min-height: 44px;
    padding: var(--s3) var(--s4);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-md);
    background: var(--surface-raised);
    color: var(--ink-2);
    text-align: left;
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-soft),
      border-color var(--dur-fast) var(--ease-soft);
  }

  .subject:hover:not(:disabled) {
    border-color: var(--line-strong);
    background: var(--surface-sunk);
    color: var(--ink);
  }

  .subject[aria-current='true'] {
    border-color: var(--sage-deep);
    background: var(--sage);
    color: var(--ink);
  }

  .subject:disabled {
    opacity: 0.55;
    cursor: progress;
  }

  .subject > .grow {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
  }

  .subject__label {
    font-size: var(--t-small);
    line-height: var(--lh-dense);
  }

  .subject__status {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s1) var(--s3);
    color: var(--ink-3);
  }

  /* ── Строки находок ────────────────────────────────────────────────────── */

  /* Карточка владеет одним действием: кнопка открыта на всю строку через
     ::after, поэтому клик и Enter ведут к доказательству, а не только к
     подписи в подсказке. */
  .finding {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  /* Субъект и статус одной строкой: по чему находку выбирают и согласована ли
     она. Сырой ключ онтологии здесь не печатается: он в «Служебных данных». */
  .finding__subject {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s3);
    flex-wrap: wrap;
    color: var(--ink-3);
    font-weight: 600;
    overflow-wrap: anywhere;
  }

  .finding__title {
    text-wrap: pretty;
  }

  /* Доступное имя кнопки равно тексту утверждения: строка озвучивается как
     находка, а не как «кнопка». */
  .finding__open {
    padding: 0;
    border: 0;
    background: none;
    color: inherit;
    text-align: left;
    cursor: pointer;
  }

  .finding__open::after {
    content: '';
    position: absolute;
    inset: 0;
  }

  /* След находки в источнике: документ, место и подсказка действия. */
  .finding__trail {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    flex-wrap: wrap;
    margin: 0;
    padding-top: var(--s3);
    border-top: 1px solid var(--line-soft);
  }

  .finding__source {
    font-size: var(--t-micro);
    font-weight: 600;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }

  .finding__cta {
    display: inline-flex;
    align-items: center;
    gap: var(--s2);
    margin-inline-start: auto;
    color: var(--action-ink);
    font-size: var(--t-micro);
    font-weight: 600;
  }

  @media (hover: hover) {
    .finding:hover .finding__cta {
      text-decoration: underline;
    }
  }

  /* Находка без доказательства не считается подтверждённым числом: карточка
     говорит об этом текстом, а не цветом. */
  .finding__noev {
    color: var(--disputed);
    font-weight: 600;
  }

  .findings__caption {
    padding: var(--s3) var(--s4);
    background: var(--surface-raised);
    color: var(--ink-3);
    font-size: var(--t-micro);
    text-align: left;
  }

  .pager {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s3);
    flex-wrap: wrap;
    padding-top: var(--s4);
    border-top: 1px solid var(--line);
  }

  /* Счётчик и подпись неполноты это отдельные строки, а не склейка в одну. */
  .pager__note {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
  }

  .pager .micro {
    font-variant-numeric: tabular-nums;
  }

  /* Отказ дозагрузки и тупик стоят под пагинатором и не липнут к нему. */
  .findings__morefail {
    margin-top: var(--s4);
  }

  /* ── Шторка источника утверждения ───────────────────────────────────────── */

  .interval__block {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    padding-top: var(--s4);
    border-top: 1px solid var(--line-soft);
  }

  .scope {
    list-style: none;
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2) var(--s5);
    margin: 0;
    padding: 0;
  }

  .scope li {
    font-size: var(--t-small);
    color: var(--ink-2);
  }

  .measure {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    padding: var(--s4);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-md);
    background: var(--surface-raised);
  }

  .measure__caption {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .measure__prop {
    font-size: var(--t-small);
    font-weight: 600;
    color: var(--ink);
  }

  .measure__value {
    font-family: var(--font-data);
    font-size: var(--t-h4);
    color: var(--ink);
  }

  .measure__track {
    position: relative;
    padding-bottom: var(--s6);
  }

  .measure__limit {
    position: absolute;
    top: -4px;
    width: 2px;
    height: 16px;
    border-radius: var(--r-pill);
    background: var(--ink-2);
    transform: translateX(-50%);
  }

  .measure__limit::after {
    content: attr(data-at);
    position: absolute;
    top: 18px;
    left: 50%;
    transform: translateX(-50%);
    font-family: var(--font-data);
    font-size: var(--t-micro);
    color: var(--ink-3);
    white-space: nowrap;
  }

  .measure__scale {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--s3);
    /* Подпись шкалы живёт между двумя числами: на узком экране она переносится,
       а не растягивает шторку. */
    flex-wrap: wrap;
    /* Подпись шкалы — текст с числами: моно остаётся самим числам (`.num`). */
    font-variant-numeric: tabular-nums;
  }

  .measure__note {
    text-wrap: pretty;
  }

  .evidence {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    margin: 0;
  }

  .evidence blockquote {
    margin: 0;
  }

  .evidence__loc {
    display: flex;
    align-items: center;
    gap: var(--s4);
    flex-wrap: wrap;
  }

  /* Служебные данные: код утверждения, код документа и контрольную сумму
     печатаем целиком — перенос вместо обрезки, иначе полное значение негде
     прочесть. */
  .svc .code,
  .kv dd {
    overflow-wrap: anywhere;
  }

  /* Коды не спорят с содержимым находки: они под раскрытием и подписью, а не
     заголовком строки. */
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

  /* Код различает моноширинный шрифт, а не третий план цвета: на тексте 13px
     `--ink-4` даёт 3.54:1 вместо нормы 4.5:1. */
  .svc .code {
    color: var(--ink-3);
  }

  .svc__row {
    color: var(--ink-3);
  }

  .history__head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  tr[data-flag='out'] td {
    background: var(--superseded-wash);
  }

  @media (pointer: coarse), (max-width: 640px) {
    /* «Служебные данные» трогают пальцем: одна строка микротекста была бы целью
       ниже 44px, поэтому раскрытию добавлен запас по вертикали. */
    .svc summary {
      padding-block: var(--s4);
    }
  }

  @media (max-width: 640px) {
    /* На мобильном строка отбора и применённые условия встают в столбик: поле
       поиска, чипы статуса и действия не толкаются друг с другом. */
    .findings__bar,
    .findings__applied {
      align-items: flex-start;
      flex-direction: column;
      gap: var(--s3);
    }

    .findings__form .row {
      width: 100%;
    }
  }
</style>
