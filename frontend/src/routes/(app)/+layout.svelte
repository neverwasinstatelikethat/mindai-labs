<script lang="ts">
  import type { Snippet } from 'svelte';
  import { browser } from '$app/environment';
  import type { LayoutData } from './$types';
  import NavBar, { type NavLink } from '$lib/ui/NavBar.svelte';
  import { session } from '$lib/sessionStore.svelte';

  let { children, data }: { children: Snippet; data: LayoutData } = $props();

  const LINKS: NavLink[] = [
    { href: '/research', label: 'Запрос', icon: 'ask', gloss: 'вопрос по корпусу с ответом и доказательствами' },
    { href: '/findings', label: 'Находки', icon: 'search', gloss: 'утверждения, числа и локаторы первоисточника' },
    { href: '/graph', label: 'Карта связей', icon: 'graph', gloss: 'сущности, связи и сообщества по корпусу' },
    { href: '/compare', label: 'Сравнение', icon: 'compare', gloss: 'технологии по измерениям, бок о бок' },
    { href: '/conflicts', label: 'Расхождения', icon: 'conflict', gloss: 'где источники спорят и где молчат' },
    { href: '/feedback', label: 'Проверка решений', icon: 'shield', gloss: 'вердикты эксперта и версии фактов' },
    { href: '/dashboard', label: 'Состояние', icon: 'gauge', gloss: 'показания корпуса и качества ответов' },
  ];

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

<NavBar links={LINKS} />
<div class="app-body">
  {@render children()}
</div>
