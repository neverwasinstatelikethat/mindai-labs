<script lang="ts">
  import { goto, invalidateAll } from '$app/navigation';
  import { page } from '$app/state';
  import { session } from '$lib/sessionStore.svelte';
  import { ApiError } from '$lib/api';
  import Button from '$lib/ui/Button.svelte';
  import Field from '$lib/ui/Field.svelte';
  import Notice from '$lib/ui/Notice.svelte';

  // Открытого редиректа не будет: принимаем только внутренний путь.
  const next = $derived.by(() => {
    const raw = page.url.searchParams.get('next');
    return raw && raw.startsWith('/') && !/^\/\/|^\/\\/.test(raw) ? raw : '/research';
  });

  // Сюда привёл рабочий раздел: человек должен видеть, что после входа он
  // вернётся туда же, а не в раздел по умолчанию.
  const cameToContinue = $derived(page.url.searchParams.has('next'));

  let email = $state('');
  let password = $state('');
  let fieldErrors = $state<{ email?: string; password?: string }>({});
  let formError = $state('');
  let busy = $state(false);

  async function submit(): Promise<void> {
    fieldErrors = {};
    formError = '';
    const problems: { email?: string; password?: string } = {};
    if (!email.trim()) problems.email = 'Укажите email, с которым регистрировались.';
    if (!password) problems.password = 'Введите пароль.';
    if (Object.keys(problems).length > 0) {
      fieldErrors = problems;
      return;
    }

    busy = true;
    const typedEmail = email.trim();
    try {
      await session.login(typedEmail, password);
      await goto(next);
      await invalidateAll();
    } catch (caught) {
      // Отказ убирает введённое из полей: email возвращаем в форму, чтобы
      // человек правил только пароль, а пароль оставаем чистым. Текст отказа
      // обещает ровно это и не называет оба поля сохранёнными.
      email = typedEmail;
      password = '';
      // Сервер отвечает одинаковым отказом и на неизвестный email, и на неверный
      // пароль: перечислять аккаунты интерфейс не вправе, поэтому форма называет
      // обе причины сразу. Действие одно; ссылка создать аккаунт стоит под кнопкой
      // входа и в тексте отказа не повторяется.
      formError =
        caught instanceof ApiError && caught.status === 401
          ? 'Email или пароль не подходят. Поправьте пароль и повторите вход.'
          : caught instanceof ApiError && caught.status === 429
            // Сервер считает паузу сам, её точная длина наружу не выходит: нужно
            // знать только, что повторить стоит позже, а не сейчас.
            ? 'Слишком много неудачных попыток входа подряд, повторите позже.'
            : 'Сервис не ответил, войти не удалось. Проверьте соединение и повторите вход.';
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head>
  <title>Вход в Научный Клубок</title>
  <meta name="description" content="Вход в рабочее пространство Научного Клубка: вопрос к корпусу, находки, карта связей." />
</svelte:head>

<div class="page auth page--cover">
  <div class="scene" aria-hidden="true">
    <span class="blob blob--coral" style="width:46vmax;height:46vmax;top:-18vmax;right:-14vmax"></span>
    <span class="blob blob--lav" style="width:38vmax;height:38vmax;bottom:-16vmax;left:-12vmax"></span>
  </div>

  <div class="wrap wrap--narrow auth__inner">
    <h1 class="display reveal">Вход в Клубок</h1>
    <p class="lead reveal" style="--reveal-delay: 90ms">
      Один аккаунт на всё рабочее пространство: вопрос к корпусу, находки с цитатами и адресами
      в источниках, карта связей и сравнение технологий.
    </p>

    {#if cameToContinue}
      <Notice tone="info" title="Нужен вход">
        Рабочий раздел открывается после входа. Войдите, и вы вернётесь на ту же страницу: адрес
        сохранён.
      </Notice>
    {/if}

    <form
      class="panel reveal auth__form"
      style="--reveal-delay: 150ms"
      novalidate
      onsubmit={(event) => { event.preventDefault(); void submit(); }}
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
          error={fieldErrors.email ?? ''}
        />
        <Field
          label="Пароль"
          name="password"
          type="password"
          autocomplete="current-password"
          placeholder="Пароль из вашего аккаунта"
          hint="Тот же пароль, что задали при регистрации"
          bind:value={password}
          error={fieldErrors.password ?? ''}
        />
      </div>

      {#if formError}
        <Notice tone="error" title="Войти не получилось">{formError}</Notice>
      {/if}

      <div class="row row--between">
        <Button type="submit" variant="action" {busy} disabled={busy}>
          {busy ? 'Отправляем…' : 'Войти'}
        </Button>
        <Button href="/register" variant="link" size="sm">Нет аккаунта? Создать</Button>
      </div>

      <div class="stack auth__notes">
        <p class="micro muted">
          Email и пароль нужны только для входа в рабочее пространство и для подписи ваших
          записей.
        </p>
        <p class="micro muted">
          Забыли пароль? Новый задаёт администратор сервиса: самостоятельной сбросной ссылки
          здесь нет.
        </p>
        <p class="micro muted">
          Экспертное право на разбор предложений по ответам, журнал действий и закрытые данные
          выдаёт администратор сервиса. Остальное открывается сразу после входа.
        </p>
      </div>
    </form>
  </div>
</div>

<style>
  /* Центрирование и высоту даёт page--cover; здесь только свои отступы. */
  .auth {
    padding-block: var(--s6) var(--s8);
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
    max-width: 15ch;
  }

  /* Примечания читаются dense-блоком под действиями, а не россыпью строк. */
  .auth__notes {
    --gap: var(--s2);
    max-width: var(--maxw-measure);
  }
</style>
