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
      // Поля после отказа пустеют: email возвращаем в форму, чтобы человек правил
      // только пароль, а пароль остаём чистым. Текст отказа обещает ровно это и не
      // называет оба поля сохранёнными.
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
  <title>Вход в StormIdea</title>
  <meta name="description" content="Войдите в StormIdea, чтобы продолжить работу с гипотезами и материалами." />
</svelte:head>

<div class="page auth page--cover">
  <div class="scene" aria-hidden="true">
    <span class="blob blob--coral" style="width:46vmax;height:46vmax;top:-18vmax;right:-14vmax"></span>
    <span class="blob blob--lav" style="width:38vmax;height:38vmax;bottom:-16vmax;left:-12vmax"></span>
  </div>

  <div class="wrap wrap--narrow auth__inner">
    <h1 class="display reveal">Войти в StormIdea</h1>

    {#if cameToContinue}
      <Notice tone="info" title="Нужен вход">
        Войдите, и вернётесь на ту же страницу: адрес сохранён.
      </Notice>
    {:else}
      <!-- Что будет сразу после входа: экран называет раздел, а не обещает
           «рабочее пространство» вообще. -->
      <p class="lead reveal" style="--reveal-delay: 90ms">
        После входа вы вернётесь к работе с гипотезами и проверке материалов.
      </p>
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
        <Button href="/register" variant="link" size="sm" class="auth__switch">
          Нет аккаунта? Создать
        </Button>
      </div>

      <p class="micro muted auth__gate">
        Если не получается войти, проверьте email и пароль или обратитесь к владельцу пространства.
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
    max-width: 15ch;
  }

  /* Единственная оговорка экрана: право выдаёт администратор, и оно сказано
     один раз. */
  .auth__gate {
    max-width: var(--maxw-measure);
    margin-top: var(--s5);
  }

  /* Ссылка-действие рядом с кнопкой входа: цель нажатия 32 px берётся
     геометрией контрола, кегль остаётся micro. На узком экране и под пальцем
     цель держит общее правило тач-целей (44 px) из app.css. */
  @media (min-width: 641px) {
    :global(a.auth__switch) {
      min-block-size: 32px;
    }
  }
</style>
