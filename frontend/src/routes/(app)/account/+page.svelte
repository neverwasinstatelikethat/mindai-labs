<script lang="ts">
  import { goto } from '$app/navigation';
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import { tick } from 'svelte';
  import { ApiError, api } from '$lib/api';
  import { observeReveals } from '$lib/reveal';
  import { session } from '$lib/sessionStore.svelte';
  import { DATA_CLASS_LABELS, CAPABILITY_LABELS, type Capability } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Field from '$lib/ui/Field.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';

  // Потолок имени известен клиенту, минимальная длина пароля — нет: её задаёт
  // сервер, и на экране она звучит как «пароль короче допустимого».
  const NAME_MAX = 120;

  // Имя права — единственное и из types.ts (CAPABILITY_LABELS): экран не заводит
  // второй набор названий. Здесь только пояснение к уже названному праву.
  type Section = { capability: Capability; note: string };

  const WORK_SECTIONS: Section[] = [
    { capability: 'knowledge:read', note: 'находки корпуса и карта связей' },
    { capability: 'query:ask', note: 'вопрос агенту в рабочем пространстве' },
    { capability: 'feedback:give', note: 'отзыв по ответу и экспертная правка' },
    { capability: 'export:run', note: 'сравнение технологий и выгрузка ответа' },
    { capability: 'evaluation:view', note: 'метрики качества ответов' },
  ];

  const EXPERT_SECTIONS: Section[] = [
    { capability: 'proposal:review', note: 'разбор предложений эволюции' },
    { capability: 'restricted:read', note: 'утверждения закрытого класса' },
    { capability: 'audit:read', note: 'журнал действий аккаунтов' },
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

  const view = $derived.by(() => {
    if (session.state === 'unknown') return 'pending';
    if (session.state === 'anonymous' || !session.account) return 'signed-out';
    return 'ready';
  });

  const workSections = $derived(
    WORK_SECTIONS.map((item) => ({
      ...item,
      label: CAPABILITY_LABELS[item.capability],
      open: session.can(item.capability),
    })),
  );
  const expertSections = $derived(
    EXPERT_SECTIONS.map((item) => ({
      ...item,
      label: CAPABILITY_LABELS[item.capability],
      open: session.can(item.capability),
    })),
  );
  const openCount = $derived(workSections.filter((item) => item.open).length);

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
      nameError = `Имя длиннее ${NAME_MAX} символов — сократите его.`;
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
    } catch (caught) {
      // Введённый текст сохраняется: ошибку показываем поверх черновика.
      nameError = messageOf(caught, 'Имя не сохранено. Проверьте соединение и повторите.');
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
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 403) {
        pwdErrors = { current: 'Текущий пароль не подошёл. Введите его ещё раз.' };
      } else if (caught instanceof ApiError && caught.status === 422) {
        pwdErrors = { next: 'Новый пароль короче допустимого — возьмите длиннее.' };
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
  <title>Аккаунт и доступ — Научный Клубок</title>
  <meta
    name="description"
    content="Профиль аналитика: отображаемое имя, контакты, смена пароля, открытые разделы корпуса и экспертный разбор."
  />
</svelte:head>

<div class="page account">
  <div class="wrap wrap--narrow stack" style="--gap: var(--s6)">
    <SectionHead
      level="1"
      eyebrow="Аккаунт · профиль · доступ"
      title="Аккаунт и доступ"
      lead="Здесь ваши имя и контакты, смена пароля и то, что открыто аккаунту: находки, карта связей, сравнение технологий и экспертный разбор."
    >
      {#if view === 'ready' && session.account}
        <div class="account__head">
          <span class="avatar" aria-hidden="true">{session.initials}</span>
          <span class="grow">
            <span class="small">{session.account.display_name}</span>
            <span class="micro muted">{session.account.email}</span>
          </span>
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
              {outError} Если остались на этой странице — войдите заново и повторите выход.
            </Notice>
          {/if}
          <Empty
            icon="lock"
            title="Нужен вход в аккаунт"
            body="Профиль, смена пароля и разделы корпуса открываются после входа. Без входа здесь нечего показывать."
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

      <Panel tone="lav">
        <div class="reveal account__id">
          <div class="account__id-head">
            <span class="avatar avatar--lg" aria-hidden="true">{session.initials}</span>
            <div class="grow">
              <h2 class="h3">{account.display_name}</h2>
              <p class="small muted">{account.email}</p>
            </div>
            <StatusPill
              status={account.review_enabled ? 'consensus' : 'off'}
              label={account.review_enabled ? 'экспертный разбор открыт' : 'экспертный разбор закрыт'}
            />
          </div>
          <dl class="kv">
            <dt>В аккаунте с</dt>
            <dd><time datetime={account.created_at}>{ruDay(account.created_at)}</time></dd>
          </dl>
          <p class="micro">
            Имя можно поменять самому, email — нет: по нему сервис находит ваши действия в журнале.
          </p>
        </div>
      </Panel>

      <div class="grid grid--2 account__forms">
        <Panel tag="section">
          <div class="panel__head">
            <div class="grow">
              <h2 class="h4">Отображаемое имя</h2>
              <p class="micro muted">Имя видно в шапке и в журнале ваших действий.</p>
            </div>
          </div>

          <form class="stack" style="--gap: var(--s4)" onsubmit={saveProfile}>
            <Field
              label="Имя"
              name="display_name"
              autocomplete="name"
              maxlength={NAME_MAX}
              placeholder="Как подписывать ваши находки"
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

        <Panel tag="section">
          <div class="panel__head">
            <div class="grow">
              <h2 class="h4">Пароль</h2>
              <p class="micro muted">Смена пароля закрывает все прочие входы этого аккаунта.</p>
            </div>
          </div>

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
              hint="Минимальную длину задаёт сервис: короткий пароль не примется."
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
      </div>

      <Panel class="reveal">
        <div class="panel__head">
          <div class="grow">
            <h2 class="h4">Что открыто аккаунту</h2>
            <p class="micro muted">
              Разделы корпуса, которые вам доступны; расширить их может администратор сервиса.
            </p>
          </div>
          <p class="micro num">
            <span class="num">{openCount}</span> из <span class="num">{workSections.length}</span>
          </p>
        </div>

        {#if openCount === 0}
          <p class="micro">
            Ни один раздел корпуса не открыт: экраны находок и карты останутся пустыми, пока
            администратор не выдаст доступ.
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
                  <span class="micro opens__note">{item.note}</span>
                </span>
                <span class="micro muted">{item.open ? 'открыто' : 'закрыто'}</span>
              </li>
            {/each}
          </ul>
        {/if}

        <hr class="rule" />

        <div class="panel__head">
          <div class="grow">
            <h3 class="h4">Открытые классы данных</h3>
            <p class="micro muted">
              Каждый документ корпуса относится к классу данных — он решает, какие находки вам видны.
            </p>
          </div>
        </div>
        {#if account.data_classes.length === 0}
          <p class="micro">Классы данных не открыты — находки корпуса не видны.</p>
        {:else}
          <div class="grants">
            {#each account.data_classes as klass (klass)}
              <span class="tag">{DATA_CLASS_LABELS[klass]}</span>
            {/each}
          </div>
        {/if}
      </Panel>

      <Panel tone="sunk" class="reveal">
        <div class="panel__head">
          <div class="grow">
            <h2 class="h4">Экспертный разбор</h2>
            <p class="micro muted">Экспертные действия доступны при расширенном доступе.</p>
          </div>
          <StatusPill
            status={session.expert ? 'consensus' : 'off'}
            label={session.expert ? 'открыт' : 'закрыт'}
          />
        </div>

        <div class="stack" style="--gap: var(--s3)">
          <p class="small">
            {#if session.expert}
              Экспертные действия открыты:
              {expertSections
                .filter((item) => item.open)
                .map((item) => item.label.toLowerCase())
                .join(', ') || 'разбор предложений'}.
            {:else}
              Экспертного разбора у этого аккаунта нет. Базового уровня хватает для ежедневной
              работы: находки, карта связей, запросы к корпусу, обратная связь и оценка качества.
            {/if}
          </p>
          <p class="small muted">
            Расширенный доступ выдаёт и снимает администратор сервиса — при регистрации он не
            выбирается и с этого экрана не включается.
          </p>
        </div>
      </Panel>

      <Panel tone="coral" class="reveal">
        <div class="panel__head">
          <div class="grow">
            <h2 class="h4">Выход</h2>
            <p class="micro muted">
              Выход закрывает только этот вход: указатель находок и история ответов остаются.
            </p>
          </div>
        </div>

        {#if outError}
          <Notice tone="error" title="Выход не завершён">{outError}</Notice>
        {/if}

        <div class="row">
          <Button variant="ink" icon="logout" busy={outBusy} disabled={outBusy} onclick={() => void signOut()}>
            Выйти
          </Button>
          <p class="micro muted">
            Войти снова можно сразу: находки и история ответов никуда не деваются.
          </p>
        </div>
      </Panel>
    {/if}
  </div>
</div>

<style>
  /* (app)-layout уже отступил на высоту pill-навигации: верх не удваиваем. */
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

  .account__forms {
    gap: var(--s5);
  }

  .account__forms > :global(section) {
    min-width: 0;
  }

  .grants {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
  }

  /* Список открытых разделов: строка на каждый, статус читается с 13–14px. */
  .opens {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    margin: 0;
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

  .grants .tag {
    min-height: 36px;
  }

  .account .rule {
    margin-block: var(--s5);
  }
</style>
