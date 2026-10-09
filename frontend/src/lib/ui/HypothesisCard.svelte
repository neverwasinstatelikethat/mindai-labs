<script lang="ts">
  import { countOf } from '$lib/format';
  import { locatorsOf } from '$lib/numbers';
  import { hypothesisKindLabel } from '$lib/terms';
  import type { HypothesisSignal } from '$lib/types';
  import Disclosure from './Disclosure.svelte';

  // Цитата открывается листом у страницы, а не ссылкой на этот же экран:
  // `/findings?hypothesis=…&evidence=…` вела обратно туда же и показывала
  // полосой то, что уже лежало в этом же раскрытии. В строке списка остаётся
  // отсылка (документ и место), а сам текст цитаты — в листе.
  let {
    signal,
    onquote,
  }: { signal: HypothesisSignal; onquote?: (index: number) => void } = $props();

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
          <button class="hypo__source" type="button" onclick={() => onquote?.(index)}>
            <span class="hypo__doc">{item.source_title || 'Источник без названия'}</span>
            <span class="hypo__where">
              {#each locatorsOf(item) as part (part.kind)}
                <span class="locator">{part.kind} <span class="num">{part.value}</span></span>
              {/each}
            </span>
            <span class="micro hypo__open">Прочитать цитату</span>
          </button>
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

  /* Отсылка — действие: она открывает лист с текстом цитаты. Подложка у
     строки появляется вместе с наведением, а не рамкой: рядов может быть
     несколько, и обведённые строки читались бы списком карточек. */
  .hypo__source {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--s1) var(--s3);
    width: 100%;
    min-height: 36px;
    padding: var(--s2) var(--s3);
    border: 0;
    border-radius: var(--r-sm);
    background: none;
    color: inherit;
    font: inherit;
    text-align: start;
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-soft);
  }

  .hypo__source:hover {
    background: var(--surface-sunk);
  }

  .hypo__doc {
    color: var(--ink-2);
    font-size: var(--t-small);
    overflow-wrap: anywhere;
  }

  .hypo__where {
    display: inline-flex;
    flex-wrap: wrap;
    gap: var(--s2);
    color: var(--ink-3);
    font-size: var(--t-micro);
  }

  .hypo__open {
    color: var(--action-ink);
  }

  .hypo__source:focus-visible {
    outline: 2px solid var(--action-ink);
    outline-offset: 2px;
  }
</style>
