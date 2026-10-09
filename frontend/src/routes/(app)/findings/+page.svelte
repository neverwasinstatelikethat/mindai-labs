<script lang="ts">
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import { tick } from 'svelte';
  import { ApiError, api } from '$lib/api';
  import { navLabel } from '$lib/nav';
  import { gapNotesOf, topicGroups, type GapKind } from '$lib/numbers';
  import { countOf, num, pct } from '$lib/format';
  import { observeReveals } from '$lib/reveal';
  import { session } from '$lib/sessionStore.svelte';
  import SourceRef from '$lib/ui/SourceRef.svelte';
  import {
    FINDINGS_ACTION,
    FINDINGS_EMPTY,
    FINDINGS_LINK_MISS,
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
    hypothesisKindLabel,
    findingsNotLoadedBody,
    findingsPartialNote,
    findingsScaleGapNote,
    findingsScaleSpread,
    findingsShownOf,
    findingsTerm,
    headingTerm,
    knownTerm,
    unitKey,
  } from '$lib/terms';
  import {
    DATA_CLASS_PHRASES,
    type ClaimHistory,
    type DocumentReceipt,
    type Evidence,
    type FindingApiStatus,
    type FindingListItem,
    type HypothesisSignal,
    type NumericObservation,
  } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Disclosure from '$lib/ui/Disclosure.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Field from '$lib/ui/Field.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import Segmented from '$lib/ui/Segmented.svelte';
  import Sheet from '$lib/ui/Sheet.svelte';
  import { goto } from '$app/navigation';
  import InfoDot from '$lib/ui/InfoDot.svelte';
  import VerdictSheet from '$lib/ui/VerdictSheet.svelte';
  import HypothesisCard from '$lib/ui/HypothesisCard.svelte';
  // Фасет склеек читает свой словарь: экран тот же, что и у находок, поэтому и
  // состояния, и доступ описываются словами очереди, а не второй копией.
  import {
    mergeNotLoadedBody,
    mergeOutcome,
    mergeWaiting,
    MERGE_PAIR_NOUNS,
    MERGE_STATUS_LABELS,
    MERGE_STATUS_TONE,
    RESOLUTION_ACTION,
    RESOLUTION_FAILURE,
    RESOLUTION_GATE,
    RESOLUTION_HINTS,
    RESOLUTION_PAGE,
    RESOLUTION_WORDS,
    shownSentenceOf,
  } from '$lib/terms';
  import type { EntityMergeProposal, FeedbackResult, MergeReviewAction } from '$lib/types';

  // ── Находки: отбор, список, импорт
  // PAGE_SIZE раскрывает список по частям на экране, SERVER_PAGE — сколько находок
  // приходит одним запросом: сервер режет список страницей, и полное число стоит
  // в заголовке ответа, а не в длине массива.
  const PAGE_SIZE = 25;
  const SERVER_PAGE = 200;
  const HYPOTHESES_PAGE = 50;
  // Список субъектов — тоже список: у него свой потолок и «Показать все».
  const SUBJECTS_CAP = 12;

  const STATUS_ORDER: FindingApiStatus[] = ['consensus', 'disputed', 'hypothesis'];

  const ACCEPT_ATTR = '.pdf,.docx,.xlsx,.json,.txt';
  const ACCEPT_NOTE = 'Подходят PDF, DOCX, XLSX, JSON и TXT.';

  // Возврат после входа — на этот же экран, а не в раздел по умолчанию:
  // сессия прервалась посреди отбора, и сбрасывать его незачем.
  const LOGIN_HREF = $derived(`/login?next=${encodeURIComponent(page.url.pathname)}`);

  const sourceRequest = $derived.by(() => {
    const params = page.url.searchParams;
    const signalId = params.get('hypothesis')?.trim() ?? '';
    const rawEvidenceIndex = params.get('evidence');
    if (!signalId && rawEvidenceIndex === null) return null;
    const parsedIndex = rawEvidenceIndex === null ? Number.NaN : Number(rawEvidenceIndex);
    return {
      signalId,
      evidenceIndex: Number.isInteger(parsedIndex) && parsedIndex >= 0 ? parsedIndex : null,
    };
  });

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
  function subjectTerm(key: string | null | undefined): { label: string } {
    return findingsTerm(SUBJECT_LABELS, key, {
      absent: FINDINGS_TERM_NAMES.noSubject,
      unknown: FINDINGS_TERM_NAMES.subject,
    });
  }

  function predicateTerm(key: string | null | undefined): { label: string } {
    return findingsTerm(PREDICATE_LABELS, key, {
      absent: FINDINGS_TERM_NAMES.noPredicate,
      unknown: FINDINGS_TERM_NAMES.predicate,
    });
  }

  function propertyTerm(key: string | null | undefined): { label: string } {
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
  // человекочитаемое имя для отображения.
  function locatorParts(ev: Evidence): string[] {
    const parts: string[] = [];
    // Источник уже может называть лист своими словами («Лист 12» в таблице
    // приёмки): подпись не ставится второй раз, иначе читается «лист Лист 12».
    const labelled = (label: string, value: string) =>
      value.trim().toLowerCase().startsWith(label) ? value.trim() : `${label} ${value}`;
    if (ev.page != null) parts.push(`страница ${num(ev.page)}`);
    if (ev.sheet) parts.push(labelled('лист', ev.sheet));
    if (ev.cell_range) parts.push(labelled('ячейки', ev.cell_range));
    return parts;
  }

  function scaleKey(property: string, unit: string): string {
    // «mg/L» и «мг/л» — одна шкала: ключ строится по свёрнутой единице, иначе
    // показатель расходится на две полосы с одинаковыми числами.
    return `${property} || ${unitKey(unit)}`;
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
  // «все находки корпуса». По `index` считают фильтры статуса и список субъектов,
  // чтобы отбор не оставлял себя без вариантов выбора.
  let index = $state<FindingListItem[]>([]);
  let rows = $state<FindingListItem[]>([]);
  let hypotheses = $state<HypothesisSignal[]>([]);
  let hypothesesQuery = $state('');
  let hypothesesKind = $state('');
  let hypothesesTotal = $state<number | null>(null);
  let hypothesesPhase = $state<'loading' | 'ready' | 'failed'>('loading');
  let hypothesesFailure = $state('');
  let hypothesesLoadingMore = $state(false);
  let hypothesesMoreFailure = $state('');
  let hypothesesServed = $state(0);
  let hypothesesSeq = 0;
  let sourceSignal = $state<HypothesisSignal | null>(null);
  let sourceEvidenceIndex = $state<number | null>(null);
  let sourcePhase = $state<'idle' | 'loading' | 'ready' | 'empty' | 'failed'>('idle');
  let sourceFailure = $state('');
  let sourceRequestSeq = 0;
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

  // Отбор по статусу — тот же жест, что и смена фасета: полоса с явно названным
  // «любой статус». Прежние кнопки quiet↔ink требовали догадаться, что повторное
  // нажатие снимает отбор.
  const statusTabs = $derived<{ value: string; label: string }[]>([
    { value: '', label: 'Любой статус' },
    ...STATUS_ORDER.map((key) => ({ value: key as string, label: STATUS_SHORT[key] })),
  ]);
  const sourceEvidence = $derived(
    sourceSignal && sourceEvidenceIndex !== null
      ? (sourceSignal.evidence[sourceEvidenceIndex] ?? null)
      : null,
  );
  // Типы с русским именем: ключ модели без перевода остаётся служебным и
  // чипом фильтра не становится. Один тип — фильтра нет, выбирать не из чего.
  const hypothesisKinds = $derived.by(() => {
    const named = new Map<string, string>();
    for (const signal of hypotheses) {
      const label = hypothesisKindLabel(signal.kind);
      if (label) named.set(signal.kind, label);
    }
    return [...named]
      .map(([kind, label]) => ({ kind, label }))
      .sort((left, right) => left.label.localeCompare(right.label, 'ru'));
  });
  // Полоса типов гипотезы: «все» называется явно, а не остаётся отсутствием
  // заливы на кнопке.
  const kindTabs = $derived<{ value: string; label: string }[]>([
    { value: '', label: 'Все типы' },
    ...hypothesisKinds.map(({ kind, label }) => ({ value: kind, label })),
  ]);
  const visibleHypotheses = $derived.by(() => {
    const query = hypothesesQuery.trim().toLocaleLowerCase('ru');
    return hypotheses.filter((signal) => {
      if (hypothesesKind && signal.kind !== hypothesesKind) return false;
      if (!query) return true;
      const searchable = [
        signal.statement,
        signal.proposal ?? '',
        ...signal.evidence.flatMap((item) => [item.source_title, item.quote]),
      ]
        .join(' ')
        .toLocaleLowerCase('ru');
      return searchable.includes(query);
    });
  });

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

  // Активный отбор остаётся рядом со списком, чтобы его можно было снять.
  type AppliedPart = {
    key: string;
    name: string;
    value: string;
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
        clear: clearSubject,
      });
    }
    if (appliedStatus) {
      parts.push({
        key: 'status',
        name: 'статус',
        value: STATUS_SHORT[appliedStatus],
        clear: clearStatus,
      });
    }
    if (appliedDocument) {
      parts.push({
        key: 'document',
        name: 'источник',
        value: documentName,
        clear: clearDocument,
      });
    }
    return parts;
  });

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
      // части списка: факт называется заголовком, причины — строкой, а молчаливой
      // пустоты здесь нет.
      linkNote = FINDINGS_LINK_MISS.reasons;
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

  function closeSourceView(): void {
    const url = new URL(page.url);
    for (const key of [...url.searchParams.keys()]) {
      if (key === 'hypothesis' || key === 'evidence') url.searchParams.delete(key);
    }
    void goto(url, { replaceState: true, noScroll: true });
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

  function mergeHypotheses(
    current: HypothesisSignal[],
    incoming: HypothesisSignal[],
  ): HypothesisSignal[] {
    const seen = new Set(current.map((signal) => signal.id));
    return [...current, ...incoming.filter((signal) => !seen.has(signal.id))];
  }

  async function loadSourceCitation(signalId: string, evidenceIndex: number): Promise<void> {
    const sequence = ++sourceRequestSeq;
    sourcePhase = 'loading';
    sourceFailure = '';
    sourceSignal = null;
    sourceEvidenceIndex = evidenceIndex;
    try {
      const signal = await api.hypothesis(signalId);
      if (sequence !== sourceRequestSeq) return;
      sourceSignal = signal;
      sourcePhase = evidenceIndex < signal.evidence.length ? 'ready' : 'empty';
    } catch (reason) {
      if (sequence !== sourceRequestSeq) return;
      sourceFailure = guidanceOf(classify(reason));
      sourcePhase = 'failed';
    }
  }

  function retrySourceCitation(): void {
    const request = sourceRequest;
    if (!request || !request.signalId || request.evidenceIndex === null) return;
    void loadSourceCitation(request.signalId, request.evidenceIndex);
  }

  async function loadHypotheses(): Promise<void> {
    const sequence = ++hypothesesSeq;
    hypothesesPhase = 'loading';
    hypothesesFailure = '';
    hypothesesMoreFailure = '';
    hypothesesLoadingMore = false;
    hypothesesServed = 0;
    try {
      const result = await api.hypotheses(HYPOTHESES_PAGE, 0);
      if (sequence !== hypothesesSeq) return;
      hypotheses = mergeHypotheses([], result.items);
      hypothesesTotal = result.total;
      hypothesesServed = result.items.length;
      hypothesesPhase = 'ready';
    } catch (reason) {
      if (sequence !== hypothesesSeq) return;
      hypotheses = [];
      hypothesesTotal = null;
      hypothesesFailure = guidanceOf(classify(reason));
      hypothesesPhase = 'failed';
    }
  }

  async function loadMoreHypotheses(): Promise<void> {
    if (hypothesesLoadingMore || hypothesesTotal === null || hypothesesServed >= hypothesesTotal) {
      return;
    }
    const sequence = hypothesesSeq;
    hypothesesLoadingMore = true;
    hypothesesMoreFailure = '';
    try {
      const result = await api.hypotheses(HYPOTHESES_PAGE, hypothesesServed);
      if (sequence !== hypothesesSeq) return;
      hypotheses = mergeHypotheses(hypotheses, result.items);
      hypothesesTotal = result.total;
      hypothesesServed += result.items.length;
    } catch (reason) {
      if (sequence === hypothesesSeq) hypothesesMoreFailure = guidanceOf(classify(reason));
    } finally {
      if (sequence === hypothesesSeq) hypothesesLoadingMore = false;
    }
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
      // весь экран, иначе фильтр статуса и счётчик разошлись бы.
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

  // Поле субъекта отправляет набранное, статус задают кнопки: второго
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
    if (accepted) await Promise.all([loadIndex(true), loadHypotheses()]);
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

  // URL передаёт только идентификатор и индекс. Подробности источника выводятся
  // после ACL-проверки detail endpoint, а не из редактируемых query-параметров.
  $effect(() => {
    const request = sourceRequest;
    const state = session.state;
    const allowed = canRead;
    if (!request) {
      sourceRequestSeq += 1;
      sourceSignal = null;
      sourceEvidenceIndex = null;
      sourceFailure = '';
      sourcePhase = 'idle';
      return;
    }
    if (state === 'unknown') {
      sourceSignal = null;
      sourcePhase = 'loading';
      return;
    }
    if (state !== 'authenticated' || !allowed) {
      sourceRequestSeq += 1;
      sourceSignal = null;
      sourceEvidenceIndex = null;
      sourcePhase = 'idle';
      return;
    }
    if (!request.signalId || request.evidenceIndex === null) {
      sourceRequestSeq += 1;
      sourceSignal = null;
      sourceEvidenceIndex = null;
      sourceFailure = 'Ссылка на источник неполная. Откройте цитату из карточки гипотезы.';
      sourcePhase = 'failed';
      return;
    }
    void loadSourceCitation(request.signalId, request.evidenceIndex);
  });

  // Право и сам факт входа приходят с сервера: до /auth/me находки не
  // запрашиваем, иначе экран показал бы «пусто» там, где ещё нет сессии.
  $effect(() => {
    const state = session.state;
    const allowed = canRead;
    if (state === 'unknown') return;
    if (state === 'authenticated' && allowed) {
      void loadIndex();
      void loadHypotheses();
      return;
    }
    index = [];
    rows = [];
    indexTotal = null;
    rowsTotal = null;
    hypothesesSeq += 1;
    hypotheses = [];
    hypothesesTotal = null;
    hypothesesPhase = 'ready';
    hypothesesFailure = '';
    serverNote = '';
    moreFailure = '';
    loadStalled = false;
    active = null;
    phase = 'ready';
  });

  // ── Фасет «Склейки имён» ─────────────────────────────────────────────────
  // Очередь склеек была отдельным разделом: человек уходил из списка, терял
  // отбор и возвращался к тому же утверждению. Теперь это фасет этого экрана, и
  // он читается из адреса (`?facet=merges`), поэтому редирект со старого адреса
  // и ссылка из ответа приводят ровно в нужный фасет со всем отбором.
  const MERGE_WINDOW = 30;
  const MERGE_STEP = 6;

  // Два вида находок стоят первыми: аналитический вывод важнее сырого
  // утверждения, и переключатель даёт выбрать, что читаешь.
  const PANES = ['hypotheses', 'findings', 'numbers', 'gaps', 'merges'] as const;
  type Pane = (typeof PANES)[number];

  const PANE_META: Record<Pane, { eyebrow: string; title: string; lead: string }> = {
    hypotheses: {
      eyebrow: '',
      title: 'Аналитические гипотезы',
      lead: 'Вывод из материалов с основанием и следующим шагом. Цитаты читаются в основаниях.',
    },
    findings: {
      eyebrow: '',
      title: 'Находки корпуса',
      lead: 'Утверждения, которые сервис вычитал из материалов: каждое держит связь с источником и контекстом.',
    },
    numbers: {
      eyebrow: '',
      title: 'Числа',
      lead: 'Один показатель в нескольких материалах: значения, разброс и предел из источника.',
    },
    gaps: {
      eyebrow: '',
      title: 'Пробелы',
      lead: 'Чего не хватает, чтобы числа в материалах стали сравнимыми.',
    },
    merges: {
      eyebrow: 'Проверка названий',
      title: 'Объединение названий',
      lead: 'Проверьте, обозначают ли два названия одно и то же. Решение повлияет на будущие материалы.',
    },
  };

  // Короткие имена фасетов для полосы: заголовок экрана называет раздел целиком,
  // переключатель — одним словом, чтобы пять пилюль читались за беглый взгляд.
  const PANE_TAB_LABELS: Record<Pane, string> = {
    hypotheses: 'Гипотезы',
    findings: 'Находки',
    numbers: 'Числа',
    gaps: 'Пробелы',
    merges: 'Проверка названий',
  };

  // Подписи полосы фасетов живут в одном месте: раньше пять литералов стояли в
  // разметке и расходились с PANE_META при первом же переименовании раздела.
  const PANE_TABS: { value: Pane; label: string; hint: string }[] = PANES.map((key) => ({
    value: key,
    label: PANE_TAB_LABELS[key],
    hint: PANE_META[key].lead,
  }));

  // Пять фасетов одного экрана читаются из адреса: `?facet=findings`,
  // `?facet=numbers`, `?facet=gaps`, `?facet=merges`. Без параметра экран
  // открывается гипотезами; глубокая ссылка на утверждение или документ приводит
  // список находок, иначе ссылка вела бы в фасет, где этого утверждения нет.
  // Неизвестное значение молча возвращается к гипотезам, а редирект со старого
  // раздела приводит в нужный фасет.
  const pane = $derived.by((): Pane => {
    const raw = page.url.searchParams.get('facet');
    if (PANES.includes(raw as Pane)) return raw as Pane;
    const deepLink = page.url.searchParams.has('claim') || page.url.searchParams.has('document');
    return deepLink ? 'findings' : 'hypotheses';
  });
  const paneMeta = $derived(PANE_META[pane]);
  const canReview = $derived(session.can('proposal:review'));

  function setPane(next: Pane): void {
    if (next === pane) return;
    const url = new URL(page.url);
    if (next === 'hypotheses') url.searchParams.delete('facet');
    else url.searchParams.set('facet', next);
    void goto(url, { replaceState: true, noScroll: true });
  }

  const groups = $derived(topicGroups(listed));
  const gaps = $derived(gapNotesOf(listed));

  // Число гипотез читается в шапке экрана, рядом с названием фасета.
  const hypothesesCountText = $derived.by(() => {
    if (hypothesesTotal === null) return '';
    if (hypothesesServed < hypothesesTotal) {
      return `${countOf(hypotheses.length, 'гипотеза', 'гипотезы', 'гипотез')} из ${countOf(
        hypothesesTotal,
        'гипотезы',
        'гипотез',
        'гипотез',
      )}`;
    }
    return countOf(hypothesesTotal, 'гипотеза', 'гипотезы', 'гипотез');
  });

  const GAP_WORD: Record<GapKind, string> = {
    pair: 'нет второго утверждения с этим показателем',
    unit: 'числа приведены в разных единицах',
    value: 'в утверждении нет числа',
    locator: 'нет места в первоисточнике',
  };

  /** Заголовок строки списка: связка и имя объекта из словаря. Сырой ключ
   *  онтологии заголовком не становится — для него есть формулировка находки.
   *  Имя объекта не повторяет надзаголовок строки: когда объект и есть субъект,
   *  в заголовке остаётся одна связка. */
  function rowTitle(finding: FindingListItem, predicate: string, subject: string): string {
    const named = knownTerm(SUBJECT_LABELS, finding.object);
    if (!named) return finding.statement.trim() || predicate;
    if (named === subject) return predicate;
    return `${predicate}: ${named}`;
  }

  /** Число строки: показатель назван один раз. Когда он совпадает со связкой
   *  заголовка, повтор не нужен — остаётся величина. */
  function valueLine(observation: NumericObservation, predicate: string): string {
    const property = propertyTerm(observation.property_name).label;
    const value = valueLabel(observation);
    return property.toLocaleLowerCase('ru') === predicate.toLocaleLowerCase('ru')
      ? value
      : `${property}: ${value}`;
  }

  let mergeItems = $state<EntityMergeProposal[]>([]);
  let mergeTotal = $state<number | null>(null);
  let mergePhase = $state<'idle' | 'loading' | 'ready' | 'failed'>('idle');
  let mergeFailure = $state<{ title: string; body: string } | null>(null);
  let mergePending = $state<Record<string, MergeReviewAction>>({});
  let mergeNote = $state<{ id: string; tone: 'ok' | 'warn' | 'error'; text: string } | null>(null);
  // Сколько строк раскрыто: решение принимают по одной паре, а не пролистывая
  // всю очередь.
  let mergeShown = $state(MERGE_STEP);
  let mergeSeq = 0;

  const mergeVisible = $derived(mergeItems.slice(0, mergeShown));
  const mergeLeft = $derived(mergeTotal === null ? 0 : Math.max(mergeTotal - mergeItems.length, 0));
  const mergeCountText = $derived(shownSentenceOf(mergeVisible.length, mergeTotal, MERGE_PAIR_NOUNS));
  const mergeWaitingText = $derived(
    mergeWaiting(
      mergeItems.filter((row) => row.status === 'proposed' || row.status === 'reverted').length,
    ),
  );

  function nameOr(value: string): string {
    return value.trim().length > 0 ? value.trim() : RESOLUTION_WORDS.nameAbsent;
  }

  function rationaleOf(proposal: EntityMergeProposal): string {
    const text = proposal.rationale.trim();
    return text.length > 0 ? text : RESOLUTION_WORDS.noRationale;
  }

  // Действия берутся из статуса пары: у принятой склейки остаётся откат, у
  // отклонённого решения действий нет, и кнопка «принять» не может висеть там,
  // где склейка уже записана.
  function actionsOf(proposal: EntityMergeProposal): MergeReviewAction[] {
    if (proposal.status === 'proposed') return ['accept', 'reject'];
    if (proposal.status === 'reverted') return ['accept'];
    if (proposal.status === 'accepted') return ['revert'];
    return [];
  }

  function mergeFailureOf(reason: unknown): { title: string; body: string } {
    const failure = classify(reason);
    if (failure.kind === 'forbidden') {
      return { title: RESOLUTION_GATE.title, body: RESOLUTION_FAILURE.deniedBody };
    }
    if (failure.kind === 'session') {
      return { title: RESOLUTION_GATE.anonymousTitle, body: RESOLUTION_GATE.anonymousBody };
    }
    if (failure.kind === 'backend') {
      return { title: RESOLUTION_FAILURE.storageTitle, body: RESOLUTION_FAILURE.storageBody };
    }
    return { title: RESOLUTION_FAILURE.transportTitle, body: RESOLUTION_FAILURE.transportBody };
  }

  /** Окно очереди: показанное при сбое дозагрузки остаётся на экране, иначе
   *  прочитанное выглядело бы пустой очередью. */
  async function loadMerges(append = false): Promise<void> {
    const call = ++mergeSeq;
    if (append) mergeNote = null;
    else mergePhase = 'loading';
    try {
      const result = await api.mergeProposals(MERGE_WINDOW, append ? mergeItems.length : 0);
      if (call !== mergeSeq) return;
      const seen = new Set(mergeItems.map((row) => row.id));
      mergeItems = append
        ? [...mergeItems, ...result.items.filter((row) => !seen.has(row.id))]
        : result.items;
      mergeTotal = result.total;
      mergePhase = 'ready';
      if (append) mergeShown += MERGE_STEP;
    } catch (reason) {
      if (call !== mergeSeq) return;
      if (append) {
        mergeNote = { id: '', tone: 'warn', text: RESOLUTION_FAILURE.more };
      } else {
        mergeFailure = mergeFailureOf(reason);
        mergePhase = 'failed';
      }
    }
  }

  /** Решение по паре заменяется тем, что вернул сервис, а не догадкой экрана. */
  async function decideMerge(
    proposal: EntityMergeProposal,
    action: MergeReviewAction,
  ): Promise<void> {
    if (!canReview || mergePending[proposal.id] !== undefined) return;
    mergePending = { ...mergePending, [proposal.id]: action };
    mergeNote = null;
    try {
      const updated = await api.mergeReview(proposal.id, action);
      mergeItems = mergeItems.map((row) => (row.id === updated.id ? updated : row));
      mergeNote = { id: updated.id, tone: 'ok', text: mergeOutcome(updated.status) };
    } catch (reason) {
      const failure = classify(reason);
      const text =
        failure.kind === 'forbidden'
          ? RESOLUTION_FAILURE.reviewDenied
          : failure.kind === 'backend'
            ? RESOLUTION_FAILURE.reviewStorage
            : RESOLUTION_FAILURE.reviewTransport;
      mergeNote = { id: proposal.id, tone: 'error', text };
    } finally {
      const next = { ...mergePending };
      delete next[proposal.id];
      mergePending = next;
    }
  }

  // Очередь запрашивается только у аккаунта с правом: запрос без права дал бы
  // отказ там, где экран обязан объяснить доступ до списка.
  $effect(() => {
    if (pane !== 'merges' || session.state === 'unknown' || !canReview) return;
    if (mergePhase !== 'idle') return;
    void loadMerges();
  });

  // ── Вердикт по находке ───────────────────────────────────────────────────
  // Отдельного раздела для отзыва нет: правка оформляется там, где человек видит
  // доказательство. Шторка трассы при этом закрывается: две шторки в одном слое
  // спорят за фокус, а строка списка остаётся на месте и открывается снова.
  let verdictFor = $state<FindingListItem | null>(null);
  let verdictNote = $state('');

  function openVerdict(finding: FindingListItem): void {
    verdictFor = finding;
    verdictNote = '';
    active = null;
  }

  function verdictSent(_result: FeedbackResult): void {
    const finding = verdictFor;
    verdictNote = 'Вердикт записан сервисом.';
    verdictFor = null;
    if (finding) active = finding.id;
  }

  // Блоки, которые появляются после ответа сервера, тоже нуждаются в
  // reveal-наблюдателе: без повторного скана они остались бы невидимыми.
  $effect(() => {
    void phase;
    void pane;
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
  <title>{paneMeta.title} — StormIdea</title>
  <meta
    name="description"
    content="Аналитические гипотезы с основанием и следующим шагом; находки корпуса держат связь с источником и контекстом."
  />
</svelte:head>

<div class="page findings">
  <div class="wrap stack" style="--gap: var(--s4)">
    <SectionHead level="1" eyebrow={paneMeta.eyebrow} title={paneMeta.title} lead={paneMeta.lead}>

      <p class="micro findings__count" role="status" aria-live="polite">
        {#if pane === 'merges'}
          <!-- Счётчик очереди живёт там, где очередь действительно прочитана:
               без права разбора «0 пар» выглядело бы как подтверждённая пустота. -->
          {#if canReview}{mergeCountText}{/if}
        {:else if pane === 'hypotheses'}
          <!-- «0 гипотез» в шапке повторял пустое состояние под списком: счётчик
               называет только то, чего не видно, а ноль видно по самому экрану. -->
          {#if hypotheses.length > 0}{hypothesesCountText}{/if}
        {:else}
          {listNote}
        {/if}
      </p>
    </SectionHead>

    <!-- Переключатель фасетов стоит над содержимым: сменить предмет нужно до
         того, как человек начал читать список. Методика экрана за «i», а не
         абзацем под заголовком. -->
    <section class="row reveal" aria-label="Фасет экрана">
      <Segmented
        class="findings__filters"
        items={PANE_TABS}
        label="Раздел находок"
        value={pane}
        onchange={(next) => setPane(next as Pane)}
      />
      <!-- Пояснения стоят у того, что объясняют: у статусов, у счёта покрытия и
           у очереди пар, а не рядом с переключателем фасетов. -->
    </section>

    {#if sourceRequest && session.state === 'authenticated' && canRead}
      <section class="panel findings__source-view" aria-labelledby="source-view-title" aria-busy={sourcePhase === 'loading'}>
        <div class="findings__source-view-head">
          <h2 class="h4" id="source-view-title">Фрагмент источника</h2>
          <Button variant="quiet" size="sm" onclick={closeSourceView}>Закрыть фрагмент</Button>
        </div>
        {#if sourcePhase === 'loading'}
          <p class="small muted" role="status">Проверяем доступ и загружаем цитату…</p>
          <span class="skeleton findings__skel"></span>
        {:else if sourcePhase === 'failed'}
          <Notice tone="warn" title="Цитату не удалось открыть">
            {sourceFailure || 'Повторите запрос из карточки гипотезы.'}
            {#if sourceRequest.signalId && sourceRequest.evidenceIndex !== null}
              <div class="row">
                <Button
                  variant="quiet"
                  size="sm"
                  onclick={retrySourceCitation}
                >
                  Повторить
                </Button>
              </div>
            {/if}
          </Notice>
        {:else if sourcePhase === 'empty'}
          <Empty
            icon="alert"
            title="Цитата не найдена"
            body="В гипотезе нет доказательства с таким номером. Откройте источник из карточки ещё раз."
          />
        {:else if sourcePhase === 'ready' && sourceEvidence}
          <div class="findings__source-detail">
            <p class="small"><strong>{sourceEvidence.source_title || 'Источник без названия'}</strong></p>
            <div class="micro muted findings__source-location">
              {#if sourceEvidence.page != null}<span>страница {sourceEvidence.page}</span>{/if}
              {#if sourceEvidence.sheet}<span>лист {sourceEvidence.sheet}</span>{/if}
              {#if sourceEvidence.cell_range}<span>ячейки {sourceEvidence.cell_range}</span>{/if}
              {#if sourceEvidence.char_start != null && sourceEvidence.char_end != null}
                <span>символы {sourceEvidence.char_start}–{sourceEvidence.char_end}</span>
              {:else if sourceEvidence.char_start != null}
                <span>символы с {sourceEvidence.char_start}</span>
              {:else if sourceEvidence.char_end != null}
                <span>символы до {sourceEvidence.char_end}</span>
              {/if}
            </div>
            <blockquote class="findings__source-quote">{sourceEvidence.quote}</blockquote>
          </div>
        {/if}
      </section>
    {/if}

    {#if pane === 'numbers' && session.state === 'authenticated' && canRead}
      {#if groups.length === 0}
        <Empty title={FINDINGS_EMPTY.noCorpusTitle} body={FINDINGS_EMPTY.noCorpusBody} />
      {:else}
        <ul class="topics">
          {#each groups as group (group.id)}
            <li class="topic">
              <Disclosure
                id={`topic-${group.id}`}
                size="h4"
                title={group.topic.title}
                open={group.findings.length > 1}
              >
                {#snippet aside()}
                  <span class="micro muted">
                    {countOf(group.findings.length, 'утверждение', 'утверждения', 'утверждений')}
                  </span>
                  <span class="micro muted">
                    {countOf(group.sourceCount, 'источник', 'источника', 'источников')}
                  </span>
                  {#if group.headline}
                    <span class="topic__spread">
                      {num(group.headline.lo)}–{num(group.headline.hi)} {group.headline.unit}
                    </span>
                  {/if}
                {/snippet}
                <ul class="topic__points">
                  {#each group.headline?.points ?? [] as point (point.findingId)}
                    <li class="point">
                      <button class="point__value" type="button" onclick={() => openSource(point.findingId)}>
                        {point.text}
                      </button>
                      <span class="point__source">{point.source}</span>
                    </li>
                  {/each}
                </ul>
              </Disclosure>
            </li>
          {/each}
        </ul>
      {/if}
    {/if}

    {#if pane === 'gaps' && session.state === 'authenticated' && canRead}
      {#if gaps.length === 0}
        <p class="micro">Здесь нет чисел, которым не хватало бы второго источника или единицы.</p>
      {:else}
        <ul class="gaps">
          {#each gaps as note (note.id)}
            <li class="gap">
              <span class="gap__topic">{note.topic.title}</span>
              <span class="gap__why">{GAP_WORD[note.kind]}</span>
              {#if note.source}<span class="micro muted">{note.source}</span>{/if}
            </li>
          {/each}
        </ul>
      {/if}
    {/if}


    {#if linkNote}
      <!-- Глубокая ссылка не доехала до утверждения: об этом говорится сразу,
           заголовок называет проверенный факт, а строка перечисляет причины. -->
      <div class="findings__state">
        <Notice tone="warn" title={FINDINGS_LINK_MISS.title}>
          {linkNote}
          <div class="row">
            <Button variant="quiet" size="sm" onclick={() => (linkNote = '')}>К списку находок</Button>
          </div>
        </Notice>
      </div>
    {/if}

    {#if pane === 'merges'}
      <!-- Фасет склеек объясняет доступ до списка и различает состояния чтения
           словами: пустая очередь, пустое окно при подтверждённом числе и отказ
           сервиса выглядят по-разному. -->
      {#if session.state === 'anonymous'}
        <div class="findings__state">
          <Notice tone="warn" title={RESOLUTION_GATE.anonymousTitle}>
            {RESOLUTION_GATE.anonymousBody}
            <div class="row">
              <Button href={LOGIN_HREF} variant="action">{RESOLUTION_GATE.anonymousAction}</Button>
            </div>
          </Notice>
        </div>
      {:else if !canReview}
        <div class="findings__state">
          <Notice tone="warn" title={RESOLUTION_GATE.title}>
            {RESOLUTION_GATE.body}
            <div class="row">
              <Button href="/account" variant="quiet">{RESOLUTION_GATE.checkAction}</Button>
            </div>
          </Notice>
        </div>
      {:else if mergePhase === 'loading'}
        <div class="findings__state">
          <div class="row" role="status" aria-label={RESOLUTION_PAGE.loadingAria}>
            <span class="spinner"></span>
            <p class="small">{RESOLUTION_PAGE.loading}</p>
          </div>
        </div>
      {:else if mergePhase === 'failed' && mergeFailure}
        <div class="findings__state">
          <Notice tone="error" title={mergeFailure.title}>{mergeFailure.body}</Notice>
          <div class="row">
            <Button variant="action" onclick={() => void loadMerges()}>{RESOLUTION_ACTION.readAgain}</Button>
          </div>
        </div>
      {:else if mergeItems.length === 0 && mergeTotal !== null}
        <div class="findings__state">
          {#if mergeTotal > 0}
            <!-- Пустое окно при подтверждённом числе пар это сбой чтения, а не
                 «склеек нет». -->
            <Notice tone="error" title={RESOLUTION_PAGE.notLoadedTitle}>
              {mergeNotLoadedBody(mergeTotal)}
            </Notice>
            <div class="row">
              <Button variant="action" onclick={() => void loadMerges()}>{RESOLUTION_ACTION.readAgain}</Button>
            </div>
          {:else}
            <Empty title={RESOLUTION_PAGE.emptyTitle} body={RESOLUTION_PAGE.emptyBody} />
          {/if}
        </div>
      {:else if mergeItems.length === 0}
        <div class="findings__state">
          <Notice tone="error" title={RESOLUTION_PAGE.notLoadedUnknownTitle}>
            {RESOLUTION_PAGE.notLoadedUnknownBody}
          </Notice>
          <div class="row">
            <Button variant="action" onclick={() => void loadMerges()}>{RESOLUTION_ACTION.readAgain}</Button>
          </div>
        </div>
      {:else}
        <div class="stack findings__list" style="--gap: var(--s4)">
          {#if mergeNote && mergeNote.id === ''}
            <p class="micro" role="status">{mergeNote.text}</p>
          {/if}
          {#if mergeTotal === null}
            <p class="micro muted">{RESOLUTION_PAGE.unknownTotal}</p>
          {/if}
          <div class="row">
            <p class="micro muted">{mergeWaitingText}</p>
            <!-- Один «i» на строку: он объясняет и цену решения, и число
                 уверенности в подписи карточки. Два кружка рядом читались как
                 второй слой техники, а не как ответ на вопрос. -->
            <InfoDot
              title="Что меняет решение по паре"
              body={`${RESOLUTION_HINTS.accept} ${RESOLUTION_HINTS.reject} ${RESOLUTION_HINTS.revert}`}
            />
          </div>
          {#each mergeVisible as proposal (proposal.id)}
            <article class="card-note finding">
              <p class="micro finding__subject">
                {RESOLUTION_WORDS.canonicalMark}
                <span class="status-word" data-status={MERGE_STATUS_TONE[proposal.status]}>
                  {MERGE_STATUS_LABELS[proposal.status]}
                </span>
              </p>
              <h3 class="h4 finding__title">{nameOr(proposal.target)}</h3>
              <p class="small">
                {RESOLUTION_WORDS.aliasMark}: <b>{nameOr(proposal.source)}</b>
              </p>
              <p class="micro muted">{RESOLUTION_WORDS.directionNote}</p>
              <div class="row">
                <InfoDot title={RESOLUTION_WORDS.rationaleTitle} body={rationaleOf(proposal)} />
                {#if proposal.source_id === null || proposal.target_id === null}
                  <span class="micro">{RESOLUTION_WORDS.idsAbsent}</span>
                  <Button href="/graph" variant="link" size="sm">{navLabel('/graph')}</Button>
                {/if}
              </div>
              {#if mergeNote && mergeNote.id === proposal.id}
                <p class="micro" role="status">{mergeNote.text}</p>
              {/if}
              <div class="row">
                {#each actionsOf(proposal) as action (action)}
                  <Button
                    variant={action === 'accept' ? 'action' : action === 'reject' ? 'quiet' : 'ink'}
                    size="sm"
                    busy={mergePending[proposal.id] === action}
                    disabled={mergePending[proposal.id] !== undefined}
                    onclick={() => void decideMerge(proposal, action)}
                  >
                    {mergePending[proposal.id] === action
                      ? RESOLUTION_ACTION.pending
                      : RESOLUTION_ACTION[action]}
                  </Button>
                {:else}
                  <span class="micro muted">{RESOLUTION_WORDS.decided}</span>
                {/each}
              </div>
            </article>
          {/each}

          {#if mergeVisible.length < mergeItems.length}
            <div class="row findings__more">
              <Button variant="quiet" size="sm" onclick={() => (mergeShown += MERGE_STEP)}>
                {FINDINGS_STATE.showMore}
              </Button>
            </div>
          {:else if mergeLeft > 0}
            <div class="row findings__more">
              <Button variant="quiet" size="sm" onclick={() => void loadMerges(true)}>
                {RESOLUTION_ACTION.showMore}
              </Button>
            </div>
          {/if}
        </div>
      {/if}
    {:else if blocked === 'session'}
      <div class="findings__state">
        <Empty
          icon="lock"
          title="Войдите, чтобы открыть факты"
          body="Факты и материалы доступны участникам рабочего пространства."
        >
          {#snippet action()}
            <!-- Второго действия здесь нет: повторный запрос с неподтверждённым
                 входом даст тот же отказ, поэтому ведёт только одна кнопка. -->
            <div class="row">
              <Button href={LOGIN_HREF} variant="action">Войти заново</Button>
            </div>
          {/snippet}
        </Empty>
      </div>
    {:else if blocked === 'permission'}
      <div class="findings__state">
        <Empty
          icon="shield"
          title="Нет доступа к материалам пространства"
          body="Попросите владельца пространства открыть доступ к фактам и связанным материалам."
        >
          {#snippet action()}
            <!-- Одно действие и здесь: повторный запрос ничего не меняет, а
                 переход в панель состояния не открывает доступ. -->
            <div class="row">
              <Button href="/account" variant="quiet">Что открыто моему аккаунту</Button>
            </div>
          {/snippet}
        </Empty>
      </div>
    {:else if pane === 'findings' && phase === 'loading'}
      <div class="findings__state">
        <div class="row" role="status">
          <span class="spinner"></span>
              <p class="small">Загружаем факты…</p>
        </div>
        <div class="stack" style="--gap: var(--s3)">
          {#each [0, 1, 2, 3] as line (line)}
            <span class="skeleton findings__skel"></span>
          {/each}
        </div>
      </div>
    {:else if pane === 'findings' && phase === 'failed'}
      <div class="findings__state">
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
      </div>
    {:else if pane === 'findings'}
      <!-- Точка входа в отбор стоит над списком: сузить находки нужно до того, как
           человек начал их читать. Порядок состояний один для всего экрана. -->
      {#if querying}
        <div class="findings__state">
          <div class="row" role="status">
            <span class="spinner"></span>
            <p class="small">Ищем находки по отбору…</p>
          </div>
          <div class="stack" style="--gap: var(--s3)">
            {#each [0, 1, 2] as line (line)}
              <span class="skeleton findings__skel"></span>
            {/each}
          </div>
        </div>
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
              bind:value={subjectDraft}
              disabled={querying}
            />
            <div class="row">
              <!-- Отбор — служебное действие экрана, кораловый ход занят
                   задачей (ответ, вердикт). -->
              <Button type="submit" variant="ink" busy={querying}>
                {FINDINGS_ACTION.applyFilter}
              </Button>
              {#if filtered}
                <!-- Живой контрол: сброшенное состояние описывает уже сказанное
                     числом, поэтому без отбора кнопки нет. -->
                <Button variant="quiet" onclick={resetAll} disabled={querying}>
                  {FINDINGS_ACTION.resetFilter}
                </Button>
              {/if}
              <Button variant="ghost" icon="list" expanded={subjectsOpen} onclick={toggleSubjects}>
                Объекты и темы
              </Button>
            </div>
          </form>

          <div class="findings__statuses">
            <p class="micro findings__status-label">Статус</p>
            <Segmented
              items={statusTabs}
              label="Статус находок"
              value={appliedStatus}
              onchange={(next) => void runQuery(appliedSubject, next as '' | FindingApiStatus)}
            />
            <InfoDot
              title="Что значат статусы"
              body="Согласуется: несколько источников называют одно число в сопоставимых условиях. Без проверки: утверждение опирается на один источник и с другими не сопоставлено. Оспаривается: по одному субъекту источники дают разные числа, и расхождение видно в доказательствах."
              align="start"
            />
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
          {/if}
        </section>

        {#if subjectsOpen && subjectIndex.length > 0}
          <aside class="panel findings__subjects reveal">
            <div class="panel__head">
              <div class="grow">
                <h2 class="h4">Объекты и темы</h2>
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
          </aside>
        {/if}

        {#if listFailed}
          <!-- Отказ владеет списком: под ним нет ни карточек, ни «пусто», иначе
               сбой прочитался бы как законный пустой результат. Применённый отбор
               виден строкой выше, поэтому здесь остаётся одно действие. -->
          <div class="findings__state">
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
          </div>
        {:else if listed.length > 0}
          <div class="stack findings__list" style="--gap: var(--s4)">
            {#each visible as finding (finding.id)}
              {@const subject = subjectTerm(finding.subject)}
              {@const predicate = predicateTerm(finding.predicate)}
              {@const place = placeOf(finding)}
              {@const subjectShown = headingTerm(subject.label)}
              <!-- Строка целиком и есть действие: клик, Tab и Enter открывают
                   доказательство с местом в источнике. Кнопка лежит на заголовке
                   утверждения, поэтому доступное имя строки читается как текст
                   находки, а не как «кнопка» без смысла. -->
              <article class="card-note finding">
                <p class="micro finding__subject">
                  {subjectShown}
                </p>
                <h3 class="h4 finding__title">
                  <button class="finding__open" type="button" onclick={() => openSource(finding.id)}>
                    {rowTitle(finding, predicate.label, subjectShown)}
                  </button>
                </h3>
                {#if finding.context}
                  <p class="micro muted">{finding.context}</p>
                {/if}
                {#if finding.observations.length > 0}
                  <ul class="finding__values" aria-label="Числовые значения из источника">
                    {#each finding.observations as observation, index (index)}
                      <li class="micro">{valueLine(observation, predicate.label)}</li>
                    {/each}
                  </ul>
                {/if}
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

    {#if pane === 'hypotheses' && session.state === 'authenticated' && canRead}
      <!-- Название фасета и его число читает заголовок экрана: второй заголовок
           внутри фасета повторял бы его. -->
      <section
        class="findings__hypotheses"
        aria-label="Аналитические гипотезы"
        aria-busy={hypothesesPhase === 'loading'}
      >
        {#if hypothesesPhase === 'loading'}
          <!-- Состояния лежат в плоской зоне раздела: панель под ними добавила
               бы вторую рамку внутрь одного экрана. -->
          <p class="small muted">Загружаем аналитические гипотезы…</p>
          <span class="skeleton findings__skel"></span>
        {:else if hypothesesPhase === 'failed'}
          <Notice tone="warn" title="Гипотезы не загрузились">
            {hypothesesFailure || 'Повторите запрос.'}
            <div class="row">
              <Button variant="quiet" size="sm" onclick={() => void loadHypotheses()}>
                Повторить
              </Button>
            </div>
          </Notice>
        {:else if hypotheses.length === 0}
          <Empty
            icon="layers"
            title="Аналитических гипотез пока нет"
            body="Факты и цитаты остаются в каталоге. Здесь появятся только аналитические выводы с основаниями и конкретным следующим шагом."
          >
            {#snippet action()}
              <Button href="/research" variant="action">Задать вопрос</Button>
            {/snippet}
          </Empty>
        {:else}
          <Field
            label="Поиск по гипотезам"
            name="hypothesesQuery"
            type="search"
            placeholder="Тема, вывод или источник"
            bind:value={hypothesesQuery}
          />
          {#if hypothesisKinds.length > 1}
            <div class="findings__hypotheses-kinds">
              <Segmented
                items={kindTabs}
                label="Тип гипотезы"
                value={hypothesesKind}
                onchange={(next) => (hypothesesKind = next)}
              />
            </div>
          {/if}
          {#if visibleHypotheses.length === 0}
            <div class="findings__hypotheses-empty">
              <p class="small muted">По этому поиску гипотезы не найдены.</p>
              <Button
                variant="quiet"
                size="sm"
                onclick={() => {
                  hypothesesQuery = '';
                  hypothesesKind = '';
                }}
              >
                Сбросить поиск и тип
              </Button>
            </div>
          {:else}
            <div class="findings__hypotheses-grid">
              {#each visibleHypotheses as signal (signal.id)}
                <HypothesisCard {signal} />
              {/each}
            </div>
          {/if}
          {#if hypothesesMoreFailure}
            <p class="micro findings__morefail" role="alert">
              Не удалось загрузить следующие гипотезы. {hypothesesMoreFailure}
            </p>
          {/if}
          {#if hypothesesTotal !== null && hypothesesServed < hypothesesTotal}
            <div class="row findings__more">
              <Button
                variant="quiet"
                size="sm"
                disabled={hypothesesLoadingMore}
                onclick={() => void loadMoreHypotheses()}
              >
                {hypothesesLoadingMore ? 'Загружаем…' : 'Показать ещё гипотезы'}
              </Button>
            </div>
          {/if}
        {/if}
      </section>
    {/if}

    {#if importOpen}
      <section class="panel panel--sunk findings__import">
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
                {uploading ? 'Добавляем материалы…' : 'Перетащите документы сюда или выберите файлы'}
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
                      <p class="micro">Добавляем материал…</p>
                    {:else if item.receipt}
                      <p class="micro upload__ok">
                        {item.receipt.status === 'duplicate'
                          ? 'Этот материал уже добавлен'
                          : 'Документ принят'}
                      </p>
                      <p class="micro upload__ok">
                        Извлечено{' '}
                        {countOf(item.receipt.extracted_claims, 'утверждение', 'утверждения', 'утверждений')}
                      </p>
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
      </section>
    {/if}
  </div>
</div>

{#if verdictFor}
  <!-- Вердикт открывается над списком, а шторка трассы в этот момент закрыта:
       две шторки в одном слое спорят за фокус, и Tab переставал ходить по
       диалогу. После записи трасса открывается той же строкой списка. -->
  <VerdictSheet
    subject={headingTerm(subjectTerm(verdictFor?.subject).label)}
    subjectNote={verdictFor?.statement ?? ''}
    findingId={verdictFor?.id ?? null}
    candidates={listed.slice(0, 20)}
    gate={canRead ? '' : FINDINGS_STATE.deniedBody}
    onclose={() => (verdictFor = null)}
    onsent={verdictSent}
  />
{/if}

{#snippet interval(finding: FindingListItem)}
  {@const history = stateOf(finding.id)}
  {@const chain = chainOf(finding.id)}
  {@const subject = subjectTerm(finding.subject)}
  {@const predicate = predicateTerm(finding.predicate)}
  {@const objectLabel = knownTerm(SUBJECT_LABELS, finding.object)}

  <h3 class="h4">{finding.statement}</h3>

  {#if finding.evidence.length > 0}
    <div class="interval__quotes">
      {#each finding.evidence as ev, position (`${finding.id}-ev-${position}`)}
        <SourceRef evidence={ev} quote />
      {/each}
    </div>
  {:else}
    <Notice tone="warn" title="Доказательств нет">
      Утверждение не трассируется до источника.
    </Notice>
  {/if}

  <!-- Четыре разных факта одной строкой читались как одно перечисление: у каждого
       своя подпись, и состояние проверки — слово, а не плашка. -->
  <dl class="facts">
    <div class="fact">
      <dt>О чём</dt>
      <dd>{subject.label}</dd>
    </div>
    <div class="fact">
      <dt>Показатель</dt>
      <!-- Объект называют одной связкой: когда он и есть субъект строки, второе
           имя повторяет первое. -->
      <dd>{predicate.label}{#if objectLabel && objectLabel !== subject.label}: {objectLabel}{/if}</dd>
    </div>
    {#if finding.context}
      <div class="fact">
        <dt>Контекст</dt>
        <dd>{finding.context}</dd>
      </div>
    {/if}
    <div class="fact">
      <dt>Проверка</dt>
      <dd><span class="status-word" data-status={finding.status}>{STATUS_SHORT[finding.status]}</span></dd>
    </div>
    <div class="fact">
      <dt>Доступ</dt>
      <dd>{DATA_CLASS_PHRASES[finding.data_class]}</dd>
    </div>
  </dl>

  <!-- Версия извлечения и код утверждения нужны, когда сверяешь запись с
       сервером: они под раскрытием, человеческие имена на виду. Дробная
       уверенность модели и сырые ключи онтологии обращению в поддержку не
       помогают, поэтому в текст интерфейса не выходят. -->

  {#if describeScope(finding.scope).length > 0}
    <section class="interval__block">
      <h4 class="interval__name">Условия применения</h4>
      <ul class="scope">
        {#each describeScope(finding.scope) as line (line)}
          <li>{line}</li>
        {/each}
      </ul>
    </section>
  {/if}

  {#if finding.observations.length > 0}
    <section class="interval__block">
      <h4 class="interval__name">Числовые наблюдения</h4>
      <!-- Полоса-шкала здесь не рисуется: одно значение не с чем сравнивать, а
           разбой показывает фасет «Числа», где у показателя несколько
           источников. -->
      <ul class="measures">
        {#each finding.observations as obs, i (`${finding.id}-obs-${i}`)}
          {@const property = propertyTerm(obs.property_name)}
          <li class="measure">
            <span class="measure__prop">{headingTerm(property.label)}</span>
            <!-- Единица уже внутри `m.raw`: словарь собирает «не меньше 95 %» и
                 «95–97 %» целиком, второй подписью единицу не дублируем. -->
            <span class="measure__value">{measureOf(obs).raw}</span>
          </li>
        {/each}
      </ul>
    </section>
  {/if}

  {#if verdictNote}
    <p class="micro" role="status">{verdictNote}</p>
  {/if}

  <section class="interval__block">
    <div class="history__head">
      <h4 class="interval__name">История версий утверждения</h4>
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
      <p class="micro">Версия одна.</p>
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
                  <span class="status-word" data-status={version.status}>{statusLabel(version.status)}</span>
                  {#if version.superseded_by}
                    <span class="status-word" data-status="superseded">{STATUS_SUPERSEDED}</span>
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
    {/if}
  </section>
{/snippet}

{#if activeFinding}
  <Sheet title="Источник находки" width="840px" onclose={closeSource}>
    {#snippet footer()}
      <Button variant="ghost" onclick={closeSource}>Закрыть</Button>
      <Button
        variant="quiet"
        onclick={() => {
          if (activeFinding) openVerdict(activeFinding);
        }}>Записать вердикт</Button>
    {/snippet}

    {@render interval(activeFinding)}
  </Sheet>
{/if}

<style>
  .interval__quotes {
    display: grid;
    gap: var(--s4);
  }

  /* ── Фасеты «Числа» и «Пробелы»: плоские строки, ряды разделены одной
     линией, карточек нет: тема и её точки это один объект, а не вложенные. ── */
  .topics,
  .gaps {
    display: grid;
    gap: 0;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  /* Темы разделены расстоянием, а не линией: отделитель здесь только поднимал
     вторую рамку там, где ряд и так назван заголовком. */
  .topic,
  .gap {
    padding-block: var(--s2);
  }

  .topic__spread {
    font-family: var(--font-data);
    font-size: var(--t-small);
    color: var(--ink-2);
    white-space: nowrap;
  }

  .topic__points {
    display: grid;
    gap: var(--s2);
    margin: 0 0 var(--s4);
    padding: 0;
    list-style: none;
  }

  .point {
    display: grid;
    grid-template-columns: minmax(0, auto) minmax(0, 1fr);
    gap: var(--s4);
    align-items: baseline;
  }

  .point__value {
    display: inline-flex;
    align-items: center;
    min-height: 34px;
    padding: var(--s1) var(--s3);
    border: 0;
    border-radius: var(--r-pill);
    background: none;
    font-family: var(--font-data);
    font-size: var(--t-small);
    color: var(--ink);
    text-align: start;
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-soft);
  }

  .point__value:hover {
    background: var(--surface-sunk);
  }

  .point__value:active {
    transform: scale(0.98);
  }

  .point__value:focus-visible {
    outline: 2px solid var(--action-ink);
    outline-offset: 2px;
  }

  .point__source {
    font-size: var(--t-small);
    color: var(--ink-3);
    overflow-wrap: anywhere;
  }

  .gap {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, auto);
    gap: var(--s2) var(--s4);
    align-items: baseline;
    padding: var(--s3) 0;
  }

  .gap__topic {
    font-size: var(--t-body);
    color: var(--ink);
    min-width: 0;
  }

  .gap__why {
    font-size: var(--t-small);
    color: var(--ink-2);
  }

  /* (app)-layout уже отступил на высоту pill-навигации: верх не удваиваем. */
  .page.findings {
    padding-top: var(--s5);
  }

  .findings__count {
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
  }

  /* Фасет гипотез плоский: рамка здесь была бы третьей коробкой на экране
     после панели отбора и карточек списка. */
  .findings__hypotheses {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  .findings__hypotheses-kinds {
    min-width: 0;
  }

  .findings__hypotheses-empty {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--s3);
  }

  .findings__hypotheses-empty p {
    margin: 0;
  }

  .findings__source-view {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    scroll-margin-top: calc(var(--topbar-h) + var(--s4));
  }

  .findings__source-view-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--s4);
    flex-wrap: wrap;
  }

  .findings__source-quote {
    margin: 0;
    padding: var(--s4);
    border-left: 2px solid var(--line-strong);
    border-radius: var(--r-sm);
    background: var(--surface-sunk);
    color: var(--ink-2);
    overflow-wrap: anywhere;
    white-space: pre-wrap;
  }

  .findings__source-location {
    display: flex;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .findings__hypotheses-grid {
    display: grid;
    gap: var(--s5);
  }

  .findings__import {
    /* Панель лежит под своим переключателем в самом низу листа: разделять
       нужно сверху, снизу достаточно поля страницы. */
    margin-top: var(--s5);
  }

  .findings__state {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
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

  /* Строка загруженного файла стоит на утопленной панели приёма, поэтому её
     собственный тон — светлее, а не обведена: рамка добавляла второй край там,
     где достаточно ступени поверхности. */
  .upload {
    display: flex;
    align-items: flex-start;
    gap: var(--s4);
    padding: var(--s4);
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

  /* Панель отбора уже названа утопленным тоном: обводка вокруг неё рисовала
     рамку ради рамки и спорила с панелью, внутри которой она стоит. */
  .findings__bar {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    padding: var(--s4) var(--s5);
    border-radius: var(--r-lg);
    background: var(--surface-sunk);
  }

  .findings__form {
    display: flex;
    align-items: flex-end;
    gap: var(--s4);
    flex-wrap: wrap;
  }

  /* Поле субъекта тянется, но держит свою меру: растянутый на всю ширину поиск
     делает из инструмента главный объект экрана, а кнопки уезжают на край. */
  .findings__form :global(.field) {
    flex: 1 1 260px;
    min-width: 0;
    max-width: 46ch;
  }

  /* Строка отбора статуса отделена расстоянием: линия над ней рисовала границу
     там, где группа и так названа подписью «Статус». */
  .findings__statuses {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
    margin-top: var(--s2);
  }

  .findings__status-label {
    color: var(--ink-3);
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

  /* В плоском списке ряд «ещё» придвигает себя сам, а на рабочей колонке шаг
     уже объявлен родителем: там те же 8px ложились бы сверху 24px шага. */
  .wrap.stack > .findings__more {
    margin-top: 0;
  }

  /* Список субъектов растёт вместе с панелью: единственным скроллом остаётся
     страница, второй прокрутки внутри панели не заводим. */
  .subjects {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  /* Строка субъекта — строка списка: отклик и выбор держатся тоном поля. Рамка
     вокруг каждой строки превращала перечень в стопку карточек, а усиление рамки
     на наведении было бы второй декорацией поверх первой. */
  .subject {
    display: flex;
    align-items: flex-start;
    gap: var(--s3);
    width: 100%;
    min-height: 44px;
    padding: var(--s3) var(--s4);
    border-radius: var(--r-md);
    background: none;
    color: var(--ink-2);
    text-align: left;
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-soft);
  }

  .subject:hover:not(:disabled) {
    background: var(--surface-sunk);
    color: var(--ink);
  }

  .subject[aria-current='true'] {
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

  /* Заголовок находки — единственный вход в её разбор, и читаться он должен
     как вход, а не как статичный текст строки: тот же приём, что на карте. */
  .finding__open:hover {
    color: var(--action-ink);
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .finding__open::after {
    content: '';
    position: absolute;
    inset: 0;
  }

  /* След находки в источнике: документ и место. От отделительной линии ряд
     выиграл бы меньше, чем потерял: строка и так стоит последней в карточке. */
  .finding__trail {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    flex-wrap: wrap;
    margin: 0;
  }

  .finding__source {
    font-size: var(--t-micro);
    font-weight: 600;
    color: var(--ink-2);
    overflow-wrap: anywhere;
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
    margin-top: var(--s4);
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

  /* Блоки шторки разведены расстоянием и подписаны своими заголовками: линия над
     каждым блоком была бы второй структурой поверх типографской. */
  .interval__block {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    margin-top: var(--s5);
  }

  /* Заголовок блока шторки — настоящий h4, а не подпись поля: он делит содержимое
     слоя, и экрану чтения нужно опираться на порядок заголовков. */
  .interval__name {
    margin: 0;
    color: var(--ink);
    font-size: var(--t-small);
    font-weight: 600;
    line-height: var(--lh-dense);
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

  /* Наблюдения — строки списка, а не карточки: внутри шторки источника
     рамка на рамке и читается то, что под ней. */
  .measures {
    display: grid;
    gap: var(--s3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .measure {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--s3) var(--s4);
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
    text-align: end;
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

  @media (max-width: 640px) {
    /* На мобильном строка отбора и применённые условия встают в столбик: поле
       поиска, фильтры и действия не толкаются друг с другом. Выравнивание —
       stretch, а не flex-start: при flex-start дочерний ряд берёт ширину своего
       max-content, и полоса переключателя растягивала документ за вьюпорт. */
    .findings__bar,
    .findings__applied {
      align-items: stretch;
      flex-direction: column;
      gap: var(--s3);
    }

    .findings__form .row {
      width: 100%;
    }
  }
</style>
