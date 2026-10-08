<script lang="ts">
  import type { Snippet } from 'svelte';
  import NavBar from '$lib/ui/NavBar.svelte';
  import { NAV_LINKS } from '$lib/nav';

  let { children }: { children: Snippet } = $props();
</script>

<NavBar links={NAV_LINKS} />
{@render children()}
