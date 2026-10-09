<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';

  // Раскрытие без нативного <summary>: маркер треугольника рисует браузер, и он
  // не наследует ни кегль, ни ритм, ни темп системы. Здесь раскрытие — обычная
  // кнопка с aria-expanded, а знак поворачивается вместе с состоянием.
  let {
    title,
    summary,
    id,
    open = $bindable(false),
    size = 'small',
    class: className = '',
    bodyClass = '',
    aside,
    children,
  }: {
    title: string;
    // Свёрнутое раскрытие обязано сказать, что внутри: счётчик называет только то,
    // что скрыто, поэтому он уместно живёт здесь, а не в заголовке раздела.
    summary?: string;
    id: string;
    open?: boolean;
    // Заголовок строки может быть и крупным: тема списка читается заголовком, а
    // не подписью действия.
    size?: 'micro' | 'small' | 'h4';
    class?: string;
    bodyClass?: string;
    // Рядом с заголовком стоят числа и знак, а не действие: их рисует потребитель.
    aside?: Snippet;
    children: Snippet;
  } = $props();

  // Геометрию строки ведёт app.css: заголовок, пояснение и числа должны
  // переноситься целиком, а не сжиматься в нечитаемую колонку на узком экране.
  // Список утилит остаётся литералами: сканер Tailwind читает исходник, а не
  // вычисленное значение.
  const head = $derived(
    [
      'disclosure__head border-0 bg-transparent p-0 text-left transition-colors',
      size === 'h4'
        ? 'text-h4 text-ink font-semibold hover:text-action-ink'
        : size === 'micro'
          ? 'text-micro text-ink-3 hover:text-ink'
          : 'text-small text-ink-3 hover:text-ink',
    ].join(' '),
  );
</script>

<div class="disclosure {className}">
  <button
    type="button"
    class={head}
    aria-expanded={open}
    aria-controls={id}
    onclick={() => (open = !open)}
  >
    <span class="disclosure__title">{title}</span>
    {#if summary}
      <span class="disclosure__summary font-normal">{summary}</span>
    {/if}
    {#if aside}
      <span class="disclosure__aside font-normal">
        {@render aside()}
      </span>
    {/if}
    <Icon name="chevronDown" size={16} class="disclosure__mark" />
  </button>

  <div {id} class="disclosure__body {bodyClass}" hidden={!open}>
    {#if open}{@render children()}{/if}
  </div>
</div>
