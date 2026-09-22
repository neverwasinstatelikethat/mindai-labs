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
  <title>Научный Клубок — проверяемые R&D-знания</title>
  <meta
    name="description"
    content="Проверяемая карта научных знаний для горно-металлургических исследований: каждый тезис трассируется до страницы, листа и диапазона ячеек первоисточника."
  />
</svelte:head>

<div class="grain" aria-hidden="true"></div>
<a class="skip-link" href="#main">Перейти к содержанию</a>
<main id="main">{@render children()}</main>
