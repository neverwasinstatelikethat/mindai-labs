<script lang="ts">
  let {
    status,
    label,
    class: className = '',
  }: {
    status: 'consensus' | 'hypothesis' | 'disputed' | 'superseded' | 'off';
    label: string;
    class?: string;
  } = $props();
</script>

<span class="status status--{status} {className}">{label}</span>
