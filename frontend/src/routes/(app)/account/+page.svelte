<script lang="ts">
  import { goto } from '$app/navigation';
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import { tick } from 'svelte';
  import { ApiError, api } from '$lib/api';
  import { dateTime, num } from '$lib/format';
  import { navLabel } from '$lib/nav';
  import { observeReveals } from '$lib/reveal';
  import { session } from '$lib/sessionStore.svelte';
  import {
    ACTIVITY_ACTION_FALLBACK,
    ACTIVITY_ACTION_LABELS,
    ACTIVITY_OUTCOME_LABELS,
    ACTIVITY_OUTCOME_UNKNOWN,
    BASIC_RIGHT_NOTE,
    CORPUS_JOURNAL,
    DECISION_ACTION_LABELS,
    EXPERT_RIGHT_NOTE,
    JOURNAL_NOUNS,
    JOURNAL_WINDOW_WORDS,
    MY_DECISIONS,
    MY_JOURNAL,
    MY_USAGE,
    RIGHT_TASK_NOTES,
    USAGE_WINDOW_LABELS,
    USAGE_WINDOW_OPTIONS,
    decisionDetail,
    journalIncomplete,
    journalLoadMoreOf,
    journalUnloaded,
    knownTerm,
    shownSentenceOf,
  } from '$lib/terms';
  import {
    CAPABILITY_LABELS,
    DATA_CLASS_LABELS,
    type ActivityEntry,
    type AuditEntry,
    type Capability,
    type ExpertDecision,
    type LlmUsageSummary,
  } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Field from '$lib/ui/Field.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';

  // Потолок имени и минимальная длина пароля заданы на сервере (политика
  // профиля и настройки длины пароля) и отдельным полем клиенту не приходят.
  // Значения экрану известны заранее, поэтому проверка идёт до отправки: предел
  // не должен узнаваться из отказа.
  const NAME_MAX = 120;
  const PASSWORD_MIN = 10;

  // Право показывается делом, которое оно открывает: имя строки читается из
  // `CAPABILITY_LABELS` (те же слова, что в навигации), пояснение «что оно
  // даёт» из `RIGHT_TASK_NOTES`, а имя раздела, куда право ведёт, из `navLabel`.
  // Экран ничего не переименовывает сам: строка профиля и пункт меню называют
  // одно и то же. `dest` нужен там, где раздел живёт на этом же экране и в
  // меню навигации его нет.
  type RightRow = { capability: Capability; href: string; dest?: string };

  const BASIC_RIGHTS: RightRow[] = [
    { capability: 'knowledge:read', href: '/findings' },
    { capability: 'query:ask', href: '/research' },
    { capability: 'feedback:give', href: '/feedback' },
    { capability: 'export:run', href: '/compare' },
    { capability: 'evaluation:view', href: '/dashboard' },
  ];

  // Экспертные дела: в читаемой части экрана о них говорит одна формулировка с
  // тем, кто выдаёт право. Служебный ключ права живёт только под «Служебными
  // данными».
  const EXPERT_RIGHTS: RightRow[] = [
    { capability: 'proposal:review', href: '/feedback' },
    { capability: 'restricted:read', href: '/findings' },
    // Журнал корпуса отдельного раздела в меню не имеет: он читается ниже на
    // этом же экране, поэтому ссылка ведёт на блок страницы, а имя блока берётся
    // из словаря ленты.
    { capability: 'audit:read', href: '#corpus-journal', dest: CORPUS_JOURNAL.heading },
  ];

  let nameDraft = $state('');
  // Серверное имя подхватывается в черновик один раз: после ошибки формы ввод
  // должен оставаться нетронутым, иначе человек печатал бы заново.
  let namePrimed = $state(false);
  let nameBusy = $state(false);
  let nameError = $state('');
  let nameConfirmed = $state('');

  let currentPwd = $state('');
  let newPwd = $state('');
  let confirmPwd = $state('');
  let pwdBusy = $state(false);
  let pwdErrors = $state<{ current?: string; next?: string; confirm?: string; form?: string }>({});
  let pwdConfirmed = $state(false);

  let outBusy = $state(false);
  let outError = $state('');

  // Потолок страницы журнала: серверное окно (`limit`/`offset`) режет ленту,
  // полное число подходит в X-Total-Count. Экран называет обе величины, а не
  // выдаёт срез за весь журнал.
  const ACTIVITY_PAGE_SIZE = 50;
  const AUDIT_PAGE_SIZE = 50;

  // Журнал собственных актов читается отдельным запросом: /api/v1/me/activity
  // возвращает только то, что сделал этот аккаунт, — фильтр по автору держит
  // сервер, из адреса запроса он не приходит.
  let activity = $state<ActivityEntry[] | null>(null);
  let activityTotal = $state<number | null>(null);
  let activityOffset = $state(0);
  let activityLoading = $state(false);
  let activityMoreLoading = $state(false);
  let activityError = $state('');
  let activityMoreError = $state('');
  // Пустая страница при положительном остатке: offset не сдвинулся, дочитывать
  // нечего — кнопка тогда звала бы в никуда.
  let activityStalled = $state(false);
  let activitySeq = 0;

  // Технические данные страницы по умолчанию свёрнуты: они нужны при разборе
  // обращения в сервис, а не для решения аналитика.
  let serviceOpen = $state(false);

  const view = $derived.by(() => {
    if (session.state === 'unknown') return 'pending';
    if (session.state === 'anonymous' || !session.account) return 'signed-out';
    return 'ready';
  });

  // Имя раздела, куда ведёт право: слово навигации, а для блока этого экрана
  // слово словаря ленты. Права, у которого раздела нет, остаются без ссылки:
  // якорь в читаемой строке читался бы как служебный код.
  function destOf(item: RightRow): string {
    if (item.dest) return item.dest;
    const label = navLabel(item.href);
    return label === item.href ? '' : label;
  }

  const workSections = $derived(
    BASIC_RIGHTS.map((item) => ({
      ...item,
      label: CAPABILITY_LABELS[item.capability],
      note: RIGHT_TASK_NOTES[item.capability] ?? '',
      to: destOf(item),
      open: session.can(item.capability),
    })),
  );
  const expertSections = $derived(
    EXPERT_RIGHTS.map((item) => ({
      ...item,
      label: CAPABILITY_LABELS[item.capability],
      note: RIGHT_TASK_NOTES[item.capability] ?? '',
      to: destOf(item),
      open: session.can(item.capability),
    })),
  );
  const openCount = $derived(workSections.filter((item) => item.open).length);
  const openExpertCount = $derived(expertSections.filter((item) => item.open).length);

  const journalRows = $derived(activity ?? []);
  // Сколько осталось за показанной страницей: без полного числа сервера это
  // неизвестность, а не ноль; «Показать ещё» тогда не предлагается.
  const activityLeft = $derived(
    activity === null || activityTotal === null
      ? null
      : Math.max(0, activityTotal - journalRows.length),
  );
  const activityUnloaded = $derived(
    journalUnloaded(activityTotal, JOURNAL_NOUNS.entry, MY_JOURNAL.scope)
  );
  const activityPartial = $derived(
    activityTotal === null && activity !== null && activity.length >= ACTIVITY_PAGE_SIZE
      ? journalIncomplete(JOURNAL_NOUNS.entry)
      : '',
  );
  // Идентификаторы объектов нужны только при разборе конкретного акта, поэтому
  // живут под «Служебными данными», а не в строке журнала.
  const journalRefs = $derived(
    journalRows.filter((entry) => Boolean(entry.object_id)).slice(0, 40),
  );
  // Служебный ключ действия остаётся за раскрытием: в строке журнала он бы
  // читался как название, а два неизвестных акта различались бы только им.
  const unknownActions = $derived(
    journalRows.filter((entry) => knownTerm(ACTIVITY_ACTION_LABELS, entry.action) === null).slice(0, 40),
  );

  // Отказ называется своей строкой: что не сохранилось и что сделать. Служебный
  // текст ответа на экран не выводим.
  function messageOf(caught: unknown, fallback: string): string {
    if (caught instanceof ApiError && caught.status === 401) {
      return 'Вход больше не подтверждён: войдите заново и повторите действие.';
    }
    return fallback;
  }

  function ruDay(iso: string): string {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : date.toLocaleDateString('ru-RU', { dateStyle: 'long' });
  }

  // Действие читают по русскому имени. Служебный ключ в строку журнала не
  // ставим: акты, которых нет в словаре, перечислены с ключом под «Служебными
  // данными», и там их можно различить.
  function actionName(action: string): string {
    return knownTerm(ACTIVITY_ACTION_LABELS, action) ?? ACTIVITY_ACTION_FALLBACK;
  }

  function outcomeLabel(outcome: string): string {
    return ACTIVITY_OUTCOME_LABELS[outcome] ?? ACTIVITY_OUTCOME_UNKNOWN;
  }

  // Исход акта кодируется не только цветом: знак и слово читаются вместе.
  function outcomeStatus(outcome: string): 'consensus' | 'disputed' | 'hypothesis' {
    if (outcome === 'success' || outcome === 'allowed') return 'consensus';
    if (outcome === 'denied' || outcome === 'failure') return 'disputed';
    return 'hypothesis';
  }

  // Порции окна могут перекрываться, если корпус между запросами изменился;
  // повтора в список не добавляется. У акта журнала своего id в контракте нет
  // (у корпуса есть), поэтому ключ — содержимое строки.
  function activityKeyOf(entry: ActivityEntry): string {
    return `${entry.created_at}|${entry.actor_id}|${entry.action}|${entry.object_id}|${entry.outcome}`;
  }

  function mergeActivity(current: ActivityEntry[], incoming: ActivityEntry[]): ActivityEntry[] {
    const seen = new Set(current.map(activityKeyOf));
    return [...current, ...incoming.filter((entry) => !seen.has(activityKeyOf(entry)))];
  }

  async function loadActivity(): Promise<void> {
    const call = ++activitySeq;
    activityLoading = true;
    activityError = '';
    try {
      const page = await api.myActivity(ACTIVITY_PAGE_SIZE, 0);
      if (call !== activitySeq) return;
      activity = page.items;
      activityTotal = page.total;
      activityOffset = page.items.length;
      activityMoreError = '';
      // Свежее чтение владеет состоянием окна: тупик дочитывания снимается,
      // даже если прежнее окно остановилось на пустой странице.
      activityStalled = false;
    } catch (caught) {
      if (call !== activitySeq) return;
      // Сбой журнала — не «вы ничего не делали»: отказ называет себя отдельно
      // от пустого списка и от неполного чтения.
      activity = null;
      activityTotal = null;
      activityOffset = 0;
      activityStalled = false;
      activityError = messageOf(caught, MY_JOURNAL.failedBody);
    } finally {
      if (call === activitySeq) activityLoading = false;
    }
  }

  // Дочитывание по серверному offset: прочитанное остаётся на экране, отказ
  // владеет только своей строкой. Смещение идёт по строкам ответа, а не по
  // локальной длине списка: перекрывающиеся порции не должны застрять.
  async function loadActivityMore(): Promise<void> {
    if (activity === null) return;
    const call = ++activitySeq;
    const offset = activityOffset;
    activityMoreLoading = true;
    activityMoreError = '';
    try {
      const page = await api.myActivity(ACTIVITY_PAGE_SIZE, offset);
      if (call !== activitySeq) return;
      activityOffset = offset + page.items.length;
      activity = mergeActivity(activity, page.items);
      // Пустая страница — не сбой и не конец журнала: записей сервис не прислал,
      // а остаток не ушёл. Ставим тупик; дубликаты его не снимают — там offset
      // сдвинулся, и следующий клик действительно продвигает чтение.
      activityStalled = page.items.length === 0;
      if (page.total !== null) activityTotal = page.total;
    } catch {
      if (call === activitySeq) activityMoreError = JOURNAL_WINDOW_WORDS.moreFailed;
    } finally {
      if (call === activitySeq) activityMoreLoading = false;
    }
  }

  // Журнал корпуса — то, ради чего существует экспертное право на чтение
  // действий: без него обещание «читать действия всех аккаунтов» нечем было бы
  // проверить. Секция читается только держателям права, остальным она доступ не
  // обещает.
  let corpusJournal = $state<AuditEntry[] | null>(null);
  let corpusTotal = $state<number | null>(null);
  let corpusOffset = $state(0);
  let corpusLoading = $state(false);
  let corpusMoreLoading = $state(false);
  let corpusError = $state('');
  let corpusMoreError = $state('');
  // Тот же тупик, что у ленты своих актов: пустая страница при неотличимом
  // остатке не даёт смещению сдвинуться.
  let corpusStalled = $state(false);
  let corpusSeq = 0;

  const canAudit = $derived(session.can('audit:read'));

  // Разделы этого экрана одним списком: человек видит на первом экране, что здесь
  // есть, и идёт к нужному без прокрутки вслепую. Журнал корпуса появляется в
  // списке только там, где на него есть экспертное право, а заголовок секции и
  // ссылка читают одну и ту же строку: разойтись им негде.
  const pageSections = $derived.by(() => {
    const rows = [
      { id: 'account-who', title: 'Кто вы' },
      { id: 'account-rights', title: 'Что вам открыто' },
      { id: 'account-journal', title: MY_JOURNAL.heading },
      { id: 'account-decisions', title: MY_DECISIONS.heading },
      { id: 'account-usage', title: MY_USAGE.heading },
    ];
    if (canAudit) rows.push({ id: 'corpus-journal', title: CORPUS_JOURNAL.heading });
    rows.push(
      { id: 'account-password', title: 'Смена пароля' },
      { id: 'account-signout', title: 'Выход' },
    );
    return rows;
  });

  function sectionTitle(id: string): string {
    return pageSections.find((row) => row.id === id)?.title ?? '';
  }

  // Право на экране называется одним словом со списком ниже: экспертное право
  // открыто либо его нет. Про «уровни доступа» экран не говорит.
  const rightLabel = $derived(
    session.account?.review_enabled ? 'экспертное право открыто' : 'экспертного права нет',
  );

  const corpusLeft = $derived(
    corpusJournal === null || corpusTotal === null
      ? null
      : Math.max(0, corpusTotal - corpusJournal.length),
  );
  const corpusUnloaded = $derived(
    journalUnloaded(corpusTotal, JOURNAL_NOUNS.entry, CORPUS_JOURNAL.scope)
  );
  const corpusPartial = $derived(
    corpusTotal === null && corpusJournal !== null && corpusJournal.length >= AUDIT_PAGE_SIZE
      ? journalIncomplete(JOURNAL_NOUNS.entry)
      : '',
  );

  async function loadCorpusJournal(): Promise<void> {
    const call = ++corpusSeq;
    corpusLoading = true;
    corpusError = '';
    try {
      const page = await api.audit(AUDIT_PAGE_SIZE, 0);
      if (call !== corpusSeq) return;
      corpusJournal = page.items;
      corpusTotal = page.total;
      corpusOffset = page.items.length;
      corpusMoreError = '';
      corpusStalled = false;
    } catch (caught) {
      if (call !== corpusSeq) return;
      corpusJournal = null;
      corpusTotal = null;
      corpusOffset = 0;
      corpusStalled = false;
      corpusError = messageOf(caught, CORPUS_JOURNAL.failedBody);
    } finally {
      if (call === corpusSeq) corpusLoading = false;
    }
  }

  async function loadCorpusJournalMore(): Promise<void> {
    if (corpusJournal === null) return;
    const call = ++corpusSeq;
    const offset = corpusOffset;
    corpusMoreLoading = true;
    corpusMoreError = '';
    try {
      const page = await api.audit(AUDIT_PAGE_SIZE, offset);
      if (call !== corpusSeq) return;
      corpusOffset = offset + page.items.length;
      corpusStalled = page.items.length === 0;
      // У акта корпуса есть id: перекрывшиеся порции склеиваются по нему.
      const seen = new Set(corpusJournal.map((entry) => entry.id));
      corpusJournal = [
        ...corpusJournal,
        ...page.items.filter((entry) => !seen.has(entry.id)),
      ];
      if (page.total !== null) corpusTotal = page.total;
    } catch {
      if (call === corpusSeq) corpusMoreError = JOURNAL_WINDOW_WORDS.moreFailed;
    } finally {
      if (call === corpusSeq) corpusMoreLoading = false;
    }
  }

  // Решения собственного аккаунта: `/audit` отдаёт акты всех под `audit:read`, а
  // автора подставляет сервер. Окно то же, что у журналов, поэтому неполное
  // чтение называется отдельно от пустоты и от сбоя.
  const DECISIONS_PAGE_SIZE = 50;
  let decisions = $state<ExpertDecision[] | null>(null);
  let decisionsTotal = $state<number | null>(null);
  let decisionsOffset = $state(0);
  let decisionsLoading = $state(false);
  let decisionsMoreLoading = $state(false);
  let decisionsError = $state('');
  let decisionsMoreError = $state('');
  let decisionsStalled = $state(false);
  let decisionsSeq = 0;

  const decisionLeft = $derived(
    decisions === null || decisionsTotal === null
      ? null
      : Math.max(0, decisionsTotal - decisions.length),
  );
  const decisionUnloaded = $derived(
    journalUnloaded(decisionsTotal, JOURNAL_NOUNS.entry, MY_DECISIONS.scope)
  );
  const decisionPartial = $derived(
    decisionsTotal === null && decisions !== null && decisions.length >= DECISIONS_PAGE_SIZE
      ? journalIncomplete(JOURNAL_NOUNS.entry)
      : '',
  );
  // Идентификатор объекта решения нужен при разборе конкретного случая, поэтому
  // он под «Служебными данными», а не в строке списка.
  const decisionRefs = $derived(
    (decisions ?? []).filter((entry) => Boolean(entry.object_id)).slice(0, 40),
  );

  function decisionName(action: string): string {
    return knownTerm(DECISION_ACTION_LABELS, action) ?? ACTIVITY_ACTION_FALLBACK;
  }

  // У решения собственного окна в контракте нет, поэтому ключ строки — пара
  // «действие, объект, время»: перекрывшиеся порции окна не удваиваются.
  function decisionKeyOf(entry: ExpertDecision): string {
    return `${entry.created_at}|${entry.action}|${entry.object_id}|${entry.outcome}`;
  }

  async function loadDecisions(): Promise<void> {
    const call = ++decisionsSeq;
    decisionsLoading = true;
    decisionsError = '';
    try {
      const page = await api.myDecisions(DECISIONS_PAGE_SIZE, 0);
      if (call !== decisionsSeq) return;
      decisions = page.items;
      decisionsTotal = page.total;
      decisionsOffset = page.items.length;
      decisionsMoreError = '';
      decisionsStalled = false;
    } catch (caught) {
      if (call !== decisionsSeq) return;
      decisions = null;
      decisionsTotal = null;
      decisionsOffset = 0;
      decisionsStalled = false;
      decisionsError = messageOf(caught, MY_DECISIONS.failedBody);
    } finally {
      if (call === decisionsSeq) decisionsLoading = false;
    }
  }

  async function loadDecisionsMore(): Promise<void> {
    if (decisions === null) return;
    const call = ++decisionsSeq;
    const offset = decisionsOffset;
    decisionsMoreLoading = true;
    decisionsMoreError = '';
    try {
      const page = await api.myDecisions(DECISIONS_PAGE_SIZE, offset);
      if (call !== decisionsSeq) return;
      decisionsOffset = offset + page.items.length;
      const seen = new Set(decisions.map(decisionKeyOf));
      decisions = [
        ...decisions,
        ...page.items.filter((entry) => !seen.has(decisionKeyOf(entry))),
      ];
      decisionsStalled = page.items.length === 0;
      if (page.total !== null) decisionsTotal = page.total;
    } catch {
      if (call === decisionsSeq) decisionsMoreError = JOURNAL_WINDOW_WORDS.moreFailed;
    } finally {
      if (call === decisionsSeq) decisionsMoreLoading = false;
    }
  }

  // Расход модели под своим входом. Окно выбирается, потому что «чем я работал
  // сегодня» и «во что обходится корпус» это разные вопросы, а сервер считает
  // только то, что попросят.
  const DEFAULT_USAGE_DAYS = 30;
  let usageDays = $state<number>(DEFAULT_USAGE_DAYS);
  let usage = $state<LlmUsageSummary | null>(null);
  let usageLoading = $state(false);
  let usageError = $state('');
  let usageSeq = 0;

  async function loadUsage(days: number): Promise<void> {
    usageDays = days;
    const call = ++usageSeq;
    usageLoading = true;
    usageError = '';
    try {
      const summary = await api.myUsage(days);
      if (call !== usageSeq) return;
      usage = summary;
    } catch (caught) {
      if (call !== usageSeq) return;
      usage = null;
      usageError = messageOf(caught, MY_USAGE.failedBody);
    } finally {
      if (call === usageSeq) usageLoading = false;
    }
  }

  // Чужой актор в читаемой строке называется словом: имена прочих аккаунтов
  // сервис не публикует, а свои акты помечаются прямо. Код актора нужен при
  // разборе конкретного акта, поэтому он живёт под «Служебными данными».
  function isMine(actorId: string): boolean {
    return actorId !== '' && actorId === session.account?.id;
  }

  const corpusActorRefs = $derived(
    (corpusJournal ?? []).filter((entry) => entry.actor_id !== '').slice(0, 40),
  );

  // Имя живёт в черновике: серверный ответ подхватывается один раз и после
  // подтверждения записи, но не перетирает напечатанное после ошибки.
  $effect(() => {
    const incoming = session.account?.display_name;
    if (typeof incoming === 'string' && !namePrimed) {
      namePrimed = true;
      nameDraft = incoming;
    }
  });

  $effect(() => {
    void view;
    if (browser) void tick().then(() => observeReveals());
  });

  $effect(() => {
    // Аккаунт подтверждён — тогда и спрашиваем его журнал. Смена имени или
    // пароля сама пишет акт, поэтому перечитываем журнал после подтверждения.
    if (view !== 'ready') return;
    void loadActivity();
  });

  $effect(() => {
    // Решения и расход читаются один раз на подтверждённом входе. Окно расхода
    // в эффекте не читается: его меняет кнопка экрана, и за каждой сменой
    // стоит собственный запрос, а не повтор этого эффекта.
    void view;
    if (view !== 'ready') return;
    void loadDecisions();
    void loadUsage(DEFAULT_USAGE_DAYS);
  });

  $effect(() => {
    // Журнал корпуса спрашивается только там, где на него есть право: запрос без
    // audit:read вернулся бы отказом, а отказ без требования — шумом в консоли.
    void canAudit;
    if (view !== 'ready' || !canAudit) return;
    void loadCorpusJournal();
  });

  async function saveProfile(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const value = nameDraft.trim();
    nameError = '';
    nameConfirmed = '';
    if (!value) {
      nameError = 'Имя не может быть пустым.';
      return;
    }
    if (value.length > NAME_MAX) {
      nameError = `Имя длиннее ${NAME_MAX} символов. Сократите его.`;
      return;
    }
    nameBusy = true;
    try {
      const updated = await api.updateProfile(value);
      session.hydrate(updated);
      // Подтверждением считаем только ответ сервера: черновик синхронизируем
      // с тем именем, которое вернул профиль, а не с тем, что было в поле.
      nameConfirmed = updated.display_name;
      nameDraft = updated.display_name;
      void loadActivity();
    } catch (caught) {
      // Введённый текст сохраняется: ошибку показываем поверх черновика.
      nameError = messageOf(caught, 'Имя не сохранено. Проверьте соединение и повторите.');
    } finally {
      nameBusy = false;
    }
  }

  // Предел длины проверяется до отправки: человек узнаёт его из поля, а не из
  // отказа сервиса. Серверная проверка остаётся своей: экран не подменяет её.
  async function savePassword(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    pwdErrors = {};
    pwdConfirmed = false;
    const problems: typeof pwdErrors = {};
    if (!currentPwd) problems.current = 'Введите текущий пароль.';
    if (!newPwd) problems.next = 'Введите новый пароль.';
    else if (newPwd.length < PASSWORD_MIN)
      problems.next = `Не короче ${PASSWORD_MIN} символов.`;
    if (!confirmPwd) problems.confirm = 'Повторите новый пароль.';
    else if (newPwd !== confirmPwd) problems.confirm = 'Подтверждение не совпадает с новым паролем.';
    if (Object.keys(problems).length > 0) {
      pwdErrors = problems;
      return;
    }
    pwdBusy = true;
    try {
      const updated = await api.changePassword({ current_password: currentPwd, new_password: newPwd });
      session.hydrate(updated);
      // Поля пароля очищаем только после подтверждения сервером.
      currentPwd = '';
      newPwd = '';
      confirmPwd = '';
      pwdConfirmed = true;
      void loadActivity();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 403) {
        pwdErrors = { current: 'Текущий пароль не подошёл. Введите его ещё раз.' };
      } else if (caught instanceof ApiError && caught.status === 422) {
        pwdErrors = { next: `Не короче ${PASSWORD_MIN} символов.` };
      } else if (caught instanceof ApiError && caught.status === 429) {
        pwdErrors = { form: 'Слишком много неудачных попыток. Повторите позже.' };
      } else {
        pwdErrors = {
          form: messageOf(caught, 'Пароль не изменён. Проверьте соединение и повторите.'),
        };
      }
    } finally {
      pwdBusy = false;
    }
  }

  async function signOut(): Promise<void> {
    outBusy = true;
    outError = '';
    try {
      await session.logout();
      await goto('/login');
    } catch (caught) {
      outError = messageOf(caught, 'Выход не завершён. Проверьте соединение и повторите.');
    } finally {
      outBusy = false;
    }
  }
