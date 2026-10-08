<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';
  import type { IconName } from './icons';

  let {
    icon = 'layers',
    title,
    body = '',
    action,
  }: {
    /** `null` — без знака: на экране, где и так есть маскот-кольцо, лишний
     *  кружок только поднимает высоту пустого состояния. */
    icon?: IconName | null;
    title: string;
    body?: string;
    action?: Snippet;
  } = $props();
</script>

<div class="empty">
  {#if icon}<span class="empty__mark"><Icon name={icon} size={22} /></span>{/if}
  <h3 class="h4">{title}</h3>
  {#if body}<p class="lead small muted">{body}</p>{/if}
  {@render action?.()}
</div>

<style>
  .empty__mark {
    display: grid;
    place-items: center;
    width: 54px;
    height: 54px;
    border-radius: var(--r-pill);
    background: var(--sage);
    color: var(--ink-2);
  }
</style>
