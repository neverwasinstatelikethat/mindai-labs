<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';
  import type { IconName } from './icons';

  let {
    tone = 'info',
    title = '',
    children,
  }: {
    tone?: 'info' | 'error' | 'warn' | 'ok';
    title?: string;
    children?: Snippet;
  } = $props();

  const ICON: Record<'info' | 'error' | 'warn' | 'ok', IconName> = {
    info: 'info',
    error: 'alert',
    warn: 'alert',
    ok: 'checkCircle',
  };
</script>

<div class="notice notice--{tone}" role={tone === 'error' ? 'alert' : 'status'}>
  <Icon name={ICON[tone]} size={18} />
  <span class="grow">
    {#if title}<strong>{title}</strong>{/if}
    {@render children?.()}
  </span>
</div>

<style>
  strong {
    display: block;
    margin-bottom: var(--s1);
    font-weight: 600;
  }
</style>
