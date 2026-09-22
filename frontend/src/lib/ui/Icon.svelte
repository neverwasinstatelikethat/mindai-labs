<script lang="ts">
  import { MARKUP, type IconName } from './icons';

  let {
    name,
    size = 20,
    class: className = '',
    label = '',
  }: {
    name: IconName;
    size?: number;
    class?: string;
    label?: string;
  } = $props();
</script>

<svg
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.6"
  stroke-linecap="round"
  stroke-linejoin="round"
  class={className}
  aria-hidden={label ? undefined : 'true'}
  role={label ? 'img' : undefined}
  aria-label={label || undefined}
>
  {@html MARKUP[name]}
</svg>
