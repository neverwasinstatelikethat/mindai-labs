<script lang="ts">
  import '../app.css';
  import type { Snippet } from 'svelte';
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import { tick } from 'svelte';
  import { initMotion, observeReveals } from '$lib/reveal';
  import { session } from '$lib/sessionStore.svelte';

  let { children }: { children: Snippet } = $props();

  // Проверка сессии нужна только там, где серверный guard не передал аккаунт
  // (публичные страницы): сам cookie httpOnly, поэтому клиент его не видит.
  // Защищённые страницы гидратируются синхронно в (app)-layout'е — до флеша
  // этого эффекта, — поэтому здесь двойной /auth/me не случается.
  $effect(() => {
    if (browser && session.state === 'unknown') void session.refresh();
  });

  $effect(() => {
    const path = page.url.pathname;
    if (!browser) return;
    void tick().then(() => observeReveals());
    void path;
  });

  $effect(() => {
    if (browser) initMotion();
  });
</script>

<svelte:head>
  <title>StormIdea — пространство для работы с гипотезами</title>
  <meta
    name="description"
    content="StormIdea помогает находить гипотезы и узкие места, проверять их по материалам и видеть связи между фактами."
  />
</svelte:head>

<div class="grain" aria-hidden="true"></div>
<a class="skip-link" href="#main">Перейти к содержанию</a>
<main id="main">{@render children()}</main>
