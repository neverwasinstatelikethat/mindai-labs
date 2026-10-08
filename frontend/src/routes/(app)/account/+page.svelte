<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { ApiError, api } from '$lib/api';
  import { dateTime, num } from '$lib/format';
  import { navLabel } from '$lib/nav';
  import { session } from '$lib/sessionStore.svelte';
  import {
    ACTIVITY_ACTION_FALLBACK,
    ACTIVITY_ACTION_LABELS,
    ACTIVITY_OUTCOME_LABELS,
    ACTIVITY_OUTCOME_UNKNOWN,
    DECISION_ACTION_LABELS,
    EXPERT_RIGHT_LINE,
    JOURNAL_WINDOW_WORDS,
    MY_WORK,
    PROFILE,
    WORK_FEED_NOUNS,
    WORK_NOUNS,
    journalIncomplete,
    journalLoadMoreOf,
    journalUnloaded,
    knownTerm,
    workFeedLine,
  } from '$lib/terms';
  import type { ActivityEntry, Capability, ExpertDecision } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Field from '$lib/ui/Field.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import InfoDot from '$lib/ui/InfoDot.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';

  const NAME_MAX = 120;
  const PASSWORD_MIN = 10;
  const PAGE_SIZE = 50;

  const PASSWORD_NOTE = 'Смена пароля закрывает прочие входы, этот остаётся.';
  const SIGNOUT_NOTE = 'Закрывает только этот вход: записи остаются.';
  const EXPERT_DOT_TITLE = 'Экспертный признак';

  type RightRow = { capability: Capability; href: string };

  const RIGHTS: RightRow[] = [
    { capability: 'knowledge:read', href: '/findings' },
    { capability: 'query:ask', href: '/research' },
    { capability: 'feedback:give', href: '/research' },
    { capability: 'export:run', href: '/numbers' },
    { capability: 'proposal:review', href: '/findings' },
    { capability: 'restricted:read', href: '/findings' },
  ];
  const BASIC_COUNT = 4;

  let nameDraft = $state('');
  let namePrimed = $state(false);
  let nameEditing = $state(false);
  let nameBusy = $state(false);
  let nameError = $state('');
  let nameConfirmed = $state('');

  let currentPwd = $state('');
  let newPwd = $state('');
  let confirmPwd = $state('');
  let pwdBusy = $state(false);
  let pwdErrors = $state<{ current?: string; next?: string; confirm?: string; form?: string }>({});
  let pwdConfirmed = $state(false);

  let outBusy = $state('');
  let outError = $state('');

  // Лента своей работы собирается из двух серверных страниц: действия аккаунта
  // и его экспертные решения. Сервер режет каждую своим `limit`/`offset`, полное
  // число подходит заголовком ответа, поэтому остаток считается от него, а не от
  // длины показывает.
  let activity = $state<ActivityEntry[] | null>(null);
  let activityTotal = $state<number | null>(null);
  let activityOffset = $state(0);
  let activityMoreLoading = $state(false);
  let activityError = $state('');
  let activityStalled = $state(false);
  let activitySeq = 0;

  let decisions = $state<ExpertDecision[] | null>(null);
  let decisionsTotal = $state<number | null>(null);
  let decisionsOffset = $state(0);
  let decisionsMoreLoading = $state(false);
  let decisionsError = $state('');
  let decisionsStalled = $state(false);
  let decisionsSeq = 0;

  let filter = $state<'all' | 'activity' | 'decisions'>('all');

  // Отбор ленты: имена переключателей читаются из того же словаря, что и
  // заголовок раздела, поэтому ключ отбора не выходит наружу служебным словом.
  const WORK_FILTERS: { key: 'all' | 'activity' | 'decisions'; label: string }[] = [
    { key: 'all', label: MY_WORK.filterAll },
    { key: 'activity', label: MY_WORK.filterActivity },
    { key: 'decisions', label: MY_WORK.filterDecisions },
  ];

  const view = $derived.by(() => {
    if (session.state === 'unknown') return 'pending';
    if (session.state === 'anonymous' || !session.account) return 'signed-out';
    return 'ready';
  });

  // Дело называется разделом, куда оно открывается: слово навигации, а не
  // служебный ключ права, чтобы список прав и полоса говорили одинаково.
  const workSections = $derived(
    RIGHTS.map((item, index) => ({
      ...item,
      label: navLabel(item.href),
      basic: index < BASIC_COUNT,
      open: session.can(item.capability),
    })),
  );
  const basicOpen = $derived(workSections.filter((item) => item.basic && item.open).length);
  const basicTotal = $derived(workSections.filter((item) => item.basic).length);
  const expertOpen = $derived(workSections.filter((item) => !item.basic && item.open).length);
  const expertTotal = $derived(workSections.filter((item) => !item.basic).length);

  // Раздел называется один раз, даже если в него ведут три дела: список ссылок
  // не превращается в пояснение про каждое право.
  const openLinks = $derived.by(() => {
    const seen = new Map<string, string>();
    for (const item of workSections) {
      if (item.open && !seen.has(item.href)) seen.set(item.href, item.label);
    }
    return [...seen.entries()];
  });

  const rightLabel = $derived(
    session.account?.review_enabled ? 'экспертное право открыто' : 'экспертного права нет',
  );

  type WorkRow = {
    key: string;
    at: string;
    label: string;
    outcome: string;
    kind: 'activity' | 'decisions';
  };

  const workRows = $derived.by<WorkRow[]>(() => {
    const rows: WorkRow[] = [];
    for (const entry of activity ?? []) {
      rows.push({
        key: `a|${entry.created_at}|${entry.action}|${entry.object_id}|${entry.outcome}`,
        at: entry.created_at,
        label: knownTerm(ACTIVITY_ACTION_LABELS, entry.action) ?? ACTIVITY_ACTION_FALLBACK,
        outcome: entry.outcome,
        kind: 'activity',
      });
    }
    for (const entry of decisions ?? []) {
      rows.push({
        key: `d|${entry.created_at}|${entry.action}|${entry.object_id}|${entry.outcome}`,
        at: entry.created_at,
        label: knownTerm(DECISION_ACTION_LABELS, entry.action) ?? ACTIVITY_ACTION_FALLBACK,
        outcome: entry.outcome,
        kind: 'decisions',
      });
    }
    rows.sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0));
    return filter === 'all' ? rows : rows.filter((row) => row.kind === filter);
  });

  const activityLeft = $derived(
    activity === null || activityTotal === null
      ? null
      : Math.max(0, activityTotal - activity.length),
  );
  const decisionsLeft = $derived(
    decisions === null || decisionsTotal === null
      ? null
      : Math.max(0, decisionsTotal - decisions.length),
  );
  const feedLine = $derived(
    workFeedLine(
      { read: activity?.length ?? 0, total: activityTotal },
      { read: decisions?.length ?? 0, total: decisionsTotal },
    )
  );
  // Неполнота каждой ленты называется отдельно: одна строка о прочитанном не
  // скрывает, что сервис не дочитал действия или решения.
  const feedPartial = $derived.by(() => {
    const parts: string[] = [];
    if (activityTotal === null && (activity?.length ?? 0) >= PAGE_SIZE) {
      parts.push(journalIncomplete(WORK_FEED_NOUNS.activity));
    }
    if (decisionsTotal === null && (decisions?.length ?? 0) >= PAGE_SIZE) {
      parts.push(journalIncomplete(WORK_FEED_NOUNS.decisions));
    }
    return parts.join('; ');
  });
  const feedUnloaded = $derived.by(() => {
    const a = journalUnloaded(activityTotal, WORK_NOUNS, 'ваших действий');
    const d = journalUnloaded(decisionsTotal, WORK_NOUNS, 'ваших решений');
    if (a && d) return a;
    return a ?? d;
  });

  const svcRows = $derived.by(() => {
    const account = session.account;
    if (!account) return [];
    // Три строки на поверхность (DESIGN.md, layout.technical-ids): поддержка
    // ищет аккаунт по идентификатору, почте и дате регистрации. Ключи прав и
    // классы данных экран уже показал человеческими числами и словом раздела.
    return [
      { label: 'Идентификатор аккаунта', value: account.id },
      { label: 'Электронная почта', value: account.email },
      { label: PROFILE.sinceTitle, value: ruDay(account.created_at) },
    ];
  });

  function messageOf(caught: unknown, fallback: string): string {
    if (caught instanceof ApiError && caught.status === 401) {
      return 'Вход больше не подтверждён: войдите заново и повторите действие.';
    }
    return fallback;
  }

  /** Дата служебного ряда, а не проза экрана: читается числом в моноширинном. */
  function ruDay(iso: string): string {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : date.toLocaleDateString('ru-RU');
  }

  function outcomeLabel(outcome: string): string {
    return ACTIVITY_OUTCOME_LABELS[outcome] ?? ACTIVITY_OUTCOME_UNKNOWN;
  }

  // Исход записи несут знак и слово, а не только цвет.
  function outcomeStatus(outcome: string): 'consensus' | 'disputed' | 'hypothesis' {
    if (outcome === 'success' || outcome === 'allowed') return 'consensus';
    if (outcome === 'denied' || outcome === 'failure') return 'disputed';
    return 'hypothesis';
  }

  function mergeActivity(current: ActivityEntry[], incoming: ActivityEntry[]): ActivityEntry[] {
    const seen = new Set(current.map((e) => `${e.created_at}|${e.action}|${e.object_id}|${e.outcome}`));
    return [...current, ...incoming.filter((e) => !seen.has(`${e.created_at}|${e.action}|${e.object_id}|${e.outcome}`))];
  }

  async function loadActivity(offset = 0): Promise<void> {
    const call = ++activitySeq;
    activityMoreLoading = offset > 0;
    activityError = '';
    try {
      const page = await api.myActivity(PAGE_SIZE, offset);
      if (call !== activitySeq) return;
      activity = offset > 0 && activity ? mergeActivity(activity, page.items) : page.items;
      activityTotal = page.total;
      activityOffset = offset + page.items.length;
      activityStalled = offset > 0 && page.items.length === 0;
    } catch (caught) {
      if (call !== activitySeq) return;
      if (offset === 0) {
        activity = null;
        activityTotal = null;
      }
      activityError = messageOf(caught, MY_WORK.failedBody);
    } finally {
      if (call === activitySeq) activityMoreLoading = false;
    }
  }

  async function loadDecisions(offset = 0): Promise<void> {
    const call = ++decisionsSeq;
    decisionsMoreLoading = offset > 0;
    decisionsError = '';
    try {
      const page = await api.myDecisions(PAGE_SIZE, offset);
      if (call !== decisionsSeq) return;
      if (offset > 0 && decisions) {
        const seen = new Set(decisions.map((e) => `${e.created_at}|${e.action}|${e.object_id}|${e.outcome}`));
        decisions = [...decisions, ...page.items.filter((e) => !seen.has(`${e.created_at}|${e.action}|${e.object_id}|${e.outcome}`))];
      } else {
        decisions = page.items;
      }
      decisionsTotal = page.total;
      decisionsOffset = offset + page.items.length;
      decisionsStalled = offset > 0 && page.items.length === 0;
    } catch (caught) {
      if (call !== decisionsSeq) return;
      if (offset === 0) {
        decisions = null;
        decisionsTotal = null;
      }
      decisionsError = messageOf(caught, MY_WORK.failedBody);
    } finally {
      if (call === decisionsSeq) decisionsMoreLoading = false;
    }
  }

  async function loadWork(): Promise<void> {
    await Promise.all([loadActivity(0), loadDecisions(0)]);
  }

  $effect(() => {
    const incoming = session.account?.display_name;
    if (typeof incoming === 'string' && !namePrimed) {
      namePrimed = true;
      nameDraft = incoming;
    }
  });

  $effect(() => {
    // Аккаунт подтверждён — тогда и спрашиваем его ленты. Смена имени или пароля
    // сама пишет запись, поэтому перечитываем ленту после подтверждения.
    if (view !== 'ready') return;
    void loadWork();
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
      nameConfirmed = updated.display_name;
      nameDraft = updated.display_name;
      nameEditing = false;
      void loadActivity(0);
    } catch (caught) {
      nameError = messageOf(caught, PROFILE.nameFailed);
    } finally {
      nameBusy = false;
    }
  }

  // Предел длины проверяется до отправки: человек узнаёт его из поля, а не из
  // отказа сервиса. Серверная проверка остаётся своей.
  async function savePassword(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    pwdErrors = {};
    pwdConfirmed = false;
    const problems: typeof pwdErrors = {};
    if (!currentPwd) problems.current = 'Введите текущий пароль.';
    if (!newPwd) problems.next = 'Введите новый пароль.';
    else if (newPwd.length < PASSWORD_MIN) problems.next = `Не короче ${PASSWORD_MIN} символов.`;
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
      currentPwd = '';
      newPwd = '';
      confirmPwd = '';
      pwdConfirmed = true;
      void loadActivity(0);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 403) {
        pwdErrors = { current: 'Текущий пароль не подошёл. Введите его ещё раз.' };
      } else if (caught instanceof ApiError && caught.status === 422) {
        pwdErrors = { next: `Не короче ${PASSWORD_MIN} символов.` };
      } else if (caught instanceof ApiError && caught.status === 429) {
        pwdErrors = { form: 'Слишком много неудачных попыток. Повторите позже.' };
      } else {
        pwdErrors = { form: messageOf(caught, 'Пароль не изменён. Проверьте соединение и повторите.') };
      }
    } finally {
      pwdBusy = false;
    }
  }

  async function signOut(): Promise<void> {
    outBusy = 'busy';
    outError = '';
    try {
      await session.logout();
      await goto('/login');
    } catch (caught) {
      outError = messageOf(caught, PROFILE.signoutFailed);
    } finally {
      outBusy = '';
    }
  }
