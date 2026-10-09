<script lang="ts">
  import { countOf } from '$lib/format';
  import { hypothesisKindLabel } from '$lib/terms';
  import { DATA_CLASS_LABELS, type HypothesisSignal } from '$lib/types';

  let { signal }: { signal: HypothesisSignal } = $props();

  function sourceHref(evidenceIndex: number): string {
    const params = new URLSearchParams();
    params.set('hypothesis', signal.id);
    params.set('evidence', String(evidenceIndex));
    return `/findings?${params.toString()}`;
  }

  function locator(evidence: HypothesisSignal['evidence'][number]): string {
    const parts: string[] = [];
    if (evidence.page != null) parts.push(`страница ${evidence.page}`);
    if (evidence.sheet) parts.push(`лист ${evidence.sheet}`);
    if (evidence.cell_range) parts.push(evidence.cell_range);
    return parts.join(' · ');
  }

  const sourceCount = $derived(new Set(signal.evidence.map((item) => item.document_id)).size);
</script>

<article class="hypothesis" aria-labelledby={`hypothesis-${signal.id}`}>
  <div class="hypothesis__head">
    <span class="micro hypothesis__kind">{hypothesisKindLabel(signal.kind)}</span>
    <span class="micro muted">{DATA_CLASS_LABELS[signal.data_class]}</span>
  </div>
  <h3 class="h4" id={`hypothesis-${signal.id}`}>{signal.statement}</h3>
  {#if signal.proposal}
    <div class="hypothesis__next-step">
      <p class="micro hypothesis__label">Следующая проверка</p>
      <p class="small hypothesis__proposal">{signal.proposal}</p>
    </div>
  {/if}
  <div class="hypothesis__meta">
    <span class="micro muted">{countOf(sourceCount, 'источник', 'источника', 'источников')}</span>
  </div>
  <details class="hypothesis__evidence">
    <summary class="small">Основания · {countOf(signal.evidence.length, 'цитата', 'цитаты', 'цитат')}</summary>
    <ul>
      {#each signal.evidence as item, index (`${signal.id}-evidence-${index}`)}
        <li>
          <a class="hypothesis__source" href={sourceHref(index)}>
            <span class="small hypothesis__source-title">{item.source_title || 'Источник без названия'}</span>
            {#if locator(item)}<span class="micro muted">{locator(item)}</span>{/if}
            <blockquote class="small">{item.quote}</blockquote>
          </a>
        </li>
      {/each}
    </ul>
  </details>
</article>

<style>
  .hypothesis {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    min-width: 0;
    padding: var(--s5);
    border: 1px solid var(--line);
    border-radius: var(--r-md);
    background: var(--surface);
  }

  .hypothesis__head,
  .hypothesis__meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .hypothesis__kind {
    color: var(--action-ink);
    font-weight: 650;
  }

  .hypothesis__proposal {
    margin: 0;
    color: var(--ink-2);
  }

  .hypothesis__next-step {
    display: grid;
    gap: var(--s1);
    padding-left: var(--s3);
    border-left: 2px solid var(--line-strong);
  }

  .hypothesis__label {
    margin: 0;
    color: var(--ink-3);
  }

  .hypothesis__evidence {
    border-top: 1px solid var(--line);
    padding-top: var(--s3);
  }

  .hypothesis__evidence summary {
    width: fit-content;
    color: var(--ink-2);
    cursor: pointer;
  }

  .hypothesis__evidence summary:focus-visible {
    outline: 2px solid var(--action-ink);
    outline-offset: 3px;
    border-radius: var(--r-sm);
  }

  .hypothesis__evidence ul {
    display: grid;
    gap: var(--s2);
    margin: var(--s3) 0 0;
    padding: 0;
    list-style: none;
  }

  .hypothesis__source {
    display: grid;
    gap: var(--s1);
    padding: var(--s3);
    border-left: 2px solid var(--line-strong);
    border-radius: var(--r-sm);
    color: inherit;
    text-decoration: none;
  }

  .hypothesis__source:hover {
    background: var(--surface-sunk);
  }

  .hypothesis__source:focus-visible {
    outline: 2px solid var(--action-ink);
    outline-offset: 2px;
  }

  .hypothesis__source-title {
    font-weight: 600;
  }

  blockquote {
    margin: 0;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
</style>
