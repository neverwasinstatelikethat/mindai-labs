<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { ApiError, api } from '$lib/api';
  import { session } from '$lib/sessionStore.svelte';
  import { PROFILE } from '$lib/terms';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Field from '$lib/ui/Field.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';

  const NAME_MAX = 120;
  const PASSWORD_MIN = 10;

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

  let outBusy = $state(false);
  let outError = $state('');

  const view = $derived.by(() => {
    if (session.state === 'unknown') return 'pending';
    if (session.state === 'anonymous' || !session.account) return 'signed-out';
    return 'ready';
  });

  function messageOf(caught: unknown, fallback: string): string {
    if (caught instanceof ApiError && caught.status === 401) {
      return 'Вход больше не подтверждён: войдите заново и повторите действие.';
    }
    return fallback;
  }

  $effect(() => {
    const incoming = session.account?.display_name;
    if (typeof incoming === 'string' && !namePrimed) {
      namePrimed = true;
      nameDraft = incoming;
    }
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
    outBusy = true;
    outError = '';
    try {
      await session.logout();
      await goto('/login');
    } catch (caught) {
      outError = messageOf(caught, PROFILE.signoutFailed);
    } finally {
      outBusy = false;
    }
  }
</script>

<svelte:head>
  <title>Профиль — StormIdea</title>
  <meta name="description" content="Данные и настройки аккаунта StormIdea." />
</svelte:head>

<div class="page profile">
  <div class="wrap account__layout">
    <SectionHead
      level="1"
      title="Профиль"
      lead="Данные аккаунта и настройки входа."
      class="account__title"
    />

    {#if view === 'pending'}
      <Panel tone="sunk" class="account__pending">
        <div class="row ac__loading" role="status">
          <span class="spinner" aria-hidden="true"></span>
          <p class="small">Загружаем профиль…</p>
        </div>
      </Panel>
    {:else if view === 'signed-out'}
      <Panel class="account__signed-out">
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

      <section class="account__identity" aria-labelledby="account-name">
        <Panel tone="sage">
          <div class="ac__identity-head">
            <span class="avatar ac__avatar" aria-hidden="true">{session.initials}</span>
            <div class="ac__identity-copy">
              <p class="micro muted">Ваш аккаунт</p>
              <h2 id="account-name" class="h3">{account.display_name}</h2>
              <p class="small ac__email">{account.email}</p>
            </div>
            <Button
              variant="quiet"
              size="sm"
              icon={nameEditing ? 'close' : 'edit'}
              expanded={nameEditing}
              controls="profile-name-form"
              onclick={() => {
                nameError = '';
                nameEditing = !nameEditing;
              }}
            >
              {nameEditing ? 'Отменить' : 'Изменить имя'}
            </Button>
          </div>

          {#if nameEditing}
            <form id="profile-name-form" class="stack ac__profile-form" onsubmit={saveProfile}>
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

      <section class="account__security" aria-labelledby="account-security-title">
        <SectionHead level="2" title="Безопасность" lead="Изменение пароля и текущий вход." />
        <Panel tone="lav">
          <div class="ac__security-content">
            <div class="ac__password-heading">
              <h3 id="account-security-title" class="h4">{PROFILE.passwordTitle}</h3>
              <p class="small muted">Для смены пароля понадобится текущий пароль.</p>
            </div>

            <form class="stack ac__password-form" onsubmit={savePassword}>
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
                  Очистить поля
                </Button>
              </div>
            </form>

            <div class="ac__session-row">
              <div>
                <h3 class="h4">Текущий сеанс</h3>
                <p class="micro muted">Завершите вход на этом устройстве.</p>
              </div>
              {#if outError}
                <Notice tone="error" title={PROFILE.signoutTitle}>{outError}</Notice>
              {/if}
              <Button
                variant="ink"
                icon="logout"
                busy={outBusy}
                disabled={outBusy}
                onclick={() => void signOut()}
              >
                {PROFILE.signoutAction}
              </Button>
            </div>
          </div>
        </Panel>
      </section>
    {/if}
  </div>
</div>

<style>
  .page.profile {
    padding-block: var(--s5) var(--s8);
  }

  .account__layout {
    display: grid;
    grid-template-columns: minmax(0, 0.82fr) minmax(0, 1.18fr);
    grid-template-areas:
      "title title"
      "identity security";
    align-items: start;
    gap: var(--s5) var(--s6);
  }

  .profile :global(.account__title) {
    grid-area: title;
    margin-bottom: var(--s1);
  }

  .account__identity {
    grid-area: identity;
    min-width: 0;
  }

  .account__security {
    grid-area: security;
    min-width: 0;
  }

  :global(.account__pending),
  :global(.account__signed-out) {
    grid-column: 1 / -1;
  }

  .account__security :global(.section-head) {
    margin-bottom: var(--s4);
  }

  .account__security :global(.section-head__text) {
    gap: var(--s2);
  }

  .ac__identity-head {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--s4);
  }

  .ac__avatar {
    width: var(--s8);
    height: var(--s8);
    font-size: var(--t-h3);
  }

  .ac__identity-copy {
    min-width: 0;
  }

  .ac__identity-copy p,
  .ac__identity-copy h2 {
    margin: 0;
  }

  .ac__identity-copy h2 {
    margin-top: var(--s1);
    overflow-wrap: anywhere;
  }

  .ac__identity-copy .ac__email {
    margin-top: var(--s1);
    overflow-wrap: anywhere;
  }

  .ac__profile-form {
    margin-top: var(--s5);
    max-width: 34rem;
  }

  .ac__security-content {
    display: grid;
    gap: var(--s4);
  }

  .ac__password-heading h3,
  .ac__password-heading p {
    margin: 0;
  }

  .ac__password-heading p {
    margin-top: var(--s1);
  }

  .ac__password-form {
    max-width: 34rem;
  }

  /* «Текущий сеанс» — заголовок блока, а не подпись под чертой: разделяет
     расстояние до предыдущей группы, линия внутри панели рисовала второй
     контур на уже обведённом листе. */
  .ac__session-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s4);
    margin-top: var(--s6);
  }

  .ac__session-row h3,
  .ac__session-row p {
    margin: 0;
  }

  .ac__session-row p {
    margin-top: var(--s1);
  }

  .row.ac__loading {
    --gap: var(--s4);
    align-items: flex-start;
  }

  @media (max-width: 800px) {
    .account__layout {
      grid-template-columns: minmax(0, 1fr);
      grid-template-areas:
        "title"
        "identity"
        "security";
      gap: var(--s4);
    }
  }

  @media (max-width: 520px) {
    .ac__identity-head {
      grid-template-columns: auto minmax(0, 1fr);
      align-items: start;
    }

    .ac__identity-head :global(.btn) {
      grid-column: 2;
      justify-self: start;
    }

    .ac__session-row {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