</script>

<svelte:head>
  <title>Профиль — Научный Клубок</title>
  <meta
    name="description"
    content="Кто вы в сервисе, какие дела открыты аккаунту, ваша лента действий и решений, расход модели и смена пароля."
  />
</svelte:head>

<div class="page account">
  <div class="wrap wrap--narrow stack" style="--gap: var(--s5)">
    <SectionHead level="1" eyebrow="профиль" title="Профиль аккаунта">
      {#if view === 'ready' && session.account}
        {@const account = session.account}
        <div class="ac__id">
          <span class="avatar" aria-hidden="true">{session.initials}</span>
          <span class="grow small ac__id-name">{account.display_name}</span>
          <StatusPill status={account.review_enabled ? 'consensus' : 'off'} label={rightLabel} />
        </div>
      {/if}
    </SectionHead>

    {#if view === 'pending'}
      <Panel tone="sunk">
        <div class="row ac__loading" role="status">
          <span class="spinner" aria-hidden="true"></span>
          <p class="small">Читаем профиль аккаунта…</p>
        </div>
      </Panel>
    {:else if view === 'signed-out'}
      <Panel>
        <Empty icon="lock" title={PROFILE.signedOutTitle} body={PROFILE.signedOutBody}>
          {#snippet action()}
            <div class="row">
              <Button href={`/login?next=${encodeURIComponent(page.url.pathname)}`} variant="action">Войти</Button>
              <Button variant="quiet" onclick={() => void session.refresh()}>Проверить вход</Button>
            </div>
          {/snippet}
        </Empty>
      </Panel>
    {:else if session.account}
      {@const account = session.account}

      <!-- ── Кто вы и что открыто ───────────────────────────────────── -->
      <section class="ac__block" id="account-rights">
        <SectionHead level="2" title={PROFILE.heading} />

        <Panel>
          <!-- Сколько дела открыто — подписанные числа, а не предложение про
               них: правило выдачи экспертного признака произносит один «i». -->
          <dl class="kv ac__rights">
            <dt>{PROFILE.basicTitle}</dt>
            <dd><span class="num">{basicOpen} из {basicTotal}</span></dd>
            <dt>
              {PROFILE.expertTitle}
              <InfoDot title={EXPERT_DOT_TITLE} body={EXPERT_RIGHT_LINE} />
            </dt>
            <dd><span class="num">{expertOpen} из {expertTotal}</span></dd>
          </dl>

          <div class="ac__links">
            {#if openLinks.length > 0}
              <span class="micro muted">{PROFILE.goto}:</span>
              {#each openLinks as [href, label] (href)}
                <a class="ac__link" href={href}>{label}</a>
              {/each}
            {:else}
              <p class="small">Ни одно дело не открыто: разделы появятся, когда доступ подтвердят.</p>
            {/if}
          </div>

          <div class="ac__classes">
            <span class="micro muted">{PROFILE.classesTitle}:</span>
            {#if account.data_classes.length === 0}
              <span class="micro">{PROFILE.classesEmpty}</span>
            {:else}
              <span class="micro">{account.data_classes.length}</span>
            {/if}
          </div>

          <p class="small ac__name-row">
            <span class="muted">{PROFILE.nameTitle}:</span> {account.display_name}
            <Button variant="ghost" size="sm" onclick={() => (nameEditing = !nameEditing)}>
              {nameEditing ? 'Отменить' : PROFILE.editName}
            </Button>
          </p>

          {#if nameEditing}
            <form class="stack" style="--gap: var(--s3)" onsubmit={saveProfile}>
              <Field
                label={PROFILE.nameTitle}
                name="display_name"
                autocomplete="name"
                maxlength={NAME_MAX}
                hint={`До ${NAME_MAX} символов.`}
                error={nameError}
                disabled={nameBusy}
                bind:value={nameDraft}
              />
              <div class="row">
                <Button type="submit" variant="action" busy={nameBusy} disabled={nameBusy}>
                  {PROFILE.saveName}
                </Button>
              </div>
            </form>
          {:else if nameConfirmed}
            <Notice tone="ok" title={PROFILE.savedTitle}>{PROFILE.savedBody(nameConfirmed)}</Notice>
          {/if}
        </Panel>
      </section>

      <!-- ── Ваша работа ────────────────────────────────────────────── -->
      <section class="ac__block" id="account-work">
        <SectionHead level="2" title={MY_WORK.heading}>
          <div class="row" role="group" aria-label={MY_WORK.heading}>
            {#each WORK_FILTERS as item (item.key)}
              <Button variant={filter === item.key ? 'ink' : 'quiet'} onclick={() => (filter = item.key)}>
                {item.label}
              </Button>
            {/each}
            <Button variant="ghost" icon="refresh" onclick={() => void loadWork()}>Обновить</Button>
          </div>
        </SectionHead>

        {#if activityError || decisionsError}
          <Panel tone="coral">
            <div class="ac__fault">
              <p class="eyebrow"><Icon name="alert" size={16} /> {MY_WORK.failedTitle}</p>
              <p class="small">{activityError || decisionsError}</p>
              <div class="row">
                <Button variant="action" icon="refresh" onclick={() => void loadWork()}>Повторить запрос</Button>
              </div>
            </div>
          </Panel>
        {:else if workRows.length === 0 && (activity === null || decisions === null)}
          <Panel tone="sunk">
            <div class="row ac__loading" role="status">
              <span class="spinner" aria-hidden="true"></span>
              <p class="small">{MY_WORK.loading}</p>
            </div>
          </Panel>
        {:else if workRows.length === 0 && feedUnloaded}
          <Panel tone="coral">
            <div class="ac__fault">
              <p class="eyebrow"><Icon name="alert" size={16} /> {feedUnloaded.title}</p>
              <p class="small">{feedUnloaded.body}</p>
              <div class="row">
                <Button variant="action" icon="refresh" onclick={() => void loadWork()}>Повторить запрос</Button>
              </div>
            </div>
          </Panel>
        {:else if workRows.length === 0 && filter !== 'all'}
          <!-- Отбор не нашёл записей там, где другой поток их показывает: это не
               пустая работа и не сбой, а пустой отбор. -->
          <Panel>
            <p class="small">{MY_WORK.filterEmpty}</p>
            <div class="row">
              <Button variant="quiet" onclick={() => (filter = 'all')}>{MY_WORK.filterAll}</Button>
            </div>
          </Panel>
        {:else if workRows.length === 0}
          <Empty icon="clock" title={MY_WORK.emptyTitle} body={MY_WORK.emptyBody}>
            {#snippet action()}
              <Button variant="action" href="/research">Задать вопрос</Button>
            {/snippet}
          </Empty>
        {:else}
          <Panel>
            <ul class="ac__feed">
              {#each workRows as row (row.key)}
                <li class="ac__feed-row">
                  <time class="micro muted" datetime={row.at}>{dateTime(row.at)}</time>
                  <span class="small grow">{row.label}</span>
                  <StatusPill status={outcomeStatus(row.outcome)} label={outcomeLabel(row.outcome)} />
                </li>
              {/each}
            </ul>

            <p class="micro muted ac__note" role="status">
              {feedLine}
              {#if feedPartial}{feedPartial}.{/if}
            </p>

            <div class="row ac__more">
              {#if activityStalled}
                <Notice tone="warn" title={JOURNAL_WINDOW_WORDS.stalledTitle}>{JOURNAL_WINDOW_WORDS.stalled}</Notice>
              {:else if activityLeft !== null && activityLeft > 0 && filter !== 'decisions'}
                <Button
                  variant="quiet"
                  busy={activityMoreLoading}
                  disabled={activityMoreLoading}
                  onclick={() => void loadActivity(activityOffset)}
                >
                  {activityMoreLoading
                    ? JOURNAL_WINDOW_WORDS.loadingMore
                    : journalLoadMoreOf(activityLeft ?? PAGE_SIZE, PAGE_SIZE, WORK_FEED_NOUNS.activity)}
                </Button>
              {/if}
              {#if decisionsStalled}
                <Notice tone="warn" title={JOURNAL_WINDOW_WORDS.stalledTitle}>
                  {JOURNAL_WINDOW_WORDS.stalled}
                </Notice>
              {:else if decisionsLeft !== null && decisionsLeft > 0 && filter !== 'activity'}
                <Button
                  variant="quiet"
                  busy={decisionsMoreLoading}
                  disabled={decisionsMoreLoading}
                  onclick={() => void loadDecisions(decisionsOffset)}
                >
                  {decisionsMoreLoading
                    ? JOURNAL_WINDOW_WORDS.loadingMore
                    : journalLoadMoreOf(decisionsLeft ?? PAGE_SIZE, PAGE_SIZE, WORK_FEED_NOUNS.decisions)}
                </Button>
              {/if}
            </div>
          </Panel>
        {/if}
      </section>

      <!-- ── Смена пароля      <!-- ── Смена пароля ───────────────────────────────────────────── -->
      <section class="ac__block" id="account-password">
        <SectionHead level="2" title={PROFILE.passwordTitle} lead={PASSWORD_NOTE} />

        <Panel>
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
              hint={`Не короче ${PASSWORD_MIN} символов.`}
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
              <Notice tone="error" title={PROFILE.passwordTitle}>{pwdErrors.form}</Notice>
            {/if}
            {#if pwdConfirmed}
              <Notice tone="ok" title={PROFILE.passwordSavedTitle}>{PROFILE.passwordSavedBody}</Notice>
            {/if}

            <div class="row">
              <Button type="submit" variant="action" busy={pwdBusy} disabled={pwdBusy}>
                {PROFILE.passwordSave}
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
                {PROFILE.passwordClear}
              </Button>
            </div>
          </form>
        </Panel>
      </section>

      <!-- ── Выход ──────────────────────────────────────────────────── -->
      <section class="ac__block" id="account-signout">
        <SectionHead level="2" title={PROFILE.signoutTitle} lead={SIGNOUT_NOTE} />
        <Panel tone="coral">
          {#if outError}
            <Notice tone="error" title={PROFILE.signoutTitle}>{outError}</Notice>
          {/if}
          <div class="row">
            <Button variant="ink" icon="logout" busy={outBusy !== ''} disabled={outBusy !== ''} onclick={() => void signOut()}>
              {PROFILE.signoutAction}
            </Button>
          </div>
        </Panel>
      </section>
    {/if}
  </div>
</div>

<style>
  /* (app)-layout уже отступил на высоту навигации: верх не удваиваем. Прокрутку
     ведёт документ — собственной высоты у экрана нет. */
  .page.account {
    padding-top: var(--s5);
  }

  .ac__block {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  .ac__id {
    display: flex;
    align-items: center;
    gap: var(--s3);
    min-width: 0;
  }

  .ac__id-name {
    line-height: var(--lh-dense);
    /* Имя до 120 символов: без переноса оно упиралось бы в плашку статуса и
       резалось по краю, а «…» здесь означало бы, что человек не видит, как его
       зовут в сервисе. */
    overflow-wrap: anywhere;
  }

  .row.ac__loading {
    --gap: var(--s4);
    align-items: flex-start;
  }

  .ac__fault {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  /* Права — две подписанные строки с числами: способ выдачи экспертного
     признака объясняет один «i», а не строка прозы под заголовком. */
  .ac__rights {
    margin: 0;
  }

  .ac__rights dt {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--s2);
  }

  /* Число со знаменателем читается одной строкой и на узком экране: в тесной
     колонке «5 из 5» иначе разваливается вертикально и знаменатель перестаёт
     относиться к числу. */
  .ac__rights dd {
    white-space: nowrap;
  }

  /* Куда права открываются: список ссылок вместо строки с пояснением на каждое
     дело. Раздел назван словом навигации, поэтому имя совпадает с полосой. */
  .ac__links {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--s2) var(--s3);
    margin-top: var(--s4);
  }

  .ac__link {
    color: var(--action-ink);
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .ac__classes {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--s2) var(--s3);
    margin-top: var(--s4);
  }

  .ac__name-row {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--s3);
    margin: var(--s4) 0 0;
  }

  /* Лента своей работы: дата, дело и итог в одну строку. */
  .ac__feed {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .ac__feed-row {
    display: grid;
    grid-template-columns: minmax(0, 150px) minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--s4);
    padding-block: var(--s2);
    border-top: 1px solid var(--line-soft);
    font-variant-numeric: tabular-nums;
  }

  .ac__note {
    max-width: 92ch;
    margin-top: var(--s4);
  }

  .ac__more {
    --gap: var(--s3);
    flex-wrap: wrap;
    margin-top: var(--s3);
  }

  @media (max-width: 640px) {
    .ac__block {
      gap: var(--s3);
    }

    .ac__feed-row {
      grid-template-columns: minmax(0, 1fr) auto;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .ac__feed-row {
      transition: none;
    }
  }
</style>
