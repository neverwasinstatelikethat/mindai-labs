<script lang="ts">
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import { tick } from 'svelte';
  import { ApiError, api } from '$lib/api';
  import { navLabel } from '$lib/nav';
  import { countOf, num, pct, plural } from '$lib/format';
  import { observeReveals } from '$lib/reveal';
  import { session } from '$lib/sessionStore.svelte';
  import {
    FINDINGS_ACTION,
    FINDINGS_SCALE_SPREAD,
    OPERATOR_SYMBOL,
    PREDICATE_LABELS,
    PROPERTY_LABELS,
    STATUS_SHORT,
    STATUS_SUPERSEDED,
    SUBJECT_LABELS,
    describeScope,
    scaleNoteFor,
    termOf,
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
  import Select from '$lib/ui/Select.svelte';
  import Sheet from '$lib/ui/Sheet.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';

  // ── Находки: отбор, список, импорт
  const PAGE_SIZE = 25;

  const STATUS_ORDER: FindingApiStatus[] = ['consensus', 'disputed', 'hypothesis'];

  // Метка одна на весь экран: класс консенсуса читают по-русски, служебный ключ
  // онтологии в списке фильтра не нужен — по нему сверяет сервер.
  const STATUS_OPTIONS = [
    { value: '', label: 'любой' },
    ...STATUS_ORDER.map((key) => ({ value: key, label: STATUS_SHORT[key] })),
  ];

  const ACCEPT_ATTR = '.pdf,.docx,.xlsx,.json,.txt';
  const ACCEPT_NOTE = '.pdf · .docx · .xlsx · .json · .txt';

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
      return 'Находки корпуса открыты не всем аккаунтам — запросите доступ у администратора сервиса.';
    }
    return 'Не удалось прочитать находки корпуса. Проверьте соединение и повторите запрос.';
  }

  // Отказ цепочки версий называется своей строкой: тот же классификатор, но
  // про то, что именно сейчас не прочиталось.
  function historyErrorOf(reason: unknown): string {
    const failure = classify(reason);
    if (failure.kind === 'session') return 'Вход не подтверждён: войдите заново и повторите запрос.';
    if (failure.kind === 'forbidden') return 'Версии этого утверждения открыты при доступе к корпусу.';
    return 'Не удалось прочитать версии утверждения. Повторите запрос.';
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

  // Имя ключа онтологии для интерфейса: русское название. Служебный ключ на
  // экран не выходит — он в подсказке наведения или в «Служебных данных».
  function subjectName(key: string | null | undefined): string {
    return termOf(SUBJECT_LABELS, key);
  }

  function predicateName(key: string | null | undefined): string {
    return termOf(PREDICATE_LABELS, key);
  }

  function propertyName(key: string | null | undefined): string {
    return termOf(PROPERTY_LABELS, key);
  }

  // Служебное имя ключа остаётся подписью при русском названии: наведение или
  // блок «Служебные данные», но не заголовок строки.
  function propHint(key: string): string {
    return `служебное имя показателя: ${key}`;
  }

  function docHint(id: string): string {
    return `код документа: ${id}`;
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

  function locator(ev: Evidence): string {
    if (ev.cell_range) return `${ev.sheet ?? 'лист'} · ${ev.cell_range}`;
    if (ev.sheet) return `лист ${ev.sheet}`;
    return `стр. ${ev.page ?? '—'}`;
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

  // Полный список несёт весь корпус: фасеты и список субъектов считаются по
  // нему, поэтому отбор не оставляет себя же без вариантов выбора.
  let index = $state<FindingListItem[]>([]);
  let rows = $state<FindingListItem[]>([]);
  let subjectDraft = $state('');
  let statusDraft = $state('');
  let appliedSubject = $state('');
  let appliedStatus = $state<'' | FindingApiStatus>('');
  let phase = $state<'loading' | 'ready' | 'failed'>('loading');
  let querying = $state(false);
  let failure = $state<Failure | null>(null);
  let shown = $state(PAGE_SIZE);
  let filtersOpen = $state(false);
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
      // Порядок по русскому имени: список читается как словарь субъектов.
      return subjectName(a[0]).localeCompare(subjectName(b[0]), 'ru');
    });
  });

  // Шкала свойства строится по всем значениям корпуса: ни один предел не берётся
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

  // Отказ опроса — состояние списка, а не «пустой результат»: строка состояния
  // обязана называть его, иначе сбой читается как законная пустота.
  const listFailed = $derived(Boolean(failure) && !querying && phase === 'ready');
  const listNote = $derived.by(() => {
    if (querying) return 'идёт отбор по корпусу';
    if (phase === 'loading') return 'читаем находки…';
    if (phase === 'failed' || failure) return 'находки не пришли';
    return `${countOf(listed.length, 'находка', 'находки', 'находок')} · всего в корпусе ${index.length}`;
  });

  // Разбивка по классам консенсуса вместо декоративных точек: числа настоящие,
  // ничего не обрезается и не прячется от скринридера.
  function statusCounts(group: FindingListItem[]): { label: string; count: number }[] {
    return STATUS_ORDER.map((key) => ({
      label: STATUS_SHORT[key],
      count: group.filter((item) => item.status === key).length,
    }));
  }

  // Текущий отбор одной строкой: её показывают и пустой список, и отказ опроса.
  const appliedEcho = $derived.by(() => {
    const parts: string[] = [];
    if (appliedSubject) parts.push(`субъект «${subjectName(appliedSubject)}»`);
    if (appliedStatus) parts.push(`класс «${STATUS_SHORT[appliedStatus]}»`);
    if (appliedDocument) parts.push(`источник «${documentName}»`);
    return parts.join(' · ') || 'без отбора';
  });

  function measureOf(obs: NumericObservation): Measure {
    const scale = scales.get(scaleKey(obs.property_name, obs.normalized_unit)) ?? null;
    const sign = OPERATOR_SYMBOL[obs.operator];
    const raw =
      obs.operator === 'between' && obs.min_value != null && obs.max_value != null
        ? `${num(obs.min_value)}–${num(obs.max_value)}`
        : obs.value != null
          ? `${sign} ${num(obs.value)}`
          : 'нет значения';

    const values = normValues(obs);
    if (!scale || scale.max <= scale.min || values.length === 0) {
      // Полосы нет, и экран называет причину: нет приведённого числа либо оно
      // встречалось в корпусе одно.
      const note = scaleNoteFor(propertyName(obs.property_name), Boolean(scale));
      return { raw, scale, band: null, limits: [], note };
    }

    const span = scale.max - scale.min;
    const pos = (value: number) => ((value - scale.min) / span) * 100;
    const limits: Measure['limits'] = [];
    let band: Measure['band'] = null;

    if (obs.operator === 'between') {
      const left = pos(obs.normalized_min ?? scale.min);
      const right = pos(obs.normalized_max ?? scale.max);
      band = { left, width: Math.max(right - left, 0) };
      limits.push({ at: left, label: num(obs.min_value ?? obs.normalized_min ?? scale.min) });
      limits.push({ at: right, label: num(obs.max_value ?? obs.normalized_max ?? scale.max) });
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
        label: `${sign === '=' ? '' : `${sign} `}${num(obs.value ?? obs.normalized_value)}`,
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
  // цепочку версий запрашивают сразу, не заставляя искать её отдельной кнопкой.
  // Открытие шторки снимает напоминание о ссылке: экран уже показал, что просили.
  function openSource(id: string): void {
    active = id;
    linkNote = '';
    if (!histories[id]) void loadHistory(id);
  }

  function closeSource(): void {
    active = null;
  }

  // Находки стоят выше отбора и импорта: после применения условия или открытия
  // формы возвращаем чтение к нужному блоку, иначе действие уводит вниз.
  async function revealNode(selector: string): Promise<void> {
    if (!browser) return;
    await tick();
    document.querySelector(selector)?.scrollIntoView({ block: 'start' });
  }

  function toggleFilters(): void {
    filtersOpen = !filtersOpen;
    if (filtersOpen) void revealNode('.findings__filters');
  }

  // Импорт больше не спорит с главным действием экрана в шапке: он открывается
  // кнопкой — из пустого корпуса или из спокойной строки под списком.
  function toggleImport(): void {
    importOpen = !importOpen;
    if (importOpen) void revealNode('.findings__import');
  }

  // Ссылка раскрывается только после прочтения списка: до ответа сервера нечего
  // открывать и нечем фильтровать. Одна попытка на заход — повторное чтение
  // находок после импорта не должно возвращать снятый отбор.
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
      // Ссылка из устаревшего ответа либо из закрытого класса доступа: причина
      // называется словами, а не молчаливой пустотой.
      linkNote =
        'Утверждение из ссылки не читается: возможно, его заменили новой версией или оно закрыто вашим доступом. Ниже — находки корпуса, где тот же факт ищется по субъекту.';
    }
  }

  // После прочтения ссылки адрес перестаёт быть источником истины: сняли отбор —
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

  // Приёмка документа не должна стирать отбор аналитика: при preserveFilters
  // черновики, применённые условия и глубина показа выживают, а находки
  // перечитываются по прежним условиям.
  async function loadIndex(preserveFilters = false): Promise<void> {
    phase = 'loading';
    failure = null;
    try {
      index = await api.findings();
      if (preserveFilters && (appliedSubject || appliedStatus)) {
        rows = await api.findings(appliedSubject || undefined, appliedStatus || undefined);
      } else {
        rows = index;
        if (!preserveFilters) {
          subjectDraft = '';
          statusDraft = '';
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
    appliedSubject = nextSubject;
    appliedStatus = nextStatus;
    shown = PAGE_SIZE;
    active = null;
    try {
      rows = await api.findings(nextSubject || undefined, nextStatus || undefined);
      phase = 'ready';
    } catch (reason) {
      // Строки прежнего отбора не остаются на экране под новым фильтром: их нет
      // в ответе. Отказ владеет списком, а не превращается в «пусто».
      rows = [];
      failure = classify(reason);
      phase = index.length > 0 ? 'ready' : 'failed';
    } finally {
      querying = false;
      filtersOpen = false;
      await revealNode('.findings__list');
    }
  }

  // Повтор ровно тех же условий, а не «перечитать всё»: намерение аналитика
  // сохраняется.
  function retryList(): void {
    void runQuery(appliedSubject, appliedStatus);
  }

  function submitFilters(event: SubmitEvent): void {
    event.preventDefault();
    void runQuery(subjectDraft.trim(), statusDraft as '' | FindingApiStatus);
  }

  function resetAll(): void {
    subjectDraft = '';
    statusDraft = '';
    appliedDocument = '';
    linkNote = '';
    dropLinkParams();
    void runQuery('', '');
  }

  function clearStatus(): void {
    statusDraft = '';
    void runQuery(appliedSubject, '');
  }

  function clearSubject(): void {
    subjectDraft = '';
    void runQuery('', appliedStatus);
  }

  function pickFacet(value: FindingApiStatus): void {
    const next = appliedStatus === value ? '' : value;
    statusDraft = next;
    void runQuery(appliedSubject, next);
  }

  function pickSubject(name: string): void {
    subjectDraft = name;
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
    active = null;
    phase = 'ready';
  });

  // Блоки, которые появляются после ответа сервера, тоже нуждаются в
  // reveal-наблюдателе: без повторного скана они остались бы невидимыми.
  $effect(() => {
    void phase;
    void filtersOpen;
    void importOpen;
    void blocked;
    if (browser) void tick().then(() => observeReveals());
  });
</script>

<svelte:head>
  <title>Находки корпуса — Научный Клубок</title>
  <meta
    name="description"
    content="Числа из документов корпуса: находка с доказательством до страницы, листа или диапазона ячеек, условиями применения и версиями утверждения."
  />
</svelte:head>

<div class="page findings">
  <div class="wrap">
    <SectionHead
      level="1"
      eyebrow="Корпус · находки"
      title="Находки корпуса"
      lead="Найдите число в документе и откройте его источник: цитату со страницей, листом или диапазоном ячеек."
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
            <Button variant="quiet" size="sm" onclick={() => (linkNote = '')}>Понятно, смотреть находки</Button>
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
              <p class="micro">Пришлите документ — его находки появятся в этом же списке.</p>
            </div>
          </div>

          <div
            class="dropzone"
            role="group"
            aria-label="Перетащите файлы сюда или выберите их в проводнике"
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
                {uploading ? 'Загружаем документы в корпус…' : 'Выбрать файлы корпуса или перетащить сюда'}
              </strong>
              <span class="micro code">{ACCEPT_NOTE}</span>
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
                    <p class="small">
                      <strong>{item.name}</strong>
                      <span class="micro muted"> · {bytesLabel(item.size)}</span>
                    </p>
                    {#if item.state === 'queued'}
                      <p class="micro">В очереди — ждёт своей загрузки.</p>
                    {:else if item.state === 'uploading'}
                      <p class="micro">Отправляем документ в корпус…</p>
                    {:else if item.receipt}
                      <p class="micro upload__ok">
                        {item.receipt.status === 'duplicate'
                          ? 'Дубликат: документ уже в корпусе'
                          : 'Документ принят'}
                        · извлечено{' '}
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
                      </details>
                      {#if item.receipt.prompt_truncated}
                        <!-- Хвост документа за бюджетом разбора — не «в корпусе
                             такого нет»: число утверждений из неполного разбора
                             обязано быть помечено. -->
                        <p class="micro">
                          Разбор дошёл не до конца: без внимания осталось
                          {item.receipt.omitted_characters} символов.
                        </p>
                      {/if}
                    {:else if item.failure}
                      <p class="micro upload__err">
                        {#if item.failure.kind === 'session'}
                          Вход не подтверждён: войдите заново и повторите загрузку.
                        {:else if item.failure.kind === 'forbidden'}
                          Документ не принят: доступ к корпусу этому аккаунту не открыт —
                          запросите его у администратора сервиса.
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
          body="Находки корпуса — рабочие данные: без входа в аккаунт они не открываются."
        >
          {#snippet action()}
            <!-- Второго действия здесь нет: повторный опрос с неподтверждённым
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
          body="Находки, карта связей и сравнение открываются при доступе к корпусу — его выдаёт администратор сервиса, на экране его не включить."
        >
          {#snippet action()}
            <div class="row">
              <Button href="/dashboard" variant="quiet">{navLabel('/dashboard')}</Button>
              <Button href="/account" variant="link">Что открыто моему аккаунту</Button>
            </div>
          {/snippet}
        </Empty>
      </Panel>
    {:else if phase === 'loading'}
      <Panel class="findings__state">
        <div class="row" role="status">
          <span class="spinner"></span>
          <p class="small">Читаем находки корпуса…</p>
        </div>
        <div class="stack" style="--gap: var(--s3)">
          {#each [0, 1, 2, 3] as line (line)}
            <span class="skeleton findings__skel"></span>
          {/each}
        </div>
      </Panel>
    {:else if phase === 'failed'}
      <Panel class="findings__state">
        <Notice tone="error" title="Находки не загрузились">
          {guidanceOf(failure)}
        </Notice>
        {#if failure?.kind === 'session'}
          <div class="row">
            <Button href={LOGIN_HREF} variant="action">Войти заново</Button>
          </div>
        {:else}
          <div class="row">
            <Button variant="action" onclick={() => void loadIndex()}>Запросить заново</Button>
            <Button href="/dashboard" variant="quiet">{navLabel('/dashboard')}</Button>
          </div>
        {/if}
      </Panel>
    {:else}
      <!-- Порядок один для всех состояний: сначала находки, под ними применённый
           отбор и список субъектов. Панель фильтров над списком мешала увидеть
           результат раньше, чем его выставили. -->
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
      {:else if listFailed}
        <!-- Отказ владеет списком: под ним нет ни карточек, ни «пусто», иначе
             сбой прочитался бы как законный пустой результат. -->
        <Panel class="findings__state">
          <Notice tone="error" title="Находки не пришли">{guidanceOf(failure)}</Notice>
          {#if filtered}
            <p class="micro muted">Отбор: {appliedEcho}.</p>
          {/if}
          {#if failure?.kind === 'session'}
            <!-- Повтор опроса с неподтверждённым входом даст тот же отказ:
                 двигает решение только повторный вход. -->
            <div class="row">
              <Button href={LOGIN_HREF} variant="action">Войти заново</Button>
            </div>
          {:else if failure?.kind === 'forbidden'}
            <!-- Повтор отказа по доступу даёт тот же отказ: здесь только то,
                 что действительно двигает решение. -->
            <div class="row">
              <Button href="/account" variant="quiet">Что открыто моему аккаунту</Button>
              <Button href="/dashboard" variant="ghost">{navLabel('/dashboard')}</Button>
            </div>
          {:else}
            <div class="row">
              <Button variant="action" onclick={retryList}>Повторить опрос</Button>
              {#if filtered}
                <Button variant="quiet" onclick={resetAll}>Снять отбор</Button>
              {/if}
              <Button variant="ghost" onclick={() => void loadIndex()}>{FINDINGS_ACTION.readAgain}</Button>
            </div>
          {/if}
        </Panel>
      {:else}
        {#if listed.length > 0}
          <div class="stack findings__list" style="--gap: var(--s4)">
            {#each visible as finding (finding.id)}
              <article
                class="card-note finding"
                title={finding.subject ? `служебное имя субъекта: ${finding.subject}` : undefined}
              >
                <p class="micro finding__spo">
                  {#if finding.subject || finding.predicate}
                    <b>{finding.subject ? subjectName(finding.subject) : 'субъект не размечен'}</b>
                    {#if finding.predicate}
                      <span aria-hidden="true"> › </span><b>{predicateName(finding.predicate)}</b>
                    {/if}
                    <span aria-hidden="true"> · </span>
                  {/if}
                  версия <b class="num">{finding.version}</b>
                </p>
                <h3 class="h4 finding__statement">{finding.statement}</h3>
                <div class="finding__tags">
                  <StatusPill status={finding.status} label={STATUS_SHORT[finding.status]} />
                  <span class="tag">класс данных: {DATA_CLASS_LABELS[finding.data_class]}</span>
                  <span class="micro">уверенность извлечения <span class="num">{pct(finding.confidence)}</span></span>
                  <span class="micro">{countOf(finding.evidence.length, 'доказательство', 'доказательства', 'доказательств')}</span>
                  <span class="micro">{countOf(finding.observations.length, 'наблюдение', 'наблюдения', 'наблюдений')}</span>
                </div>
                <div class="finding__foot">
                  <p class="micro muted">
                    {#if active === finding.id}
                      открыто: источник и версии этого утверждения
                    {:else if finding.evidence.length > 0}
                      трассируется до источников:
                      {countOf(finding.evidence.length, 'доказательство', 'доказательства', 'доказательств')}
                    {:else}
                      доказательств нет — как факт утверждение не считается
                    {/if}
                  </p>
                  <Button size="sm" variant="quiet" icon="quote" onclick={() => openSource(finding.id)}>
                    {FINDINGS_ACTION.openSource}
                  </Button>
                </div>
              </article>
            {/each}
          </div>

          <div class="pager">
            <p class="micro">
              показано {countOf(visible.length, 'находка', 'находки', 'находок')} из
              {listed.length} {filtered ? 'по этому отбору' : 'в корпусе'}
            </p>
            <div class="row">
              {#if listed.length > shown}
                <Button variant="quiet" size="sm" onclick={() => (shown += PAGE_SIZE)}>
                  Показать ещё <span class="num">{Math.min(PAGE_SIZE, listed.length - shown)}</span>
                </Button>
              {:else if shown > PAGE_SIZE}
                <Button variant="ghost" size="sm" onclick={() => (shown = PAGE_SIZE)}>
                  Только первые <span class="num">{PAGE_SIZE}</span>
                </Button>
              {/if}
            </div>
          </div>

          <!-- Импорт ушёл из шапки: на непустом корпусе это фоновая работа, а не
               второе лицо экрана. -->
          <div class="row findings__more">
            <Button variant="ghost" size="sm" icon="upload" expanded={importOpen} onclick={toggleImport}>
              {importOpen ? FINDINGS_ACTION.hideImport : FINDINGS_ACTION.importDocs}
            </Button>
          </div>
        {:else if filtered}
          <Empty
            icon="filter"
            title="Под этот отбор находок нет"
            body={`Ни одна находка не подошла под отбор (${appliedEcho}). Имя субъекта достаточно начать писать частями — готовые имена лежат списком под находками.`}
          >
            {#snippet action()}
              <div class="row">
                <Button variant="action" onclick={resetAll}>{FINDINGS_ACTION.resetFilter}</Button>
                <Button variant="quiet" onclick={() => void loadIndex()}>{FINDINGS_ACTION.readAgain}</Button>
              </div>
            {/snippet}
          </Empty>
        {:else if index.length === 0}
          <!-- Пустой корпус честен и остаётся единственным, что выше действий:
               у экрана тогда ровно одно главное действие. -->
          <Empty
            icon="layers"
            title="В корпусе пока нет находок"
            body="Сюда попадают утверждения из документов: число, условие применения и доказательство до страницы, листа или диапазона ячеек. Пришлите первый документ."
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
          <!-- Находки в корпусе есть, а список пуст: это сбой чтения, а не
               пустой корпус — экран обязан различать эти два факта. -->
          <Empty
            icon="alert"
            title="Список пуст, хотя корпус не пуст"
            body={`В корпусе числится ${countOf(index.length, 'находка', 'находки', 'находок')}, но запрос вернул пустой список. Перечитайте находки; если повтор не поможет — это сбой сервиса, а не пустой корпус.`}
          >
            {#snippet action()}
              <div class="row">
                <Button variant="action" onclick={() => void loadIndex()}>{FINDINGS_ACTION.readAgain}</Button>
                <Button href="/dashboard" variant="quiet">{navLabel('/dashboard')}</Button>
              </div>
            {/snippet}
          </Empty>
        {/if}

        {#if filtered || listed.length > 0}
          <!-- Применённый отбор — строкой под результатом: видно, что список
               отсеял, и есть куда вернуться за изменением. -->
          <div class="findings__applied reveal">
            <p class="micro findings__applied-title"><Icon name="filter" size={14} /> Отбор</p>
            <div class="row">
              {#if appliedSubject}
                <Chip pressed onclick={clearSubject}>субъект «{subjectName(appliedSubject)}» · снять</Chip>
              {/if}
              {#if appliedStatus}
                <Chip pressed onclick={clearStatus}>класс «{STATUS_SHORT[appliedStatus]}» · снять</Chip>
              {/if}
              {#if appliedDocument}
                <!-- Фильтр пришёл из адреса («все находки этого источника»):
                     называется человеческим именем файла, код остаётся в подсказке. -->
                <Chip pressed title={docHint(appliedDocument)} onclick={clearDocument}>
                  источник «{documentName}» · снять
                </Chip>
              {/if}
              {#if !filtered}
                <span class="micro muted">без отбора — показаны все находки корпуса</span>
              {/if}
            </div>
            <Button size="sm" variant="quiet" expanded={filtersOpen} onclick={toggleFilters}>
              {filtersOpen ? FINDINGS_ACTION.hideFilter : FINDINGS_ACTION.changeFilter}
            </Button>
          </div>

          {#if filtersOpen}
            <Panel raised class="findings__filters reveal">
              <form class="filters" onsubmit={submitFilters}>
                <div class="filters__grid">
                  <Field
                    label="Субъект"
                    name="subject"
                    type="search"
                    placeholder="напр. обратный осмос"
                    hint="Достаточно части имени субъекта; готовые имена — списком под находками."
                    bind:value={subjectDraft}
                  />

                  <Select
                    label="Класс консенсуса"
                    name="status"
                    hint="Согласуется, оспаривается или гипотеза."
                    bind:value={statusDraft}
                    options={STATUS_OPTIONS}
                  />
                </div>

                <div class="filters__go">
                  <Button type="submit" variant="action" busy={querying}>
                    {querying ? 'Ищем по корпусу…' : FINDINGS_ACTION.applyFilter}
                  </Button>
                  <Button variant="quiet" onclick={resetAll} disabled={querying || !filtered}>
                    {FINDINGS_ACTION.resetFilter}
                  </Button>
                  <p class="micro muted">
                    {#if filtered}
                      по отбору {countOf(rows.length, 'находка', 'находки', 'находок')} · {appliedEcho}
                    {:else}
                      весь корпус — {countOf(index.length, 'находка', 'находки', 'находок')}
                    {/if}
                  </p>
                </div>

                <div class="filters__facets">
                  {#each facets as facet (facet.key)}
                    <Chip
                      pressed={appliedStatus === facet.key}
                      disabled={querying}
                      onclick={() => pickFacet(facet.key)}
                    >
                      {facet.label} · <span class="num">{facet.count}</span>
                    </Chip>
                  {/each}
                  <Chip
                    pressed={!appliedStatus}
                    disabled={querying || !appliedStatus}
                    onclick={clearStatus}
                  >
                    {FINDINGS_ACTION.anyStatus} · <span class="num">{index.length}</span>
                  </Chip>
                </div>
              </form>
            </Panel>
          {/if}
        {/if}

        {#if subjectIndex.length > 0}
          <Panel tag="aside" class="findings__subjects reveal">
            <div class="panel__head">
              <div class="grow">
                <h2 class="h4">Субъекты корпуса</h2>
                <p class="micro muted">Отбор считается по всему корпусу, а не по строкам на экране.</p>
              </div>
              <p class="micro">
                <span class="num">{index.length}</span>
                {plural(index.length, 'находка', 'находки', 'находок')} ·
                <span class="num">{subjectIndex.length}</span>
                {plural(subjectIndex.length, 'субъект', 'субъекта', 'субъектов')}
              </p>
            </div>
            <div class="subjects">
              {#each subjectIndex as [name, group] (name)}
                <button
                  class="subject"
                  type="button"
                  aria-current={appliedSubject === name ? 'true' : undefined}
                  title={name ? `служебное имя: ${name}` : undefined}
                  disabled={querying}
                  onclick={() => pickSubject(name)}
                >
                  <span class="grow">
                    <span class="subject__label">{name ? subjectName(name) : 'субъект не размечен'}</span>
                    <span class="micro muted">
                      <span class="num">{group.length}</span>
                      {plural(group.length, 'находка', 'находки', 'находок')}
                    </span>
                    <span class="subject__status">
                      {#each statusCounts(group) as status (status.label)}
                        <span class="micro">
                          {status.label} <span class="num">{status.count}</span>
                        </span>
                      {/each}
                    </span>
                  </span>
                </button>
              {/each}
            </div>
          </Panel>
        {/if}
      {/if}
    {/if}
  </div>
</div>

{#snippet interval(finding: FindingListItem)}
  {@const history = stateOf(finding.id)}
  {@const chain = chainOf(finding.id)}

  <h3 class="h4">{finding.statement}</h3>

  <dl class="kv">
    <dt>Субъект</dt>
    <dd>
      {#if finding.subject}
        {subjectName(finding.subject)}
      {:else}
        субъект не размечен
      {/if}
    </dd>
    <dt>Предикат</dt>
    <dd>
      {#if finding.predicate}
        {predicateName(finding.predicate)}
      {:else}
        предикат не размечен
      {/if}
    </dd>
    <dt>Версия</dt>
    <dd class="num">{finding.version}</dd>
    <dt>Класс консенсуса</dt>
    <dd><StatusPill status={finding.status} label={STATUS_SHORT[finding.status]} /></dd>
    <dt>Класс данных</dt>
    <dd>{DATA_CLASS_LABELS[finding.data_class]}</dd>
    <dt>Уверенность извлечения</dt>
    <dd class="num">{pct(finding.confidence)}</dd>
  </dl>

  <!-- Идентификаторы не решают, кому верить, но нужны, когда сверяешь запись с
       сервером: они под раскрытием, русские имена — на виду. -->
  <details class="svc">
    <summary class="micro">Служебные данные</summary>
    <p class="micro svc__row">код утверждения <code class="code">{finding.id}</code></p>
    {#if finding.subject}
      <p class="micro svc__row">субъект <code class="code">{finding.subject}</code></p>
    {/if}
    {#if finding.predicate}
      <p class="micro svc__row">предикат <code class="code">{finding.predicate}</code></p>
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
      Числовые наблюдения · <span class="num">{finding.observations.length}</span>
      {plural(finding.observations.length, 'наблюдение', 'наблюдения', 'наблюдений')}
    </p>
    {#if finding.observations.length > 0}
      {#each finding.observations as obs, i (`${finding.id}-obs-${i}`)}
        {@const m = measureOf(obs)}
        <div class="measure">
          <div class="measure__caption">
            <span class="measure__prop" title={propHint(obs.property_name)}>
              {propertyName(obs.property_name)}
            </span>
            <span>
              <span class="measure__value">{m.raw}</span>
              <span class="micro muted"> {obs.unit}</span>
            </span>
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
              <span>
                {FINDINGS_SCALE_SPREAD} · {obs.normalized_unit} · значений
                <span class="num">{m.scale.count}</span>
              </span>
              <span class="num">{num(m.scale.max)}</span>
            </p>
          {:else}
            <p class="micro measure__note">{m.note}</p>
          {/if}
          <p class="micro">из источника: <code class="code">{obs.raw_text}</code></p>
        </div>
      {/each}
    {:else}
      <p class="micro">
        Числовых наблюдений нет — сравнивать число с пределом по этому утверждению нечего.
      </p>
    {/if}
  </section>

  <section class="interval__block">
    <p class="field__label">Доказательства · локатор до страницы, листа, диапазона ячеек</p>
    {#if finding.evidence.length > 0}
      {#each finding.evidence as ev, position (`${finding.id}-ev-${position}`)}
        <figure class="evidence">
          <blockquote class="quote">{ev.quote}</blockquote>
          <figcaption class="evidence__loc" title={docHint(ev.document_id)}>
            <b class="small">{ev.source_title}</b>
            <span class="locator"><Icon name="pin" size={14} /> {locator(ev)}</span>
            {#if ev.page != null && (ev.sheet || ev.cell_range)}
              <span class="locator">стр. <span class="num">{ev.page}</span></span>
            {/if}
            {#if ev.char_start != null}
              <span class="locator">
                симв. <span class="num">{ev.char_start}</span>–<span class="num">{ev.char_end ?? '—'}</span>
              </span>
            {/if}
          </figcaption>
        </figure>
      {/each}
    {:else}
      <Notice tone="warn" title="Доказательств нет">
        Утверждение не трассируется до источника, как факт оно не считается. Оформите правку в
        разделе «Оценка ответов» или дополните корпус документом, где этот показатель есть.
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
        {history?.loading ? 'Читаем цепочку…' : 'Показать цепочку версий'}
      </Button>
    </div>

    {#if !history}
      <p class="micro">Цепочку версий ещё не читали.</p>
    {:else if history.loading}
      <p class="row micro" role="status">
        <span class="spinner spinner--quiet"></span>
        Читаем цепочку версий этого утверждения…
      </p>
    {:else if history.error}
      <Notice tone="error" title="История не получена">{history.error}</Notice>
    {:else if !chain || chain.versions.length === 0}
      <!-- Пустая цепочка — отдельный факт, а не «версия одна»: иначе ноль
           прочитался бы как подтверждённое отсутствие замен. -->
      <p class="micro">
        Версий этого утверждения не найдено: цепочка версий пустая, хотя само
        утверждение в корпусе есть.
      </p>
    {:else if chain.versions.length === 1}
      <p class="micro">
        Версия одна (<span class="num">{chain.versions[0].version}</span>): экспертных замен
        этого утверждения ещё не было.
      </p>
    {:else}
      <div class="table-wrap">
        <table class="table">
          <caption class="findings__caption">
            Версий <span class="num">{chain.versions.length}</span>
          </caption>
          <thead>
            <tr>
              <th>вер.</th>
              <th>утверждение</th>
              <th>класс консенсуса</th>
              <th>проверка</th>
            </tr>
          </thead>
          <tbody>
            {#each chain.versions as version (version.finding_id)}
              <tr data-flag={version.superseded_by ? 'out' : undefined}>
                <td class="num">{version.version}</td>
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
        <p class="micro svc__row">корень цепочки <code class="code">{chain.claim_id}</code></p>
        {#each chain.versions as version (version.finding_id)}
          <p class="micro svc__row">
            версия <span class="num">{version.version}</span> · код утверждения
            <code class="code">{version.finding_id}</code>
            {#if version.reviewer_id}
              · проверил <code class="code">{version.reviewer_id}</code>
            {/if}
            {#if version.superseded_by}
              · заменено на <code class="code">{version.superseded_by}</code>
            {/if}
          </p>
        {/each}
      </details>
    {/if}
  </section>
{/snippet}

{#if activeFinding}
  <Sheet
    title="Источник утверждения"
    description="Дословная цитата, локатор первоисточника и цепочка версий этого утверждения."
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

  /* ── Применённый отбор и форма отбора под списком ───────────────────── */

  .findings__applied {
    margin-top: var(--s6);
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

  .findings__applied-title {
    display: flex;
    align-items: center;
    gap: var(--s2);
    color: var(--ink-3);
    font-weight: 600;
  }

  .findings__applied > .row {
    flex: 1 1 auto;
    min-width: 0;
  }

  .findings__filters {
    margin-top: var(--s4);
  }

  .filters {
    display: flex;
    flex-direction: column;
    gap: var(--s5);
  }

  .filters__grid {
    display: grid;
    gap: var(--s4);
    grid-template-columns: repeat(auto-fit, minmax(min(240px, 100%), 1fr));
    align-items: start;
  }

  .filters__go,
  .filters__facets {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .filters__facets {
    padding-top: var(--s4);
    border-top: 1px solid var(--line-soft);
  }

  /* ── Список находок и перечень субъектов под ним ───────────────────── */

  /* К списку и к форме отбора возвращают действие «Показать находки» и
     «Изменить отбор»: отступ учитываем, иначе строка уходит под навигацию. */
  .findings__list,
  .findings__filters,
  .findings__subjects {
    scroll-margin-top: calc(var(--topbar-h) + var(--s4));
  }

  .findings__subjects {
    margin-top: var(--s6);
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

  .finding {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  .finding__spo {
    display: flex;
    gap: var(--s2);
    flex-wrap: wrap;
    align-items: baseline;
  }

  .finding__spo b {
    color: var(--ink);
    font-weight: 600;
  }

  .finding__statement {
    text-wrap: pretty;
  }

  .finding__tags,
  .finding__foot {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .finding__foot {
    justify-content: space-between;
    padding-top: var(--s3);
    border-top: 1px solid var(--line-soft);
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

  .pager .micro {
    font-variant-numeric: tabular-nums;
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

  /* Коды не спорят с содержимым находки: они под раскрытием и в третьестепенном
     цвете. */
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

  @media (max-width: 640px) {
    /* Строка отбора на мобильном не толкается: чипы и действие «Изменить отбор»
       встают друг под друга. */
    .findings__applied {
      align-items: flex-start;
      flex-direction: column;
      gap: var(--s3);
    }
  }
</style>
