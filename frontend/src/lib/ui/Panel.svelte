<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    tone = 'default',
    flush = false,
    raised = false,
    tag: Tag = 'section',
    class: className = '',
    children,
  }: {
    tone?: 'default' | 'sunk' | 'sage' | 'lav' | 'coral' | 'ink';
    flush?: boolean;
    raised?: boolean;
    tag?: 'section' | 'div' | 'article' | 'aside' | 'li' | 'form';
    class?: string;
    children?: Snippet;
  } = $props();

  const cls = $derived(
    [
      'panel',
      tone === 'default' ? '' : `panel--${tone}`,
      flush ? 'panel--flush' : '',
      raised ? 'panel--raised' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
</script>

<svelte:element this={Tag} class={cls}>
  {@render children?.()}
</svelte:element>
