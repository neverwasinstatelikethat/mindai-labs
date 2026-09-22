<script lang="ts">
  import type { Snippet } from 'svelte';
  import NavBar from '$lib/ui/NavBar.svelte';

  let { children }: { children: Snippet } = $props();
</script>

<NavBar />
{@render children()}