</script>

<svelte:head>
  <title>Профиль и пароль — Научный Клубок</title>
  <meta
    name="description"
    content="Кто вы в сервисе, какие дела открыты аккаунту, журнал ваших действий и решений, расход модели и смена пароля."
  />
</svelte:head>

<div class="page account">
  <div class="wrap wrap--narrow stack" style="--gap: var(--s6)">
    <SectionHead
      level="1"
      eyebrow="профиль и пароль"
      title="Посмотреть свой доступ и сменить пароль"
      lead="Кто вы в сервисе, какие дела открыты аккаунту, что вы здесь делали и как поменять пароль."
    >
      {#if view === 'ready' && session.account}
        <!-- Первый экран отвечает на три вопроса без прокрутки: кто вы, что здесь
             есть и какое действие главное. Право названо тем же словом, что и
             список дел ниже: одно право, а не уровни доступа. -->
        <div class="account__brief">
          <div class="account__head">
            <span class="avatar" aria-hidden="true">{session.initials}</span>
            <span class="grow">
              <span class="small">{session.account.display_name}</span>
              <span class="micro muted">{session.account.email}</span>
            </span>
            <StatusPill
              status={session.account.review_enabled ? 'consensus' : 'off'}
              label={rightLabel}
            />
          </div>
          <nav class="account__toc" aria-label="Разделы этого экрана">
            {#each pageSections as section (section.id)}
              <a class="account__toc-link" href={`#${section.id}`}>{section.title}</a>
            {/each}
          </nav>
          <div class="row">
            <Button href="#account-password" variant="action">Сменить пароль</Button>
            <span class="micro muted">Имя, права и журнал действий читаются на этой же странице.</span>
          </div>
        </div>
      {/if}
    </SectionHead>

    {#if view === 'pending'}
      <Panel>
        <div class="account__stack">
          <div class="row" role="status">
            <span class="spinner"></span>
            <p class="small">Читаем профиль аккаунта…</p>
          </div>
          <div class="stack" style="--gap: var(--s3)">
            {#each [0, 1, 2] as line (line)}
              <span class="skeleton account__skel"></span>
            {/each}
          </div>
        </div>
      </Panel>
    {:else if view === 'signed-out'}
      <Panel>
        <div class="account__stack">
          {#if outError}
            <Notice tone="error" title="Выход не завершён">
              {outError} Если вы остались на этой странице, войдите заново и повторите выход.
            </Notice>
          {/if}
          <Empty
            icon="lock"
            title="Нужен вход в аккаунт"
            body="Профиль, права, журнал действий и смена пароля появляются здесь после входа."
          >
            {#snippet action()}
              <div class="row">
                <Button href={`/login?next=${encodeURIComponent(page.url.pathname)}`} variant="action">Войти</Button>
                <Button variant="quiet" onclick={() => void session.refresh()}>Проверить вход</Button>
              </div>
            {/snippet}
          </Empty>
        </div>
      </Panel>
    {:else if session.account}
      {@const account = session.account}

      <!-- ── Кто вы ─────────────────────────────────────────────────── -->
      <section class="account__block" id="account-who">
        <SectionHead
          level="2"
          title={sectionTitle('account-who')}
          lead="По этому аккаунту сервис узнаёт, чьи правки и запросы записаны."
        />

        <Panel tone="lav">
          <div class="reveal account__id">
            <div class="account__id-head">
              <span class="avatar avatar--lg" aria-hidden="true">{session.initials}</span>
              <div class="grow">
                <p class="h3">{account.display_name}</p>
                <p class="small muted">{account.email}</p>
              </div>
              <StatusPill
                status={account.review_enabled ? 'consensus' : 'off'}
                label={rightLabel}
              />
            </div>
            <dl class="kv">
              <dt>В аккаунте с</dt>
              <dd><time datetime={account.created_at}>{ruDay(account.created_at)}</time></dd>
            </dl>
            <p class="micro">
              Имя можно поменять самому. Email остаётся как при регистрации: по нему сервис находит
              ваши действия в журнале.
            </p>
          </div>
        </Panel>

        <Panel tag="section">
          <div class="panel__head">
            <div class="grow">
              <h3 class="h4">Отображаемое имя</h3>
              <p class="micro muted">Имя видно в шапке и в журнале ваших действий.</p>
            </div>
          </div>

          <form class="stack" style="--gap: var(--s4)" onsubmit={saveProfile}>
            <Field
              label="Имя"
              name="display_name"
              autocomplete="name"
              maxlength={NAME_MAX}
              placeholder="Как вас подписывать в журнале"
              hint={`До ${NAME_MAX} символов; пробелы по краям убираются.`}
              error={nameError}
              disabled={nameBusy}
              bind:value={nameDraft}
            />

            {#if nameConfirmed}
              <Notice tone="ok" title="Имя сохранено">
                Теперь вы подписаны как {nameConfirmed}.
              </Notice>
            {/if}

            <div class="row">
              <Button type="submit" variant="action" busy={nameBusy} disabled={nameBusy}>
                Сохранить имя
              </Button>
              <Button
                variant="ghost"
                disabled={nameBusy || nameDraft.trim() === account.display_name}
                onclick={() => {
                  nameDraft = account.display_name;
                  nameError = '';
                  nameConfirmed = '';
                }}
              >
                Вернуть сохранённое имя
              </Button>
            </div>
          </form>
        </Panel>
      </section>

      <!-- ── Что вам открыто ────────────────────────────────────────── -->
      <section class="account__block" id="account-rights">
        <SectionHead
          level="2"
          title={sectionTitle('account-rights')}
          lead="Список дел этого аккаунта. Права выдаёт администратор сервиса, на этом экране их включить нельзя."
        />

        <Panel class="reveal">
          <p class="micro muted">
            Обычные дела: открыто {openCount} из {workSections.length}.
          </p>
          {#if openCount === 0}
            <p class="small account__lead">
              Ни одно дело не открыто: находки, карта связей и вопросы к корпусу станут доступны, когда
              администратор выдаст права.
            </p>
          {:else}
            <ul class="opens">
              {#each workSections as item (item.capability)}
                <li class="opens__item" data-open={item.open ? 'yes' : 'no'}>
                  <span class="opens__mark" aria-hidden="true">
                    <Icon name={item.open ? 'check' : 'close'} size={15} />
                  </span>
                  <span class="grow">
                    {item.label}
                    <span class="micro opens__note">
                      {item.open ? item.note : BASIC_RIGHT_NOTE}
                    </span>
                  </span>
                  <span class="micro muted">
                    {item.open ? 'открыто' : 'закрыто'}
                    {#if item.open && item.to}
                      <a href={item.href}> в раздел «{item.to}»</a>
                    {/if}
                  </span>
                </li>
              {/each}
            </ul>
          {/if}

          <hr class="rule" />

          <p class="micro muted">
            Экспертные дела: открыто {openExpertCount} из {expertSections.length}.
          </p>
          <ul class="opens">
            {#each expertSections as item (item.capability)}
              <li class="opens__item" data-open={item.open ? 'yes' : 'no'}>
                <span class="opens__mark" aria-hidden="true">
                  <Icon name={item.open ? 'check' : 'close'} size={15} />
                </span>
                <span class="grow">
                  {item.label}
                  <span class="micro opens__note">{item.open ? item.note : EXPERT_RIGHT_NOTE}</span>
                </span>
                <span class="micro muted">
                  {item.open ? 'открыто' : 'закрыто'}
                  {#if item.open && item.to}
                    <a href={item.href}> в раздел «{item.to}»</a>
                  {/if}
                </span>
              </li>
            {/each}
          </ul>

          <p class="micro muted account__note">
            Экспертное право выдаёт и снимает администратор сервиса, при регистрации его не выбрать.
            Служебные ключи прав и классы данных лежат ниже, в раскрытии «Служебные данные».
          </p>
        </Panel>
      </section>

      <!-- ── Журнал ваших действий ──────────────────────────────────── -->
      <section class="account__block" id="account-journal">
        <SectionHead
          level="2"
          title={sectionTitle('account-journal')}
          lead="Ваши вопросы к корпусу, отзывы на ответ, правки, вход и выход."
        >
          <Button
            variant="quiet"
            icon="refresh"
            busy={activityLoading}
            disabled={activityLoading}
            onclick={() => void loadActivity()}
          >
            Обновить журнал
          </Button>
        </SectionHead>

        {#if activityLoading && activity === null}
          <Panel tone="sunk">
            <div class="row account__loading" role="status">
              <span class="spinner" aria-hidden="true"></span>
              <p class="small">{MY_JOURNAL.loading}</p>
            </div>
          </Panel>
        {:else if activityError}
          <Panel tone="coral">
            <div class="account__fault">
              <p class="eyebrow"><Icon name="alert" size={16} /> {MY_JOURNAL.failedTitle}</p>
              <p class="small">{activityError}</p>
              <div class="row">
                <Button
                  variant="action"
                  icon="refresh"
                  busy={activityLoading}
                  disabled={activityLoading}
                  onclick={() => void loadActivity()}
                >
                  Повторить запрос
                </Button>
              </div>
            </div>
          </Panel>
        {:else if activity !== null && journalRows.length === 0}
          <!-- Пустота подтверждается только нулём от сервера: при ненулевом или
               неизвестном полном числе журнал называется незагруженным. -->
          {#if activityUnloaded}
            <Panel tone="coral">
              <div class="account__fault">
                <p class="eyebrow"><Icon name="alert" size={16} /> {activityUnloaded.title}</p>
                <p class="small">{activityUnloaded.body}</p>
                <div class="row">
                  <Button
                    variant="action"
                    icon="refresh"
                    busy={activityLoading}
                    disabled={activityLoading}
                    onclick={() => void loadActivity()}
                  >
                    Повторить запрос
                  </Button>
                </div>
              </div>
            </Panel>
          {:else}
            <Empty
              icon="clock"
              title={MY_JOURNAL.emptyTitle}
              body={MY_JOURNAL.emptyBody}
            >
              {#snippet action()}
                <Button variant="action" href="/research">Задать вопрос</Button>
              {/snippet}
            </Empty>
          {/if}
        {:else if activity !== null}
          <Panel>
            <ul class="journal">
              {#each journalRows as entry, i (i)}
                <li class="journal__row">
                  <div class="journal__when">
                    <time datetime={entry.created_at}>{dateTime(entry.created_at)}</time>
                  </div>
                  <div class="journal__what">
                    <p class="small">{actionName(entry.action)}</p>
                  </div>
                  <StatusPill status={outcomeStatus(entry.outcome)} label={outcomeLabel(entry.outcome)} />
                </li>
              {/each}
            </ul>
            <p class="micro muted account__note" role="status">
              {shownSentenceOf(journalRows.length, activityTotal, JOURNAL_NOUNS.entry)}.
              {#if activityPartial}{activityPartial}.{/if}
            </p>
            {#if activityStalled}
              <!-- Остаток назван, а страница пришла пустой: действие убрано,
                   иначе клик был бы пустым. Тупик отличается и от сбоя, и от
                   «показаны все», поэтому говорит о себе своей строкой. -->
              <Notice tone="warn" title={JOURNAL_WINDOW_WORDS.stalledTitle}>
                {JOURNAL_WINDOW_WORDS.stalled}
              </Notice>
            {:else if activityLeft !== null && activityLeft > 0}
              <div class="row">
                <Button
                  variant="quiet"
                  busy={activityMoreLoading}
                  disabled={activityMoreLoading || activityLeft === 0}
                  onclick={() => void loadActivityMore()}
                >
                  {activityMoreLoading
                    ? JOURNAL_WINDOW_WORDS.loadingMore
                    : journalLoadMoreOf(
                        activityLeft ?? ACTIVITY_PAGE_SIZE,
                        ACTIVITY_PAGE_SIZE,
                        JOURNAL_NOUNS.entry
                      )}
                </Button>
              </div>
            {/if}
            {#if activityMoreError}
              <Notice tone="error" title={JOURNAL_WINDOW_WORDS.moreFailedTitle}>
                {activityMoreError}
              </Notice>
            {/if}
          </Panel>
        {/if}
      </section>

      <!-- ── Ваши экспертные решения ────────────────────────────────── -->
      <section class="account__block" id="account-decisions">
        <SectionHead level="2" title={sectionTitle('account-decisions')} lead={MY_DECISIONS.lead}>
          <Button
            variant="quiet"
            icon="refresh"
            busy={decisionsLoading}
            disabled={decisionsLoading}
            onclick={() => void loadDecisions()}
          >
            Обновить решения
          </Button>
        </SectionHead>

        {#if decisionsLoading && decisions === null}
          <Panel tone="sunk">
            <div class="row account__loading" role="status">
              <span class="spinner" aria-hidden="true"></span>
              <p class="small">{MY_DECISIONS.loading}</p>
            </div>
          </Panel>
        {:else if decisionsError}
          <Panel tone="coral">
            <div class="account__fault">
              <p class="eyebrow"><Icon name="alert" size={16} /> {MY_DECISIONS.failedTitle}</p>
              <p class="small">{decisionsError}</p>
              <div class="row">
                <Button
                  variant="action"
                  icon="refresh"
                  busy={decisionsLoading}
                  disabled={decisionsLoading}
                  onclick={() => void loadDecisions()}
                >
                  Повторить запрос
                </Button>
              </div>
            </div>
          </Panel>
        {:else if decisions !== null && decisions.length === 0}
          {#if decisionUnloaded}
            <Panel tone="coral">
              <div class="account__fault">
                <p class="eyebrow"><Icon name="alert" size={16} /> {decisionUnloaded.title}</p>
                <p class="small">{decisionUnloaded.body}</p>
                <div class="row">
                  <Button
                    variant="action"
                    icon="refresh"
                    busy={decisionsLoading}
                    disabled={decisionsLoading}
                    onclick={() => void loadDecisions()}
                  >
                    Повторить запрос
                  </Button>
                </div>
              </div>
            </Panel>
          {:else}
            <Empty
              icon="clock"
              title={MY_DECISIONS.emptyTitle}
              body={MY_DECISIONS.emptyBody}
            />
          {/if}
        {:else if decisions !== null}
          <Panel>
            <ul class="journal">
              {#each decisions as entry, i (i)}
                {@const detail = decisionDetail(entry.action, entry.metadata)}
                <li class="journal__row">
                  <div class="journal__when">
                    <time datetime={entry.created_at}>{dateTime(entry.created_at)}</time>
                  </div>
                  <div class="journal__what">
                    <p class="small">{decisionName(entry.action)}</p>
                    {#if detail}
                      <p class="micro muted">{detail}</p>
                    {/if}
                  </div>
                  <StatusPill
                    status={outcomeStatus(entry.outcome)}
                    label={outcomeLabel(entry.outcome)}
                  />
                </li>
              {/each}
            </ul>
            <p class="micro muted account__note" role="status">
              {shownSentenceOf(decisions.length, decisionsTotal, JOURNAL_NOUNS.entry)}.
              {#if decisionPartial}{decisionPartial}.{/if}
            </p>
            {#if decisionsStalled}
              <Notice tone="warn" title={JOURNAL_WINDOW_WORDS.stalledTitle}>
                {JOURNAL_WINDOW_WORDS.stalled}
              </Notice>
            {:else if decisionLeft !== null && decisionLeft > 0}
              <div class="row">
                <Button
                  variant="quiet"
                  busy={decisionsMoreLoading}
                  disabled={decisionsMoreLoading}
                  onclick={() => void loadDecisionsMore()}
                >
                  {decisionsMoreLoading
                    ? JOURNAL_WINDOW_WORDS.loadingMore
                    : journalLoadMoreOf(
                        decisionLeft ?? DECISIONS_PAGE_SIZE,
                        DECISIONS_PAGE_SIZE,
                        JOURNAL_NOUNS.entry,
                      )}
                </Button>
              </div>
            {/if}
            {#if decisionsMoreError}
              <Notice tone="error" title={JOURNAL_WINDOW_WORDS.moreFailedTitle}>
                {decisionsMoreError}
              </Notice>
            {/if}
          </Panel>
        {/if}
      </section>

      <!-- ── Расход модели ──────────────────────────────────────────── -->
      <section class="account__block" id="account-usage">
        <SectionHead level="2" title={sectionTitle('account-usage')} lead={MY_USAGE.lead}>
          <Button
            variant="quiet"
            icon="refresh"
            busy={usageLoading}
            disabled={usageLoading}
            onclick={() => void loadUsage(usageDays)}
          >
            Обновить расход
          </Button>
        </SectionHead>

        <Panel tone="lav">
          <div class="row" role="group" aria-label={MY_USAGE.windowCaption}>
            {#each USAGE_WINDOW_OPTIONS as days (days)}
              <Button
                variant={days === usageDays ? 'ink' : 'quiet'}
                disabled={usageLoading}
                onclick={() => void loadUsage(days)}
              >
                {USAGE_WINDOW_LABELS[days]}
              </Button>
            {/each}
          </div>

          {#if usageLoading && usage === null}
            <div class="row account__loading" role="status">
              <span class="spinner" aria-hidden="true"></span>
              <p class="small">{MY_USAGE.loading}</p>
            </div>
          {:else if usageError}
            <div class="account__fault">
              <p class="eyebrow"><Icon name="alert" size={16} /> {MY_USAGE.failedTitle}</p>
              <p class="small">{usageError}</p>
              <div class="row">
                <Button
                  variant="action"
                  icon="refresh"
                  busy={usageLoading}
                  disabled={usageLoading}
                  onclick={() => void loadUsage(usageDays)}
                >
                  Повторить запрос
                </Button>
              </div>
            </div>
          {:else if usage}
            <dl class="kv">
              <dt>{MY_USAGE.runs}</dt>
              <dd class="num">{num(usage.runs)}</dd>
              <dt>{MY_USAGE.failedRuns}</dt>
              <dd class="num">{num(usage.failed_runs)}</dd>
              <dt>{MY_USAGE.promptTokens}</dt>
              <dd class="num">{num(usage.prompt_tokens)}</dd>
              <dt>{MY_USAGE.completionTokens}</dt>
              <dd class="num">{num(usage.completion_tokens)}</dd>
            </dl>
            <!-- Активное окно называется строкой, а не только плашкой кнопки:
                 с клавиатуры выбранное окно иначе не прочитать. -->
            <p class="micro muted account__note" role="status">
              {MY_USAGE.windowCaption}: {USAGE_WINDOW_LABELS[usageDays]}.
              {#if usage.runs === 0}{MY_USAGE.empty}{/if}
            </p>
          {/if}
        </Panel>
      </section>

      <!-- ── Журнал корпуса ─────────────────────────────────────────── -->
      {#if canAudit}
        <section class="account__block" id="corpus-journal">
          <SectionHead
            level="2"
            title={sectionTitle('corpus-journal')}
            lead="Действия всех аккаунтов в этом корпусе: правки утверждений, решения по предложениям, отказы доступа."
          >
            <Button
              variant="quiet"
              icon="refresh"
              busy={corpusLoading}
              disabled={corpusLoading}
              onclick={() => void loadCorpusJournal()}
            >
              Обновить журнал
            </Button>
          </SectionHead>

          {#if corpusLoading && corpusJournal === null}
            <Panel tone="sunk">
              <div class="row account__loading" role="status">
                <span class="spinner" aria-hidden="true"></span>
                <p class="small">{CORPUS_JOURNAL.loading}</p>
              </div>
            </Panel>
          {:else if corpusError}
            <Panel tone="coral">
              <div class="account__fault">
                <p class="eyebrow"><Icon name="alert" size={16} /> {CORPUS_JOURNAL.failedTitle}</p>
                <p class="small">{corpusError}</p>
                <div class="row">
                  <Button
                    variant="action"
                    icon="refresh"
                    busy={corpusLoading}
                    disabled={corpusLoading}
                    onclick={() => void loadCorpusJournal()}
                  >
                    Повторить запрос
                  </Button>
                </div>
              </div>
            </Panel>
          {:else if corpusJournal !== null}
            {#if corpusJournal.length === 0}
              {#if corpusUnloaded}
                <Panel tone="coral">
                  <div class="account__fault">
                    <p class="eyebrow"><Icon name="alert" size={16} /> {corpusUnloaded.title}</p>
                    <p class="small">{corpusUnloaded.body}</p>
                    <div class="row">
                      <Button
                        variant="action"
                        icon="refresh"
                        busy={corpusLoading}
                        disabled={corpusLoading}
                        onclick={() => void loadCorpusJournal()}
                      >
                        Повторить запрос
                      </Button>
                    </div>
                  </div>
                </Panel>
              {:else}
                <Empty
                  icon="clock"
                  title={CORPUS_JOURNAL.emptyTitle}
                  body={CORPUS_JOURNAL.emptyBody}
                />
              {/if}
            {:else}
              <Panel>
                <ul class="journal">
                  {#each corpusJournal as entry, i (i)}
                    <li class="journal__row">
                      <div class="journal__when">
                        <time datetime={entry.created_at}>{dateTime(entry.created_at)}</time>
                      </div>
                      <div class="journal__what">
                        <p class="small">{actionName(entry.action)}</p>
                        <p class="micro muted">
                          {#if isMine(entry.actor_id)}это были вы{:else}другой аккаунт{/if}
                        </p>
                      </div>
                      <StatusPill status={outcomeStatus(entry.outcome)} label={outcomeLabel(entry.outcome)} />
                    </li>
                  {/each}
                </ul>
                <p class="micro muted account__note" role="status">
                  {shownSentenceOf(
                    corpusJournal.length,
                    corpusTotal,
                    JOURNAL_NOUNS.entry
                  )}.
                  {#if corpusPartial}{corpusPartial}.{/if}
                  Имена прочих аккаунтов сервис не публикует.
                </p>
                {#if corpusStalled}
                  <Notice tone="warn" title={JOURNAL_WINDOW_WORDS.stalledTitle}>
                    {JOURNAL_WINDOW_WORDS.stalled}
                  </Notice>
                {:else if corpusLeft !== null && corpusLeft > 0}
                  <div class="row">
                    <Button
                      variant="quiet"
                      busy={corpusMoreLoading}
                      disabled={corpusMoreLoading}
                      onclick={() => void loadCorpusJournalMore()}
                    >
                      {corpusMoreLoading
                        ? JOURNAL_WINDOW_WORDS.loadingMore
                        : journalLoadMoreOf(
                            corpusLeft ?? AUDIT_PAGE_SIZE,
                            AUDIT_PAGE_SIZE,
                            JOURNAL_NOUNS.entry
                          )}
                    </Button>
                  </div>
                {/if}
                {#if corpusMoreError}
                  <Notice tone="error" title={JOURNAL_WINDOW_WORDS.moreFailedTitle}>
                    {corpusMoreError}
                  </Notice>
                {/if}
              </Panel>
            {/if}
          {/if}
        </section>
      {/if}

      <!-- ── Смена пароля ───────────────────────────────────────────── -->
      <section class="account__block" id="account-password">
        <SectionHead
          level="2"
          title={sectionTitle('account-password')}
          lead="После смены пароля прочие входы этого аккаунта закрываются, этот вход остаётся."
        />

        <Panel tag="section">
          <form class="stack" style="--gap: var(--s4)" onsubmit={savePassword}>
            <Field
              label="Текущий пароль"
              name="current_password"
              type="password"
              autocomplete="current-password"
              error={pwdErrors.current ?? ''}
              disabled={pwdBusy}
              bind:value={currentPwd}
            />
            <Field
              label="Новый пароль"
              name="new_password"
              type="password"
              autocomplete="new-password"
              hint={`Не короче ${PASSWORD_MIN} символов. Удобнее длинная фраза, чем короткий набор.`}
              error={pwdErrors.next ?? ''}
              disabled={pwdBusy}
              bind:value={newPwd}
            />
            <Field
              label="Повторите новый пароль"
              name="confirm_password"
              type="password"
              autocomplete="new-password"
              error={pwdErrors.confirm ?? ''}
              disabled={pwdBusy}
              bind:value={confirmPwd}
            />

            {#if pwdErrors.form}
              <Notice tone="error" title="Пароль не изменён">{pwdErrors.form}</Notice>
            {/if}

            {#if pwdConfirmed}
              <Notice tone="ok" title="Пароль сохранён">
                Этот вход продолжает работу, остальные входы аккаунта закрыты.
              </Notice>
            {/if}

            <div class="row">
              <Button type="submit" variant="action" busy={pwdBusy} disabled={pwdBusy}>
                Сменить пароль
              </Button>
              <Button
                variant="ghost"
                disabled={pwdBusy}
                onclick={() => {
                  currentPwd = '';
                  newPwd = '';
                  confirmPwd = '';
                  pwdErrors = {};
                  pwdConfirmed = false;
                }}
              >
                Очистить поля
              </Button>
            </div>
          </form>
        </Panel>
      </section>

      <!-- ── Выход ──────────────────────────────────────────────────── -->
      <section class="account__block" id="account-signout">
        <SectionHead
          level="2"
          title={sectionTitle('account-signout')}
          lead="Выход закрывает только этот вход: корпус и журнал ваших действий остаются."
        />

        <Panel tone="coral" class="reveal">
          {#if outError}
            <Notice tone="error" title="Выход не завершён">{outError}</Notice>
          {/if}

          <div class="row">
            <Button variant="ink" icon="logout" busy={outBusy} disabled={outBusy} onclick={() => void signOut()}>
              Выйти
            </Button>
            <p class="micro muted">Войти снова можно сразу.</p>
          </div>
        </Panel>
      </section>

      <!-- ── Служебные данные ───────────────────────────────────────── -->
      <section class="account__block">
        <div class="acc">
          <button
            type="button"
            class="acc__head"
            aria-expanded={serviceOpen}
            aria-controls="account-service"
            onclick={() => (serviceOpen = !serviceOpen)}
          >
            <span>Служебные данные</span>
            <Icon name="plus" size={16} class="acc__icon" />
          </button>
          {#if serviceOpen}
            <div class="acc__body" id="account-service">
              <p>
                Служебный слой профиля: ключи прав, классы данных и идентификаторы. Они нужны при разборе
                обращения в сервис, а на решение аналитика не влияют.
              </p>

              <h3 class="h4 account__sub">Права аккаунта</h3>
              <ul class="rights">
                {#each [...workSections, ...expertSections] as item (item.capability)}
                  <li class="rights__row">
                    <span class="grow">{item.label}</span>
                    <code class="tech rights__key">{item.capability}</code>
                    <span class="micro muted">{item.open ? 'выдано' : 'не выдано'}</span>
                  </li>
                {/each}
              </ul>

              <h3 class="h4 account__sub">Доступные классы данных</h3>
              {#if account.data_classes.length === 0}
                <p class="micro muted">
                  Классы данных не открыты: находки корпуса не видны, даже если право на чтение выдано.
                </p>
              {:else}
                <div class="grants">
                  {#each account.data_classes as klass (klass)}
                    <span class="tag">{DATA_CLASS_LABELS[klass]}</span>
                  {/each}
                </div>
              {/if}

              <h3 class="h4 account__sub">Идентификаторы</h3>
              <dl class="kv account__ids">
                <dt>Аккаунт</dt>
                <dd><code class="tech">{account.id}</code></dd>
                <dt>Электронная почта</dt>
                <dd><code class="tech">{account.email}</code></dd>
              </dl>

              {#if journalRefs.length > 0}
                <h3 class="h4 account__sub">Объекты последних актов</h3>
                <ul class="refs">
                  {#each journalRefs as entry, i (i)}
                    <li class="refs__row">
                      <span class="micro muted">{dateTime(entry.created_at)}</span>
                      <span class="micro">{actionName(entry.action)}</span>
                      <code class="tech">{entry.object_id}</code>
                    </li>
                  {/each}
                </ul>
              {:else if activity !== null && journalRows.length > 0}
                <p class="micro muted">Объект не указан ни в одном акте этого списка журнала.</p>
              {/if}

              {#if decisionRefs.length > 0}
                <h3 class="h4 account__sub">Объекты ваших решений</h3>
                <p class="micro muted">
                  Идентификатор предложения, расхождения или утверждения, по которому принято решение.
                </p>
                <ul class="refs">
                  {#each decisionRefs as entry, i (i)}
                    <li class="refs__row">
                      <span class="micro muted">{dateTime(entry.created_at)}</span>
                      <span class="micro">{decisionName(entry.action)}</span>
                      <code class="tech">{entry.object_id}</code>
                    </li>
                  {/each}
                </ul>
              {/if}

              {#if unknownActions.length > 0}
                <h3 class="h4 account__sub">Действия без названия</h3>
                <p class="micro muted">
                  В словаре экрана их нет, поэтому в журнале они подписаны общими словами. Здесь ключ
                  виден целиком.
                </p>
                <ul class="refs">
                  {#each unknownActions as entry, i (i)}
                    <li class="refs__row">
                      <span class="micro muted">{dateTime(entry.created_at)}</span>
                      <code class="tech">{entry.action}</code>
                    </li>
                  {/each}
                </ul>
              {/if}

              {#if corpusActorRefs.length > 0}
                <h3 class="h4 account__sub">Акторы журнала корпуса</h3>
                <p class="micro muted">Свои акты помечены словами, чужие читаются идентификатором.</p>
                <ul class="refs">
                  {#each corpusActorRefs as entry, i (i)}
                    <li class="refs__row">
                      <span class="micro muted">{dateTime(entry.created_at)}</span>
                      <span class="micro">{actionName(entry.action)}</span>
                      <code class="tech">{isMine(entry.actor_id) ? 'этот аккаунт' : entry.actor_id}</code>
                    </li>
                  {/each}
                </ul>
              {/if}
            </div>
          {/if}
        </div>
      </section>
    {/if}
  </div>
</div>

<style>
  /* (app)-layout уже отступил на высоту pill-навигации: верх не удваиваем.
     Прокрутку ведёт документ — собственной высоты и внутреннего скролла у
     экрана нет. */
  .page.account {
    padding-top: var(--s5);
  }

  .account__head {
    display: flex;
    align-items: center;
    gap: var(--s3);
    min-width: 0;
  }

  .account__head .grow {
    display: flex;
    flex-direction: column;
    line-height: var(--lh-dense);
  }

  /* Обзор профиля: кто вы, что здесь есть и одно главное действие. Это должно
     читаться на первом экране 1280×900 без прокрутки. */
  .account__brief {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    min-width: 0;
  }

  /* Разделы экрана — ссылки внутрь страницы, а не в меню: они доступны с Tab
     наравне с кнопкой действия и названы тем же словом, что и заголовок секции. */
  .account__toc {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
  }

  .account__toc-link {
    display: inline-flex;
    align-items: center;
    min-height: 36px;
    padding: var(--s2) var(--s4);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-pill);
    background: var(--surface-raised);
    color: var(--ink-2);
    font-size: var(--t-micro);
    text-decoration: none;
  }

  .account__toc-link:hover {
    border-color: var(--line-strong);
    color: var(--ink);
    text-decoration: underline;
  }

  .account__stack {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  .account__skel {
    display: block;
    height: 56px;
    border-radius: var(--r-md);
  }

  /* Секции читаются блоками: заголовок раздела отделяется ритмом, а не рамкой. */
  .account__block {
    display: flex;
    flex-direction: column;
    gap: var(--s5);
  }

  .account__block > :global(.section-head) {
    margin-bottom: 0;
  }

  .account__id {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    min-width: 0;
  }

  .account__id-head {
    display: flex;
    align-items: center;
    gap: var(--s4);
    flex-wrap: wrap;
  }

  .account__id .avatar {
    background: var(--surface);
    border: 1px solid var(--lavender-deep);
  }

  .account__id .avatar--lg {
    width: 56px;
    height: 56px;
    font-size: var(--t-h4);
  }

  .account__id .kv {
    padding-top: var(--s4);
    border-top: 1px solid var(--lavender-deep);
  }

  .account__note {
    max-width: 68ch;
    margin-top: var(--s4);
  }

  /* Пояснение в списке прав: читается той же мерой, что и строки списка. */
  .account__lead {
    max-width: 68ch;
    margin-top: var(--s2);
  }

  .row.account__loading {
    --gap: var(--s4);
    align-items: flex-start;
  }

  .account__fault {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  /* Журнал: дата, действие и итог в одну строку. Итог несёт знак и слово, а не
     только цвет. */
  .journal {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }

  .journal__row {
    display: grid;
    grid-template-columns: minmax(0, 128px) minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--s4);
    padding-block: var(--s3);
    font-variant-numeric: tabular-nums;
  }

  .journal__row + .journal__row {
    border-top: 1px solid var(--line-soft);
  }

  .journal__when {
    color: var(--ink-3);
    font-size: var(--t-micro);
  }

  .journal__what {
    min-width: 0;
  }

  /* Служебного слоя в строке журнала нет намеренно: ключ действия и идентификатор
     объекта читаются только под «Служебными данными», поэтому правила для
     `.journal__what .tech` здесь не осталось. */

  .rights {
    list-style: none;
    margin: var(--s3) 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  .rights__row {
    display: flex;
    align-items: center;
    gap: var(--s4);
    padding-block: var(--s2);
    border-bottom: 1px solid var(--line-soft);
  }

  /* Ключ права читается целиком и переносится, а не распирает строку: он здесь
     единственный служебный слой этого списка. */
  .rights__row .rights__key {
    flex: none;
    max-width: 40%;
    overflow-wrap: anywhere;
  }

  /* Идентификаторы объектов журнала: служебный слой, перенос не распирает
     колонку. */
  .refs {
    list-style: none;
    margin: var(--s3) 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  .refs__row {
    display: grid;
    grid-template-columns: minmax(0, 128px) minmax(0, 1fr);
    gap: var(--s2) var(--s4);
  }

  .refs__row .tech {
    grid-column: 1 / -1;
  }

  .account__sub {
    margin-top: var(--s5);
    font-size: var(--t-small);
    font-weight: 600;
  }

  .account__ids {
    margin-top: var(--s3);
    grid-template-columns: minmax(0, auto) minmax(0, 1fr);
  }

  .grants {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
    margin-top: var(--s3);
  }

  .grants .tag {
    min-height: 36px;
  }

  /* Список открытых разделов: строка на каждый, статус читается с 13–14px. */
  .opens {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    margin: var(--s3) 0 0;
    padding: 0;
  }

  .opens__item {
    display: flex;
    align-items: center;
    gap: var(--s3);
    min-height: 40px;
    padding: var(--s2) var(--s4);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-md);
    background: var(--surface-raised);
    font-size: var(--t-small);
    color: var(--ink-2);
  }

  /* Статус несут текст, плашка и знак: отдельного цвета границ в токенах нет,
     поэтому здесь только готовые wash-поверхности. */
  .opens__item[data-open='yes'] {
    border-color: var(--line-strong);
    background: var(--consensus-wash);
    color: var(--ink);
  }

  .opens__item[data-open='no'] {
    border-color: var(--line-strong);
    background: var(--disputed-wash);
  }

  .opens__note {
    display: block;
    color: var(--ink-3);
  }

  .opens__mark {
    display: grid;
    place-items: center;
    flex: none;
    color: var(--ink-3);
  }

  .opens__item[data-open='yes'] .opens__mark {
    color: var(--consensus);
  }

  .opens__item[data-open='no'] .opens__mark {
    color: var(--disputed);
  }

  .account .rule {
    margin-block: var(--s5);
  }

  @media (max-width: 900px) {
    .journal__row {
      grid-template-columns: minmax(0, 1fr) auto;
    }

    .journal__when {
      grid-column: 1 / -1;
    }
  }

  @media (max-width: 640px) {
    .account__block {
      gap: var(--s4);
    }

    .journal__row {
      gap: var(--s2);
      padding-block: var(--s2);
    }

    .refs__row {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
