<script lang="ts">
  import { onMount } from 'svelte';
  import { goto, invalidateAll } from '$app/navigation';
  import { page } from '$app/state';
  import { NAV_LINKS } from '$lib/nav';
  import { session } from '$lib/sessionStore.svelte';
  import { api, ApiError } from '$lib/api';
  import Button from '$lib/ui/Button.svelte';
  import Field from '$lib/ui/Field.svelte';
  import InfoDot from '$lib/ui/InfoDot.svelte';
  import Notice from '$lib/ui/Notice.svelte';

  // Открытого редиректа не будет: принимаем только внутренний путь. `//host` и
  // `/\host` — не внутренние пути, их отбрасываем вместе с чужими доменами.
  const next = $derived.by(() => {
    const raw = page.url.searchParams.get('next');
    const internal = raw !== null && raw.startsWith('/') && !raw.startsWith('//') && !raw.startsWith('/\\');
    return internal ? raw : '/research';
  });

  // Клиент зеркалит политику длины, а не задаёт свою: минимум держит настройка
  // password_min_length, потолок 256 задан схемой запроса. Email-проверка та же,
  // что и при регистрации на стороне сервиса.
  const PASSWORD_MIN = 10;
  const PASSWORD_MAX = 256;
  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/;

  type Errors = {
    email?: string;
    displayName?: string;
    password?: string;
    repeat?: string;
  };

  let email = $state('');
  let displayName = $state('');
  let password = $state('');
  let repeat = $state('');
  let errors = $state<Errors>({});
  let formError = $state('');
  let taken = $state(false);
  let busy = $state(false);

  // Сервис может работать без постоянного хранилища: тогда аккаунт живёт до
  // перезапуска. Это называют до отправки формы, а не после потери записей.
  let volatileStorage = $state(false);

  onMount(() => {
    void (async () => {
      try {
        const status = await api.status();
        volatileStorage = status.accounts === 'in-memory' || status.state_backend === 'in-memory';
      } catch {
        // Показания не пришли: про хранение не говорим ничего, ложь дороже
        // молчания.
        volatileStorage = false;
      }
    })();
  });

  // Названия разделов берутся из того же списка, что и навигация: регистрация не
  // имеет права учить одному языку, а рабочее пространство называть иначе.

  // Ссылка на вход сохраняет куда идти после входа, если путь задан в ?next=.
  const loginHref = $derived(next === '/research' ? '/login' : `/login?next=${encodeURIComponent(next)}`);

  // Введённое при ошибке не стирается: форма остаётся заполненной и
  // повторяется одним действием.
  function validate(): boolean {
    const problems: Errors = {};
    if (!email.trim()) problems.email = 'Укажите email: он же логин.';
    else if (!EMAIL_RE.test(email.trim())) problems.email = 'Проверьте формат, например analyst@example.org.';
    if (!displayName.trim()) problems.displayName = 'Укажите имя: оно будет подписью к вашим записям.';
    if (!password) problems.password = 'Придумайте пароль.';
    else if (password.length < PASSWORD_MIN)
      problems.password = `Не короче ${PASSWORD_MIN} символов.`;
    if (!repeat) problems.repeat = 'Повторите пароль, чтобы не опечататься.';
    else if (password && repeat !== password) problems.repeat = 'Пароли не совпадают.';
    errors = problems;
    return Object.keys(problems).length === 0;
  }

  async function submit(): Promise<void> {
    formError = '';
    taken = false;
    if (!validate()) return;

    busy = true;
    try {
      // Аккаунт создаётся и подтверждается одним ответом, поэтому вторичный
      // запрос на вход не нужен — сразу идём в рабочее пространство.
      await session.register({
        email: email.trim(),
        display_name: displayName.trim(),
        password,
      });
      await goto(next);
      await invalidateAll();
    } catch (caught) {
      const status = caught instanceof ApiError ? caught.status : null;
      // «Данные сохранены» про аккаунт, а не про форму: такой фразы здесь нет,
      // потому что при отказе аккаунт не создан. Про поля говорят отдельно и
      // только то, что форма действительно держит введённое.
      if (status === 409) {
        taken = true;
        // Факт звучит один раз: поле называет отказ, подсказка ниже — только то,
        // чего не делает кнопка входа.
        errors = { ...errors, email: 'Этот email уже занят.' };
        formError = 'Пароль от занятого email меняют в профиле после входа.';
      } else if (status === 429) {
        formError = 'Слишком много попыток регистрации подряд, повторите позже.';
      } else if (status === 422) {
        formError = 'Регистрация не прошла: проверьте email и длину пароля.';
      } else if (status !== null && status >= 500) {
        formError = 'Аккаунт не создан: сервис не принял запись, повторите позже.';
      } else if (status !== null && status >= 400 && status < 500) {
        formError = 'Сервис отклонил регистрацию: проверьте email и пароль, затем повторите.';
      } else {
        formError = 'Сервис не ответил, аккаунт не создан. Проверьте соединение и повторите.';
      }
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head>
  <title>Регистрация в Научном Клубке</title>
  <meta
    name="description"
    content="Аккаунт Научного Клубка: вопрос к корпусу, находки с адресами в источниках, карта связей, отзывы на ответы."
  />
</svelte:head>

<div class="page auth page--cover">
  <div class="scene" aria-hidden="true">
    <span class="blob blob--coral" style="width:46vmax;height:46vmax;top:-18vmax;right:-14vmax"></span>
    <span class="blob blob--sage" style="width:40vmax;height:40vmax;top:8vmax;left:-16vmax"></span>
    <span class="blob blob--lav" style="width:38vmax;height:38vmax;bottom:-16vmax;left:-12vmax"></span>
  </div>

  <div class="wrap wrap--narrow auth__inner">
    <h1 class="display reveal">Регистрация в Клубке</h1>
    <p class="lead reveal" style="--reveal-delay: 90ms">
      Аккаунт нужен, чтобы рабочее пространство оставалось вашим: история запросов, находки
      с цитатами и адресами в источниках, ваши отзывы на ответы.
    </p>

    {#if volatileStorage}
      <Notice tone="warn" title="Хранилище непостоянное">
        Аккаунт и история ответов проживут до перезапуска сервиса.
      </Notice>
    {/if}

    <form
      class="panel reveal auth__form"
      style="--reveal-delay: 150ms"
      novalidate
      onsubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div class="auth__grid">
        <Field
          label="Email"
          name="email"
          type="email"
          autocomplete="email"
          inputmode="email"
          placeholder="analyst@example.org"
          autofocus
          bind:value={email}
          error={errors.email ?? ''}
        />
        <Field
          label="Имя для рабочего пространства"
          name="display-name"
          type="text"
          autocomplete="name"
          maxlength={120}
          placeholder="Как вас подписывать в находках"
          bind:value={displayName}
          error={errors.displayName ?? ''}
        />
        <Field
          label="Пароль"
          name="password"
          type="password"
          autocomplete="new-password"
          maxlength={PASSWORD_MAX}
          hint={`Не короче ${PASSWORD_MIN} символов: длинная фраза надёжнее короткого набора.`}
          placeholder="Длинный пароль, который не повторяется"
          bind:value={password}
          error={errors.password ?? ''}
        />
        <Field
          label="Пароль ещё раз"
          name="password-repeat"
          type="password"
          autocomplete="new-password"
          maxlength={PASSWORD_MAX}
          placeholder="Тот же пароль"
          bind:value={repeat}
          error={errors.repeat ?? ''}
        />
      </div>

      {#if taken}
        <Notice tone="error" title="Email уже занят">
          {formError}
          <span class="row notice__actions">
            <Button href={loginHref} variant="link" size="sm">Войти в существующий аккаунт</Button>
          </span>
        </Notice>
      {:else if formError}
        <Notice tone="error" title="Регистрация не завершена">{formError}</Notice>
      {/if}

      <p class="micro muted auth__consent">
        Регистрация означает согласие на обработку указанных вами данных.
        <InfoDot
          title="Зачем сервису эти данные"
          body="Email и имя нужны для входа и для подписи ваших записей, пароль — только для входа. Других целей у них нет, письмами и уведомлениями сервис не пользуется."
        />
      </p>

      <div class="row row--between">
        <Button type="submit" variant="action" {busy} disabled={busy}>
          {busy ? 'Отправляем…' : 'Создать аккаунт'}
        </Button>
        <Button href="/login" variant="link" size="sm" class="auth__switch">
          Уже есть аккаунт? Войти
        </Button>
      </div>

      <p class="small muted auth__rooms">
        После регистрации будут доступны рабочие разделы по вашему аккаунту.
      </p>
      <p class="micro muted auth__gate">
        Экспертное право на разбор предложений по ответам выдаёт администратор сервиса.
      </p>
    </form>
  </div>
</div>

<style>
  /* Центрирование и высоту даёт page--cover; здесь только свои отступы. */
  /* Верхний отступ обязан учитывать измеренную высоту шапки: собственное
     значение .auth перебивало зазор .page, и заголовок регистрации
     ложился под плавающую навигацию. */
  .auth {
    padding-block: calc(var(--topbar-h) + var(--s6)) var(--s8);
  }

  .auth__inner {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  .auth__form {
    display: flex;
    flex-direction: column;
    gap: var(--s5);
    margin-top: var(--s5);
  }

  .auth__grid {
    display: grid;
    gap: var(--s4);
    grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr));
  }

  .auth__inner .display {
    max-width: 16ch;
  }

  /* Согласие стоит над кнопкой: человек соглашается до отправки данных. */
  .auth__consent {
    max-width: var(--maxw-measure);
    padding-top: var(--s1);
    border-top: 1px solid var(--line);
  }

  .auth__rooms,
  .auth__gate {
    max-width: var(--maxw-measure);
  }

  /* Оговорка об экспертном праве звучит на экране одна: она стоит последней
     строкой под действиями. */
  .auth__gate {
    padding-top: var(--s3);
    border-top: 1px solid var(--line);
    color: var(--ink-3);
  }

  .notice__actions {
    --gap: var(--s4);
    margin-top: var(--s2);
  }

  /* Ссылка-действие рядом с кнопкой регистрации: цель нажатия 32 px берётся
     геометрией контрола, кегль остаётся micro. На узком экране и под пальцем
     цель держит общее правило тач-целей (44 px) из app.css. */
  @media (min-width: 641px) {
    :global(a.auth__switch) {
      min-block-size: 32px;
    }
  }
</style>
