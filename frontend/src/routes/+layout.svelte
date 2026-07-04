<script lang="ts">
  import '../app.css';
  import type { Snippet } from 'svelte';
  import { onMount } from 'svelte';
  import { initRoleStore } from '$lib/roleStore.svelte';

  let { children }: { children: Snippet } = $props();

  // Инициализация роли из localStorage — только на клиенте, после гидратации
  onMount(() => {
    initRoleStore();
  });
</script>

<svelte:head>
  <title>Научный Клубок — проверяемые R&D-знания</title>
  <meta
    name="description"
    content="Проверяемая карта научных знаний для горно-металлургических исследований"
  />
</svelte:head>

{@render children()}

