<script lang="ts">
  import { countOf } from '$lib/format';
  import { hypothesisKindLabel } from '$lib/terms';
  import type { HypothesisSignal } from '$lib/types';
  import Disclosure from './Disclosure.svelte';
  import SourceRef from './SourceRef.svelte';

  let { signal }: { signal: HypothesisSignal } = $props();

  function sourceHref(evidenceIndex: number): string {
    const params = new URLSearchParams();
    params.set('hypothesis', signal.id);
    params.set('evidence', String(evidenceIndex));
    return `/findings?${params.toString()}`;
  }

  const sourceCount = $derived(new Set(signal.evidence.map((item) => item.document_id)).size);

  const kindLabel = $derived(hypothesisKindLabel(signal.kind));

  const evidenceSummary = $derived(
    `${countOf(signal.evidence.length, 'цитата', 'цитаты', 'цитат')} из ${countOf(
      sourceCount,
      'источник',
      'источника',
      'источников',
    )}`,
  );
</script>

<article class="hypo" aria-labelledby={`hypothesis-${signal.id}`}>
  {#if kindLabel}
    <span class="hypo__kind">{kindLabel}</span>
  {/if}
  <h3 class="h4 hypo__statement" id={`hypothesis-${signal.id}`}>{signal.statement}</h3>
  {#if signal.proposal}
    <p class="hypo__proposal">
      <span class="hypo__label">Следующая проверка:</span>
      {signal.proposal}
    </p>
  {/if}
  <Disclosure
    id={`hypothesis-evidence-${signal.id}`}
    size="small"
    title="Основание"
    summary={evidenceSummary}
  >
    <ul class="hypo__evidence">
      {#each signal.evidence as item, index (`${signal.id}-evidence-${index}`)}
        <li>
          <a class="hypo__source" href={sourceHref(index)}>
            <SourceRef evidence={item} quote />
          </a>
        </li>
      {/each}
    </ul>
  </Disclosure>
</article>

<style>
  .hypo {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    min-width: 0;
    padding: var(--s4) 0;
  }

  /* Тип гипотезы — тихая подпись над выводом: коралловый чернильный цвет спорил
     со ссылкой и делал из категории действие. */
  .hypo__kind {
    color: var(--ink-4);
    font-size: var(--t-micro);
    font-weight: 500;
  }

  .hypo__statement {
    margin: 0;
  }

  .hypo__proposal {
    margin: 0;
    color: var(--ink-2);
    font-size: var(--t-small);
    max-width: var(--maxw-measure);
  }

  .hypo__label {
    color: var(--ink-3);
  }

  /* Класс стоит на самом списке: на теге компонента он не получил бы scoped-хеш
     родителя, и правило молча переставало применяться. */
  .hypo__evidence {
    display: grid;
    gap: var(--s3);
    margin: var(--s3) 0 0;
    padding: 0;
    list-style: none;
  }

  .hypo__source {
    display: block;
    color: inherit;
    text-decoration: none;
    border-radius: var(--r-sm);
  }

  .hypo__source:focus-visible {
    outline: 2px solid var(--action-ink);
    outline-offset: 2px;
  }
</style>
