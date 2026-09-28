<script lang="ts">
  import type { Snippet } from 'svelte';
  import { browser } from '$app/environment';
  import type { LayoutData } from './$types';
  import NavBar from '$lib/ui/NavBar.svelte';
  import { NAV_LINKS } from '$lib/nav';
  import { session } from '$lib/sessionStore.svelte';

  let { children, data }: { children: Snippet; data: LayoutData } = $props();

  // Аккаунт приходит из серверного guard'а: повторно /auth/me дёргать не нужно.
  // Гидратация синхронная, в теле скрипта: скрипты детей исполняются до флеша
  // эффектов корневого layout'а, и его проверка «сессия ещё неизвестна?» уже
  // видит известное состояние — двойного /auth/me не случается. Здесь берём
  // именно начальное значение data; обновления навигаций ведёт эффект ниже.
  // svelte-ignore state_referenced_locally
  if (browser) session.hydrate(data.account);

  // Навигации приносят свежий ответ guard'а: перегидрируем по изменению data.
  $effect(() => {
    session.hydrate(data.account);
  });
</script>

<NavBar links={NAV_LINKS} />
<div class="app-body">
  {@render children()}
</div>
