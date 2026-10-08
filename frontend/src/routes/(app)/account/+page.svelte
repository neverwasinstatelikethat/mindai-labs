<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { ApiError, api } from '$lib/api';
  import { dateTime } from '$lib/format';
  import { navLabel } from '$lib/nav';
  import { session } from '$lib/sessionStore.svelte';
  import {
    DECISION_ACTION_LABELS,
    JOURNAL_WINDOW_WORDS,
    MY_WORK,
    PROFILE,
    DECISION_NOUN,
    journalLoadMoreOf,
    knownTerm,
  } from '$lib/terms';
  import type { Capability, ExpertDecision } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Field from '$lib/ui/Field.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';

  const NAME_MAX = 120;
  const PASSWORD_MIN = 10;
  const PAGE_SIZE = 50;

  const PASSWORD_NOTE = 'Смена пароля закрывает прочие входы, этот остаётся.';
  const SIGNOUT_NOTE = 'Закрывает только этот вход: записи остаются.';
  type RightRow = { capability: Capability; href: string };

  const RIGHTS: RightRow[] = [
    { capability: 'knowledge:read', href: '/findings' },
    { capability: 'knowledge:read', href: '/graph' },
    { capability: 'query:ask', href: '/research' },
    { capability: 'feedback:give', href: '/research' },
    { capability: 'export:run', href: '/numbers' },
    { capability: 'proposal:review', href: '/findings' },
    { capability: 'restricted:read', href: '/findings' },
  ];
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

  // В профиле остаются решения по содержимому пространства; входы и другие
  // системные события не превращаются в пользовательскую ленту.
  let decisions = $state<ExpertDecision[] | null>(null);
  let decisionsTotal = $state<number | null>(null);
  let decisionsOffset = $state(0);
  let decisionsMoreLoading = $state(false);
  let decisionsError = $state('');
  let decisionsStalled = $state(false);
  let decisionsSeq = 0;

  const view = $derived.by(() => {
    if (session.state === 'unknown') return 'pending';
    if (session.state === 'anonymous' || !session.account) return 'signed-out';
    return 'ready';
  });

  // Дело называется разделом, куда оно открывается: слово навигации, а не
  // служебный ключ права, чтобы список прав и полоса говорили одинаково.
  const workSections = $derived(
    RIGHTS.map((item) => ({
      ...item,
      label: navLabel(item.href),
      open: session.can(item.capability),
    })),
  );

  // Раздел называется один раз, даже если в него ведут три дела: список ссылок
  // не превращается в пояснение про каждое право.
  const openLinks = $derived.by(() => {
    const seen = new Map<string, string>();
    for (const item of workSections) {
      if (item.open && !seen.has(item.href)) seen.set(item.href, item.label);
    }
    return [...seen.entries()];
  });

  type WorkRow = {
    key: string;
    at: string;
    label: string;
    outcome: ExpertDecision['outcome'];
  };

  const workRows = $derived.by<WorkRow[]>(() => {
    const rows = (decisions ?? []).map((entry) => ({
      key: `${entry.created_at}|${entry.action}|${entry.object_id}|${entry.outcome}`,
      at: entry.created_at,
      label: knownTerm(DECISION_ACTION_LABELS, entry.action) ?? 'Решение по материалу',
      outcome: entry.outcome,
    }));
    rows.sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0));
    return rows;
  });

  const decisionsLeft = $derived(
    decisions === null || decisionsTotal === null
      ? null
      : Math.max(0, decisionsTotal - decisions.length),
  );
  function messageOf(caught: unknown, fallback: string): string {
    if (caught instanceof ApiError && caught.status === 401) {
      return 'Вход больше не подтверждён: войдите заново и повторите действие.';
    }
    return fallback;
  }

  // Исход записи несут знак и слово, а не только цвет.
  function outcomeStatus(outcome: string): 'consensus' | 'disputed' | 'hypothesis' {
    if (outcome === 'success' || outcome === 'allowed') return 'consensus';
    if (outcome === 'denied' || outcome === 'failure') return 'disputed';
    return 'hypothesis';
  }

  function outcomeLabel(outcome: ExpertDecision['outcome']): string {
    if (outcome === 'success') return 'Записано';
    if (outcome === 'denied') return 'Отклонено';
    return 'Не завершено';
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

  $effect(() => {
    const incoming = session.account?.display_name;
    if (typeof incoming === 'string' && !namePrimed) {
      namePrimed = true;
      nameDraft = incoming;
    }
  });

  $effect(() => {
    // Решения загружаются только для подтверждённого аккаунта.
    if (view !== 'ready') return;
    void loadDecisions(0);
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
  <title>Профиль — StormIdea</title>
  <meta
    name="description"
    content="Профиль, история решений и смена пароля в StormIdea."
  />
</svelte:head>

<div class="page profile">
  <div class="wrap account__layout">
    <SectionHead level="1" title="Профиль" class="account__title">
      {#if view === 'ready' && session.account}
        {@const account = session.account}
        <div class="ac__id">
          <span class="avatar" aria-hidden="true">{session.initials}</span>
          <span class="ac__id-info">
            <strong class="small ac__id-name">{account.display_name}</strong>
            <span class="micro muted">{account.email}</span>
          </span>
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
      <section class="ac__block ac__access" id="account-rights">
        <SectionHead level="2" title="Разделы пространства" />

        <Panel tone="sage">
          <nav aria-label="Доступные разделы">
            {#if openLinks.length > 0}
              <ul class="ac__links">
                {#each openLinks as [href, label] (href)}
                  <li>
                    <a class="ac__link" href={href}>
                      <span>{label}</span>
                      <Icon name="arrowRight" size={18} />
                    </a>
                  </li>
                {/each}
              </ul>
            {:else}
              <p class="small ac__no-links">Пока нет доступных разделов.</p>
            {/if}
          </nav>
        </Panel>
      </section>

      <section class="ac__block ac__identity" id="account-identity">
        <SectionHead level="2" title="Данные аккаунта" />
        <Panel>
          <div class="ac__name-row">
            <div>
              <p class="micro muted">{PROFILE.nameTitle}</p>
              <p class="small ac__identity-value">{account.display_name}</p>
            </div>
            <div>
              <p class="micro muted">Электронная почта</p>
              <p class="small ac__identity-value">{account.email}</p>
            </div>
            <Button variant="quiet" size="sm" icon="edit" onclick={() => (nameEditing = !nameEditing)}>
              {nameEditing ? 'Отменить' : PROFILE.editName}
            </Button>
          </div>

          {#if nameEditing}
            <form class="stack ac__profile-form" onsubmit={saveProfile}>
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

      <!-- ── Решения по материалам ──────────────────────────────────── -->
      <section class="ac__block ac__work" id="account-work">
        <SectionHead level="2" title={MY_WORK.heading}>
          <div class="row">
            <Button variant="ghost" icon="refresh" onclick={() => void loadDecisions(0)}>Обновить</Button>
          </div>
        </SectionHead>

        {#if decisionsError}
          <Panel tone="coral">
            <div class="ac__fault">
              <p class="eyebrow"><Icon name="alert" size={16} /> {MY_WORK.failedTitle}</p>
              <p class="small">{decisionsError}</p>
              <div class="row">
                <Button variant="action" icon="refresh" onclick={() => void loadDecisions(0)}>Повторить</Button>
              </div>
            </div>
          </Panel>
        {:else if decisions === null}
          <Panel tone="sunk">
            <div class="row ac__loading" role="status">
              <span class="spinner" aria-hidden="true"></span>
              <p class="small">{MY_WORK.loading}</p>
            </div>
          </Panel>
        {:else if workRows.length === 0}
          <Empty icon="checkCircle" title={MY_WORK.emptyTitle} body={MY_WORK.emptyBody} />
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

            <div class="row ac__more">
              {#if decisionsStalled}
                <Notice tone="warn" title={JOURNAL_WINDOW_WORDS.stalledTitle}>
                  {JOURNAL_WINDOW_WORDS.stalled}
                </Notice>
              {:else if decisionsLeft !== null && decisionsLeft > 0}
                <Button
                  variant="quiet"
                  busy={decisionsMoreLoading}
                  disabled={decisionsMoreLoading}
                  onclick={() => void loadDecisions(decisionsOffset)}
                >
                  {decisionsMoreLoading
                    ? JOURNAL_WINDOW_WORDS.loadingMore
                    : journalLoadMoreOf(decisionsLeft ?? PAGE_SIZE, PAGE_SIZE, DECISION_NOUN)}
                </Button>
              {/if}
            </div>
          </Panel>
        {/if}
      </section>

      <!-- ── Доступ к аккаунту ─────────────────────────────────────────── -->
      <section class="ac__block ac__security" id="account-password">
        <SectionHead level="2" title="Безопасность" />

        <Panel tone="lav">
          <div class="ac__security-grid">
            <section class="ac__security-password" aria-labelledby="account-password-title">
              <h3 id="account-password-title" class="h4">{PROFILE.passwordTitle}</h3>
              <p class="micro muted">{PASSWORD_NOTE}</p>
              <form class="stack" onsubmit={savePassword}>
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
            </section>

            <section class="ac__security-signout" aria-labelledby="account-signout-title">
              <h3 id="account-signout-title" class="h4">{PROFILE.signoutTitle}</h3>
              <p class="micro muted">{SIGNOUT_NOTE}</p>
              {#if outError}
                <Notice tone="error" title={PROFILE.signoutTitle}>{outError}</Notice>
              {/if}
              <Button variant="ink" icon="logout" busy={outBusy !== ''} disabled={outBusy !== ''} onclick={() => void signOut()}>
                {PROFILE.signoutAction}
              </Button>
            </section>
          </div>
        </Panel>
      </section>
    {/if}
  </div>
</div>

<style>
  /* (app)-layout уже отступил на высоту навигации: верх не удваиваем. Прокрутку
     ведёт документ — собственной высоты у экрана нет. */
  .page.profile {
    padding-top: var(--s4);
  }

  .account__layout {
    display: grid;
    grid-template-columns: minmax(18rem, 0.8fr) minmax(0, 1.35fr);
    grid-template-areas:
      "title title"
      "identity work"
      "access work"
      "security security";
    align-items: start;
    gap: var(--s5);
  }

  .profile :global(.account__title) { grid-area: title; }
  .ac__access { grid-area: access; }
  .ac__identity { grid-area: identity; }
  .ac__work { grid-area: work; }
  .ac__security { grid-area: security; }

  .ac__block {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  .ac__block :global(.h2) {
    font-size: var(--t-h3);
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

  .ac__id-info {
    display: grid;
    gap: var(--s1);
    min-width: 0;
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

  /* Каждая ссылка ведёт к самостоятельному разделу, поэтому список остаётся
     навигацией, а не набором несвязанных чипов. */
  .ac__links {
    display: grid;
    gap: 0;
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .ac__link {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s3);
    padding-block: var(--s3);
    border-top: 1px solid var(--line-soft);
    color: var(--action-ink);
    font-weight: 600;
    text-decoration: none;
  }

  .ac__link:hover {
    color: var(--ink);
  }

  .ac__link:focus-visible {
    border-radius: var(--r-xs);
    outline: 2px solid var(--action-ink);
    outline-offset: 3px;
  }

  .ac__no-links {
    margin: 0;
  }

  .ac__name-row {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr)) auto;
    align-items: center;
    gap: var(--s4);
  }

  .ac__name-row p {
    margin: 0;
  }

  .ac__identity-value {
    overflow-wrap: anywhere;
  }

  .ac__profile-form {
    margin-top: var(--s4);
  }

  .ac__security-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(15rem, 0.6fr);
    gap: var(--s6);
  }

  .ac__security-password,
  .ac__security-signout {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--s3);
    min-width: 0;
  }

  .ac__security-password h3,
  .ac__security-password p,
  .ac__security-signout h3,
  .ac__security-signout p {
    margin: 0;
  }

  .ac__security-signout {
    align-self: stretch;
    padding-inline-start: var(--s5);
    border-inline-start: 1px solid var(--line-strong);
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

  .ac__more {
    --gap: var(--s3);
    flex-wrap: wrap;
    margin-top: var(--s3);
  }

  @media (max-width: 760px) {
    .account__layout {
      grid-template-columns: minmax(0, 1fr);
      grid-template-areas:
        "title"
        "identity"
        "access"
        "work"
        "security";
    }

    .ac__block {
      gap: var(--s3);
    }

    .ac__feed-row {
      grid-template-columns: minmax(0, 1fr) auto;
    }

    .ac__name-row,
    .ac__security-grid {
      grid-template-columns: minmax(0, 1fr);
    }

    .ac__security-signout {
      padding: var(--s4) 0 0;
      border-inline-start: 0;
      border-top: 1px solid var(--line-strong);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .ac__feed-row {
      transition: none;
    }
  }
</style>
