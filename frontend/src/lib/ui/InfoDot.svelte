<script module lang="ts">
  let serial = 0;
</script>

<script lang="ts">
  import type { Snippet } from 'svelte';
  import { browser } from '$app/environment';
  import Icon from './Icon.svelte';

  /**
   * Раскрытие по «i» — единственный дом объяснения механизма.
   *
   * Методика, способ измерения и смысл статуса читаются по требованию, а не
   * стоят в одной строке с данными. Иначе слов о числе на экране становится
   * больше, чем самого числа, и читатель перестаёт видеть результат.
   *
   * Это поповер, а не шторка: пояснение остаётся в контексте строки, которую
   * объясняет, и не уводит человека с места.
   */
  let {
    title,
    body = '',
    align = 'start',
    label,
    children,
  }: {
    title: string;
    body?: string;
    align?: 'start' | 'end';
    /** Доступное имя кнопки. По умолчанию «Пояснение: {title}». */
    label?: string;
    children?: Snippet;
  } = $props();

  let open = $state(false);
  let uid = $state('');
  let flip = $state(false);
  let flipBlock = $state(false);
  let mobileViewport = $state(false);
  let button = $state<HTMLButtonElement | undefined>();
  let pop = $state<HTMLDivElement | undefined>();
  let host = $state<HTMLSpanElement | undefined>();

  function place(): void {
    if (!browser || !host || !pop) return;
    const box = pop.getBoundingClientRect();
    const anchor = host.getBoundingClientRect();
    mobileViewport = window.innerWidth <= 640;
    if (mobileViewport) {
      const left = Math.min(Math.max(8, anchor.left), window.innerWidth - box.width - 8);
      const below = anchor.bottom + 8;
      const above = anchor.top - box.height - 8;
      const top = below + box.height <= window.innerHeight - 8
        ? below
        : above >= 8
          ? above
          : Math.max(8, window.innerHeight - box.height - 8);
      pop.style.setProperty('--infodot-x', `${left}px`);
      pop.style.setProperty('--infodot-y', `${top}px`);
      flip = false;
      flipBlock = false;
      return;
    }
    const left = align === 'end' ? anchor.right - box.width : anchor.left;
    flip = left < 8 || left + box.width > window.innerWidth - 8;
    flipBlock = anchor.bottom + box.height > window.innerHeight - 12 && anchor.top - box.height > 12;
  }

  function close(restore = true): void {
    if (!open) return;
    open = false;
    flip = false;
    flipBlock = false;
    if (restore) button?.focus();
  }

  function toggle(): void {
    if (open) {
      close();
      return;
    }
    if (!uid) uid = `info-${++serial}`;
    open = true;
    requestAnimationFrame(() => {
      place();
      pop?.focus();
    });
  }

  // Escape закрывает пояснение и возвращает фокус кнопке; Tab уходит из
  // поповера наружу, а не зацикливается внутри одной фразы.
  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.stopPropagation();
      close();
    }
  }

  function onWindowPointer(event: PointerEvent): void {
    if (!open) return;
    const target = event.target as Node | null;
    if (target && host?.contains(target)) return;
    close(false);
  }
</script>

<svelte:window onpointerdown={onWindowPointer} />

