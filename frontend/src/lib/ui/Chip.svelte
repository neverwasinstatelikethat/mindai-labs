<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    pressed = false,
    disabled = false,
    onclick,
    children,
  }: {
    pressed?: boolean;
    disabled?: boolean;
    onclick?: (event: MouseEvent) => void;
    children?: Snippet;
  } = $props();
</script>

<button type="button" class="chip" aria-pressed={pressed} {disabled} {onclick}>
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
