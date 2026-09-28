<script lang="ts">
  import { goto } from '$app/navigation';
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import { tick } from 'svelte';
  import { ApiError, api } from '$lib/api';
  import { countOf, dateTime } from '$lib/format';
  import { observeReveals } from '$lib/reveal';
  import { session } from '$lib/sessionStore.svelte';
  import {
    ACTIVITY_ACTION_FALLBACK,
    ACTIVITY_ACTION_LABELS,
    ACTIVITY_OUTCOME_LABELS,
    ACTIVITY_OUTCOME_UNKNOWN,
    knownTerm,
  } from '$lib/terms';
  import {
    CAPABILITY_LABELS,
    DATA_CLASS_LABELS,
    type ActivityEntry,
    type AuditEntry,
    type Capability,
  } from '$lib/types';
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

  // Экспертные права — уже технический слой: в читаемой части экрана о них
  // говорит одна строка, перечень уходит под «Служебные данные».
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

  // Журнал собственных актов читается отдельным запросом: /api/v1/me/activity
  // возвращает только то, что сделал этот аккаунт, — фильтр по автору держит
  // сервер, из адреса запроса он не приходит.
  let activity = $state<ActivityEntry[] | null>(null);
  let activityLoading = $state(false);
  let activityError = $state('');

  // Технические данные страницы по умолчанию свёрнуты: они нужны при разборе
  // обращения в сервис, а не для решения аналитика.
  let serviceOpen = $state(false);

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
  const openExpertCount = $derived(expertSections.filter((item) => item.open).length);

  const journalRows = $derived(activity ?? []);
  // Идентификаторы объектов нужны только при разборе конкретного акта, поэтому
  // живут под «Служебными данными», а не в строке журнала.
  const journalRefs = $derived(
    journalRows.filter((entry) => Boolean(entry.object_id)).slice(0, 40),
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

  // Действие читают по русскому имени: служебный ключ журнала остаётся под
  // «Служебными данными». Неизвестное словарю действие не остаётся безликим —
  // при общей подписи показывается и ключ, иначе два разных неизвестных акта
  // выглядели бы одним.
  function actionTerm(action: string): { name: string; named: boolean } {
    const known = knownTerm(ACTIVITY_ACTION_LABELS, action);
    return known ? { name: known, named: true } : { name: ACTIVITY_ACTION_FALLBACK, named: false };
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

  async function loadActivity(): Promise<void> {
    activityLoading = true;
    activityError = '';
    try {
      activity = await api.myActivity();
    } catch (caught) {
      // Сбой журнала — не «вы ничего не делали»: отказ называет себя отдельно
      // от пустого списка и предлагает повтор.
      activity = null;
      activityError = messageOf(caught, 'Журнал ваших действий не прочитан. Повторите запрос.');
    } finally {
      activityLoading = false;
    }
  }

  // Журнал корпуса — то, ради чего существует право `audit:read`: после того
  // как трейсинг ушёл из «Состояния», проверить обещание «журнал действий
  // аккаунтов» было нечем. Секция читается только держателям права, остальным
  // она не обещает доступ.
  let corpusJournal = $state<AuditEntry[] | null>(null);
  let corpusLoading = $state(false);
  let corpusError = $state('');

  const canAudit = $derived(session.can('audit:read'));

  async function loadCorpusJournal(): Promise<void> {
    corpusLoading = true;
    corpusError = '';
    try {
      corpusJournal = await api.audit();
    } catch (caught) {
      corpusJournal = null;
      corpusError = messageOf(caught, 'Журнал корпуса не прочитан. Повторите запрос.');
    } finally {
      corpusLoading = false;
    }
  }

  // Чужой актор в журнале — только код: имена прочих аккаунтов сервис не
  // публикует, а свои акты помечаются словом.
  function actorLabel(actorId: string): { mine: boolean; code: string } {
    return {
      mine: actorId !== '' && actorId === session.account?.id,
      code: actorId.slice(0, 8),
    };
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

  $effect(() => {
    // Аккаунт подтверждён — тогда и спрашиваем его журнал. Смена имени или
    // пароля сама пишет акт, поэтому перечитываем журнал после подтверждения.
    if (view !== 'ready') return;
    void loadActivity();
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
      void loadActivity();
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
      void loadActivity();
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
  <title>Профиль и пароль — Научный Клубок</title>
  <meta
    name="description"
    content="Кто вы в системе, какие разделы корпуса открыты, журнал ваших действий и смена пароля."
  />
</svelte:head>

<div class="page account">
  <div class="wrap wrap--narrow stack" style="--gap: var(--s6)">
    <SectionHead
      level="1"
      eyebrow="Профиль и пароль"
      title="Посмотреть свой доступ и сменить пароль"
      lead="Кто вы в системе, какие разделы вам открыты, что вы здесь делали и как поменять пароль."
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
            body="Профиль, журнал действий, смена пароля и разделы корпуса открываются после входа. Без входа здесь нечего показывать."
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
      <section class="account__block">
        <SectionHead level="2" title="Кто вы" lead="По этому аккаунту сервис узнаёт, чьи правки и запросы записаны." />

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
      </section>

      <!-- ── Что вам открыто ────────────────────────────────────────── -->
      <section class="account__block">
        <SectionHead
          level="2"
          title="Что вам открыто"
          lead="Права выдаёт администратор сервиса; на этом экране их включить нельзя."
        />

        <Panel class="reveal">
          {#if openCount === 0}
            <p class="small">
              Ни один раздел корпуса не открыт: находки, карта связей и запросы останутся недоступны,
              пока администратор не выдаст права.
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
            <p class="micro muted account__note">
              Закрытый раздел объясняется правом из этого списка: полное их перечисление и доступные
              классы данных лежат ниже, в «Служебных данных».
            </p>
          {/if}

          <hr class="rule" />

          <p class="small">
            {#if session.expert}
              Экспертные действия открыты:
              {expertSections
                .filter((item) => item.open)
                .map((item) => item.label.toLowerCase())
                .join(', ') || 'разбор предложений'}.
            {:else}
              Экспертного разбора у этого аккаунта нет. Базового уровня хватает для ежедневной работы:
              находки, карта связей, запросы к корпусу, обратная связь и оценка качества.
            {/if}
          </p>
          {#if openExpertCount === 0}
            <p class="micro muted account__note">
              Расширенный доступ выдаёт и снимает администратор сервиса — при регистрации он не
              выбирается.
            </p>
          {/if}
        </Panel>
      </section>

      <!-- ── Журнал моих действий ───────────────────────────────────── -->
      <section class="account__block">
        <SectionHead
          level="2"
          title="Журнал моих действий"
          lead="Запросы к корпусу, оценки ответов, правки и вход с выходом — только то, что делали вы."
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
            <div class="row account__loading">
              <span class="spinner" aria-hidden="true"></span>
              <p class="small">Читаем журнал ваших действий…</p>
            </div>
          </Panel>
        {:else if activityError}
          <Panel tone="coral">
            <div class="account__fault">
              <p class="eyebrow"><Icon name="alert" size={16} /> журнал не прочитан</p>
              <p class="small">{activityError} Это не пустой журнал — он не прочитан.</p>
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
          <Empty
            icon="clock"
            title="Ваших актов в журнале пока нет"
            body="Задайте вопрос в рабочем пространстве или загрузите документ — запись появится здесь после обновления журнала."
          >
            {#snippet action()}
              <Button variant="action" href="/research">Начать с запроса</Button>
            {/snippet}
          </Empty>
        {:else if activity !== null}
          <Panel>
            <ul class="journal">
              {#each journalRows as entry, i (i)}
                {@const action = actionTerm(entry.action)}
                <li class="journal__row">
                  <div class="journal__when">
                    <time datetime={entry.created_at}>{dateTime(entry.created_at)}</time>
                  </div>
                  <div class="journal__what">
                    <p class="small">
                      {action.name}
                      {#if !action.named}<code class="tech">{entry.action}</code>{/if}
                    </p>
                  </div>
                  <StatusPill status={outcomeStatus(entry.outcome)} label={outcomeLabel(entry.outcome)} />
                </li>
              {/each}
            </ul>
            <p class="micro muted account__note">
              Показаны последние {countOf(journalRows.length, 'запись', 'записи', 'записей')}; более
              старые в этот список не подтягиваются.
            </p>
          </Panel>
        {/if}
      </section>

      <!-- ── Журнал корпуса ─────────────────────────────────────────── -->
      {#if canAudit}
        <section class="account__block">
          <SectionHead
            level="2"
            title="Журнал корпуса"
            lead="Действия всех аккаунтов в этом корпусе: правки фактов, разборы предложений, отказы доступа."
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
              <div class="row account__loading">
                <span class="spinner" aria-hidden="true"></span>
                <p class="small">Читаем журнал корпуса…</p>
              </div>
            </Panel>
          {:else if corpusError}
            <Panel tone="coral">
              <div class="account__fault">
                <p class="eyebrow"><Icon name="alert" size={16} /> журнал не прочитан</p>
                <p class="small">{corpusError} Это не пустой журнал — он не прочитан.</p>
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
              <Empty
                icon="clock"
                title="В журнале корпуса пока нет актов"
                body="Запросы к корпусу, правки и отказы доступа записываются сюда по мере работы."
              />
            {:else}
              <Panel>
                <ul class="journal">
                  {#each corpusJournal as entry, i (i)}
                    {@const action = actionTerm(entry.action)}
                    {@const actor = actorLabel(entry.actor_id)}
                    <li class="journal__row">
                      <div class="journal__when">
                        <time datetime={entry.created_at}>{dateTime(entry.created_at)}</time>
                      </div>
                      <div class="journal__what">
                        <p class="small">
                          {action.name}
                          {#if !action.named}<code class="tech">{entry.action}</code>{/if}
                        </p>
                        <p class="micro muted">
                          {#if actor.mine}это были вы{:else}актор <code class="tech">{actor.code}</code>{/if}
                        </p>
                      </div>
                      <StatusPill status={outcomeStatus(entry.outcome)} label={outcomeLabel(entry.outcome)} />
                    </li>
                  {/each}
                </ul>
                <p class="micro muted account__note">
                  Показаны последние {countOf(corpusJournal.length, 'запись', 'записи', 'записей')};
                  более старые в этот запрос не подтягиваются, а имена прочих аккаунтов журнал не
                  публикует.
                </p>
              </Panel>
            {/if}
          {/if}
        </section>
      {/if}

      <!-- ── Безопасность ───────────────────────────────────────────── -->
      <section class="account__block">
        <SectionHead
          level="2"
          title="Безопасность"
          lead="Смена пароля закрывает все прочие входы этого аккаунта; выход закрывает только этот."
        />

        <Panel tag="section">
          <div class="panel__head">
            <div class="grow">
              <h3 class="h4">Пароль</h3>
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

        <Panel tone="coral" class="reveal">
          <div class="panel__head">
            <div class="grow">
              <h3 class="h4">Выход</h3>
              <p class="micro muted">
                Выход закрывает только этот вход: список находок и история ответов остаются.
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
                Полный перечень прав и классов данных: по нему видно, почему конкретный раздел закрыт.
                Идентификаторы нужны при разборе обращения в сервис.
              </p>

              <h3 class="h4 account__sub">Права аккаунта</h3>
              <ul class="rights">
                {#each [...workSections, ...expertSections] as item (item.capability)}
                  <li class="rights__row">
                    <span class="grow">{item.label}</span>
                    <span class="micro muted">{item.open ? 'выдано' : 'не выдано'}</span>
                  </li>
                {/each}
              </ul>

              <h3 class="h4 account__sub">Доступные классы данных</h3>
              {#if account.data_classes.length === 0}
                <p class="micro muted">
                  Классы данных не открыты — находки корпуса не видны, даже если право на чтение выдано.
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
                    {@const action = actionTerm(entry.action)}
                    <li class="refs__row">
                      <span class="micro muted">{dateTime(entry.created_at)}</span>
                      <span class="micro">{action.name}</span>
                      <code class="tech">{entry.object_id}</code>
                    </li>
                  {/each}
                </ul>
              {:else if activity !== null && journalRows.length > 0}
                <p class="micro muted">Объект не указан ни в одном акте этого списка журнала.</p>
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

  .journal__what .tech {
    display: block;
  }

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