<span class={`infodot ${open ? 'infodot--open' : ''}`} bind:this={host}>
  <button
    class="infodot__btn"
    type="button"
    bind:this={button}
    aria-expanded={open}
    aria-controls={open ? uid : undefined}
    aria-label={label ?? `Пояснение: ${title}`}
    onclick={toggle}
    onkeydown={onKeydown}
  >
    <Icon name={open ? 'close' : 'info'} size={14} />
  </button>

  {#if open}
    <div
      class={`infodot__pop ${align === 'end' ? 'infodot__pop--end' : ''} ${flip ? 'infodot__pop--flip' : ''} ${flipBlock ? 'infodot__pop--above' : ''} ${mobileViewport ? 'infodot__pop--mobile' : ''}`}
      id={uid}
      role="dialog"
      aria-label={title}
      tabindex="-1"
      bind:this={pop}
      onkeydown={onKeydown}
    >
      <p class="infodot__title">{title}</p>
      {#if body}<p class="infodot__body">{body}</p>{/if}
      {@render children?.()}
    </div>
  {/if}
</span>

<style>
  .infodot {
    position: relative;
    display: inline-flex;
    flex: none;
  }

  /* Цель нажатия 34 px при видимом кружке 22 px. Отрицательное поле возвращает
     строке её прежнюю высоту: «i» стоит вплотную к числам в плотной строке, и
     раздувать ради касания саму строку нельзя. Кружок рисуется псевдоэлементом,
     поэтому рамка фокуса и подсветка остаются на видимом круге, а не на
     прозрачной поля вокруг него. */
  .infodot__btn {
    position: relative;
    display: inline-grid;
    place-items: center;
    inline-size: 34px;
    block-size: 34px;
    margin: -6px;
    padding: 0;
    border: 0;
    background: none;
    color: var(--ink-3);
    cursor: pointer;
  }

  .infodot__btn::before {
    content: "";
    position: absolute;
    inset: 6px;
    border: 1px solid var(--line);
    border-radius: var(--r-pill);
    background: var(--surface);
    transition:
      border-color var(--dur-fast) var(--ease-soft),
      background-color var(--dur-fast) var(--ease-soft);
  }

  .infodot__btn > :global(svg) {
    position: relative;
  }

  .infodot__btn:hover {
    color: var(--ink);
  }

  .infodot__btn:hover::before {
    border-color: var(--line-strong);
    background: var(--surface-raised);
  }

  .infodot__btn:active {
    transform: scale(0.94);
  }

  .infodot__btn:focus-visible {
    outline: none;
  }

  .infodot__btn:focus-visible::before {
    outline: 2px solid var(--action);
    outline-offset: 2px;
    border-color: var(--line-strong);
  }

  .infodot--open .infodot__btn {
    color: var(--ink);
  }

  .infodot--open .infodot__btn::before {
    border-color: var(--line-strong);
    background: var(--surface-sunk);
  }

  .infodot__pop {
    position: absolute;
    top: calc(100% + var(--s2));
    inset-inline-start: 0;
    z-index: var(--z-dock);
    inline-size: min(34rem, calc(100vw - 2rem));
    max-block-size: min(70dvh, 34rem);
    overflow: auto;
    padding: var(--s4);
    border: 1px solid var(--line);
    border-radius: var(--r-sm);
    background: var(--surface-raised);
    box-shadow: var(--shadow-lift);
    text-align: start;
    animation: info-in var(--dur-fast) var(--ease-enter);
  }

  .infodot__pop--end {
    inset-inline-start: auto;
    inset-inline-end: 0;
  }

  /* У края экрана пояснение уходит в противоположную сторону, а не обрезается:
     текст важнее симметрии. */
  .infodot__pop--flip {
    inset-inline-start: auto;
    inset-inline-end: 0;
  }

  .infodot__pop--end.infodot__pop--flip {
    inset-inline-end: auto;
    inset-inline-start: 0;
  }

  .infodot__pop--above {
    top: auto;
    bottom: calc(100% + var(--s2));
  }

  .infodot__pop--mobile {
    position: fixed;
    inset: var(--infodot-y) auto auto var(--infodot-x);
    inline-size: min(34rem, calc(100vw - 1rem));
  }

  .infodot__pop:focus {
    outline: none;
  }

  .infodot__pop:focus-visible {
    box-shadow: var(--shadow-lift), var(--shadow-focus);
  }

  .infodot__title {
    margin: 0;
    font-size: var(--t-small);
    font-weight: 600;
    line-height: var(--lh-dense);
    color: var(--ink);
  }

  .infodot__body {
    margin: var(--s2) 0 0;
    font-size: var(--t-small);
    line-height: var(--lh-body);
    letter-spacing: var(--tr-body);
    color: var(--ink-2);
    text-wrap: pretty;
  }

  .infodot__pop :global(.kv),
  .infodot__pop :global(.stack) {
    margin: var(--s3) 0 0;
  }

  @keyframes info-in {
    from {
      opacity: 0;
      transform: translateY(-4px);
    }
    to {
      opacity: 1;
      transform: none;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .infodot__pop {
      animation: none;
    }
  }
</style>
