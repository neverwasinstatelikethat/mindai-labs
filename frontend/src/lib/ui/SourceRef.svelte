<script lang="ts">
  import type { Evidence } from '../types';

  let { evidence, quote = false }: { evidence: Evidence; quote?: boolean } = $props();
  const sourceUrl = $derived(
    evidence.source_url && /^https?:\/\//i.test(evidence.source_url) ? evidence.source_url : null
  );

  const locator = $derived.by(() => {
    const parts: string[] = [];
    if (evidence.page != null) parts.push(`страница ${evidence.page}`);
    if (evidence.sheet) parts.push(`лист ${evidence.sheet}`);
    if (evidence.cell_range) parts.push(`ячейки ${evidence.cell_range}`);
    return parts.join(', ');
  });
</script>

{#if quote}
  <figure class="srcquote">
    <blockquote class="srcquote__text">{evidence.quote}</blockquote>
    <figcaption class="srcquote__meta">
      <span class="srcquote__title">{evidence.source_title}</span>
      {#if sourceUrl}<a href={sourceUrl} target="_blank" rel="noopener noreferrer">Открыть источник</a>{/if}
      {#if locator}<span class="srcquote__loc">{locator}</span>{/if}
    </figcaption>
  </figure>
{:else}
  <span class="srcref">
    <span class="srcref__title">{evidence.source_title}</span>
    {#if sourceUrl}<a href={sourceUrl} target="_blank" rel="noopener noreferrer">Открыть источник</a>{/if}
    {#if locator}<span class="srcref__loc">{locator}</span>{/if}
  </span>
{/if}

<style>
  .srcref {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--s2);
    min-width: 0;
  }

  .srcref__title {
    color: var(--ink-2);
    font-size: var(--t-small);
    overflow-wrap: anywhere;
  }

  .srcref__loc {
    color: var(--ink-3);
    font-size: var(--t-micro);
    font-family: var(--font-data);
  }

  .srcquote {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  .srcquote__text {
    margin: 0;
    padding: var(--s4) var(--s5);
    background: var(--surface-sunk);
    border-radius: var(--r-sm);
    font-size: var(--t-body);
    line-height: var(--lh-body);
    color: var(--ink);
    max-width: var(--maxw-measure);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .srcquote__meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
    align-items: baseline;
  }

  .srcquote__title {
    font-size: var(--t-small);
    color: var(--ink-2);
    font-weight: 600;
  }

  .srcquote__loc {
    font-size: var(--t-micro);
    color: var(--ink-3);
    font-family: var(--font-data);
  }
</style>
