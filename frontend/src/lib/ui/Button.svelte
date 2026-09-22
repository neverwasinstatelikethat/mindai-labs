<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';
  import type { IconName } from './icons';

  let {
    variant = 'quiet',
    size = 'md',
    block = false,
    busy = false,
    disabled = false,
    href,
    type = 'button',
    icon,
    iconEnd,
    title,
    expanded,
    controls,
    class: className = '',
    onclick,
    children,
  }: {
    variant?: 'action' | 'ink' | 'quiet' | 'ghost' | 'link';
    size?: 'sm' | 'md';
    block?: boolean;
    busy?: boolean;
    disabled?: boolean;
    href?: string;
    type?: 'button' | 'submit' | 'reset';
    icon?: IconName;
    iconEnd?: IconName;
    title?: string;
    // Раскрытие (аккордеон, след прохода) — кнопка обязана называть, что она
    // открывает, иначе экрану чтения не на что опереться.
    expanded?: boolean;
    controls?: string;
    class?: string;
    onclick?: (event: MouseEvent) => void;
    children?: Snippet;
  } = $props();

  const cls = $derived(
    [
      'btn',
      `btn--${variant}`,
      size === 'sm' ? 'btn--sm' : '',
      block ? 'btn--block' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
</script>

{#if href}
  <a
    {href}
    class={cls}
    {title}
    aria-disabled={disabled || busy || undefined}
    rel={href.startsWith('http') ? 'external noreferrer' : undefined}
  >
    {#if busy}<span class="spinner spinner--quiet"></span>{/if}
    {#if icon && !busy}<Icon name={icon} size={size === 'sm' ? 16 : 18} />{/if}
    {@render children?.()}
    {#if iconEnd}<Icon name={iconEnd} size={size === 'sm' ? 16 : 18} />{/if}
  </a>
{:else}
  <button
    {type}
    class={cls}
    {title}
    disabled={disabled || busy}
    aria-busy={busy || undefined}
    aria-expanded={expanded}
    aria-controls={controls}
    {onclick}
  >
    {#if busy}<span class="spinner spinner--quiet"></span>{/if}
    {#if icon && !busy}<Icon name={icon} size={size === 'sm' ? 16 : 18} />{/if}
    {@render children?.()}
    {#if iconEnd}<Icon name={iconEnd} size={size === 'sm' ? 16 : 18} />{/if}
  </button>
{/if}
