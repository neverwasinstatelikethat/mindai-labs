<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    eyebrow = '',
    title,
    lead = '',
    level = '2',
    id,
    class: className = '',
    children,
  }: {
    eyebrow?: string;
    title: string;
    lead?: string;
    level?: '1' | '2' | '3';
    id?: string;
    class?: string;
    children?: Snippet;
  } = $props();

  const Tag = $derived((`h${level}`) as 'h1' | 'h2' | 'h3');
  const cls = $derived(level === '1' ? 'h1' : level === '2' ? 'h2' : 'h3');
</script>

<header class="section-head {className}">
  <div class="section-head__text">
    {#if eyebrow}<p class="eyebrow reveal">{eyebrow}</p>{/if}
    <svelte:element this={Tag} {id} class="{cls} reveal">{title}</svelte:element>
    {#if lead}<p class="lead reveal" style="--reveal-delay: 90ms">{lead}</p>{/if}
  </div>
  {@render children?.()}
</header>

<style>
  .section-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: var(--s6);
    flex-wrap: wrap;
    margin-bottom: var(--s6);
  }

  .section-head__text {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    min-width: min(100%, 320px);
  }

  .section-head .eyebrow {
    gap: var(--s3);
  }

  .section-head .eyebrow::after {
    content: '';
    width: var(--s7);
    height: 1px;
    background: var(--line-strong);
  }
</style>
