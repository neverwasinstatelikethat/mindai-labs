<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    pressed = false,
    disabled = false,
    title = undefined,
    onclick,
    children,
  }: {
    pressed?: boolean;
    disabled?: boolean;
    // Служебный ключ (код документа, имя типа) показывается только подсказкой:
    // на виду остаётся русское имя.
    title?: string;
    onclick?: (event: MouseEvent) => void;
    children?: Snippet;
  } = $props();
</script>

<button type="button" class="chip" aria-pressed={pressed} {disabled} {title} {onclick}>
  <span class="chip__dot {pressed ? 'chip__dot--on' : ''}" aria-hidden="true"></span>
  {@render children?.()}
</button>

<style>
  .chip__dot {
    width: 7px;
    height: 7px;
    border-radius: var(--r-pill);
    background: var(--line-strong);
    transition: background var(--dur-fast) var(--ease-soft);
  }

  .chip__dot--on {
    background: var(--action);
  }
</style>
