<script lang="ts">
  import { DATA_CLASS_LABELS, type HypothesisSignal } from '$lib/types';

  let { signal }: { signal: HypothesisSignal } = $props();

  const KIND_LABELS: Record<string, string> = {
    numeric_discrepancy: 'Числовая несостыковка',
    cross_source_conflict: 'Расхождение между источниками',
    improvement_opportunity: 'Возможность улучшения',
    bottleneck: 'Возможное узкое место',
    information_gap: 'Пробел в информации',
    contradiction: 'Несостыковка',
    improvement: 'Идея улучшения',
    gap: 'Пробел в данных',
    opportunity: 'Возможность',
    risk: 'Риск',
  };

  function kindLabel(kind: string): string {
    const normalized = kind.trim().toLowerCase().replaceAll('-', '_');
    return KIND_LABELS[normalized] ?? kind.replaceAll('_', ' ');
  }

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

  const confidenceLabel = $derived(
    Number.isFinite(signal.confidence) && signal.confidence >= 0 && signal.confidence <= 1
      ? `${Math.round(signal.confidence * 100)}% уверенности`
      : '',
  );
</script>

<article class="hypothesis" aria-labelledby={`hypothesis-${signal.id}`}>
  <div class="hypothesis__head">
    <span class="micro hypothesis__kind">{kindLabel(signal.kind)}</span>
    <span class="micro muted">{DATA_CLASS_LABELS[signal.data_class]}</span>
  </div>
  <h3 class="h4" id={`hypothesis-${signal.id}`}>{signal.statement}</h3>
  {#if signal.proposal}
    <p class="small hypothesis__proposal"><strong>Что можно проверить:</strong> {signal.proposal}</p>
  {/if}
  <div class="hypothesis__meta">
    {#if confidenceLabel}<span class="micro muted">{confidenceLabel}</span>{/if}
    <span class="micro muted">
      {signal.evidence.length} {signal.evidence.length === 1 ? 'источник' : 'источников'}
    </span>
  </div>
  <ul class="hypothesis__evidence" aria-label="Основания гипотезы">
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

  .hypothesis__evidence {
    display: grid;
    gap: var(--s2);
    margin: 0;
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
