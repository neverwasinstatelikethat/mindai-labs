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
    try {
      await session.login(email.trim(), password);
      await goto(next);
      await invalidateAll();
    } catch (caught) {
      formError =
        caught instanceof ApiError && caught.status === 401
          ? 'Неверный email или пароль.'
          : caught instanceof ApiError && caught.status === 429
            // Формулировку держит интерфейс: сервер считает паузу, но её текст
            // не должен становиться единственной подсказкой на экране входа.
            ? 'Слишком много неудачных попыток входа подряд. Подождите минуту и повторите — введённое осталось в форме.'
            : 'Войти не удалось. Проверьте соединение и повторите вход — введённое осталось в форме.';
    } finally {
      busy = false;
    }
  }
</script>

<div class="page auth page--cover">
  <div class="scene" aria-hidden="true">
    <span class="blob blob--coral" style="width:46vmax;height:46vmax;top:-18vmax;right:-14vmax"></span>
    <span class="blob blob--lav" style="width:38vmax;height:38vmax;bottom:-16vmax;left:-12vmax"></span>
  </div>

  <div class="wrap wrap--narrow auth__inner">
    <h1 class="display reveal">Вход в Клубок</h1>
    <p class="lead reveal" style="--reveal-delay: 90ms">
      Один вход на всё рабочее место: вопрос агенту, находки с трассировкой до источника,
      карта связей и сравнение технологий.
    </p>

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
          bind:value={password}
          error={fieldErrors.password ?? ''}
        />
      </div>

      {#if formError}
        <Notice tone="error" title="Войти не получилось">{formError}</Notice>
      {/if}

      <div class="row row--between">
        <Button type="submit" variant="action" {busy} disabled={busy}>Войти</Button>
        <Button href="/register" variant="link" size="sm">Нет аккаунта? Создать</Button>
      </div>

      <p class="micro muted">
        Разбор предложений эволюции, аудит и закрытые данные открываются при расширенном доступе;
        остальное рабочее пространство доступно сразу.
      </p>
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
</style>
