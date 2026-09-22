<script lang="ts">
  import { browser } from '$app/environment';
  import { onDestroy, onMount } from 'svelte';
  import Icon from './Icon.svelte';
  import type { IconName } from './icons';

  /**
   * Текучая строка: одно длинное предложение читается горизонтальным полотном,
   * привязанным к прокрутке (GSAP ScrollTrigger). Знаки графического контура
   * стоят внутри фразы вместо союзов, поэтому полотно читается как строка,
   * а не как слайды. До инициализации и при prefers-reduced-motion полотно
   * остаётся обычной прокручиваемой лентой.
   */
  type Piece = { t?: string; m?: IconName | 'curve'; k?: 'soft' | 'hand'; s?: 'sm' | 'lg'; gap?: number };

  const LINE: Piece[] = [
    { t: 'В каждом документе' },
    { m: 'pin', s: 'sm', gap: 26 },
    { t: 'проверяемое знание', k: 'hand', gap: 30 },
    { t: '·', k: 'soft', gap: 34 },
    { t: 'отрывок со страницы', k: 'soft', gap: 22 },
    { m: 'curve', s: 'lg', gap: 30 },
    { t: 'число с условиями', k: 'soft', gap: 22 },
    { m: 'target', s: 'sm', gap: 24 },
    { t: 'и доказательство' },
    { t: '·', k: 'soft', gap: 34 },
    { t: 'вердикт эксперта', k: 'soft', gap: 20 },
    { m: 'graph', s: 'sm', gap: 26 },
    { t: 'новая версия факта', gap: 28 },
    { m: 'curve', s: 'lg', gap: 34 },
    { t: 'тезис без локатора' },
    { t: 'остаётся', k: 'soft', gap: 18 },
    { t: 'непроверенным', k: 'hand', gap: 26 },
  ];

  const SENTENCE =
    'В каждом документе — проверяемое знание: отрывок со страницы, число с условиями и доказательство, вердикт эксперта, новая версия факта. Тезис без локатора остаётся непроверенным.';

  let host = $state<HTMLDivElement | undefined>();
  let track = $state<HTMLDivElement | undefined>();

  let teardown: (() => void) | null = null;

  onMount(async () => {
    if (!browser || !host || !track) return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    const [{ gsap }, { ScrollTrigger }] = await Promise.all([import('gsap'), import('gsap/ScrollTrigger')]);
    gsap.registerPlugin(ScrollTrigger);

    const root = host;
    const rail = track;
    const distance = () => Math.max(0, rail.scrollWidth - window.innerWidth);
    const ctx = gsap.context(() => {
      root.classList.add('ticker--pinned');
      gsap.to(rail, {
        x: () => -distance(),
        ease: 'none',
        scrollTrigger: {
          trigger: root,
          start: 'top top',
          end: () => `+=${Math.round(distance() * 0.25)}`,
          pin: true,
          scrub: 0.7,
          anticipatePin: 1,
          invalidateOnRefresh: true,
          // Прогресс полотна идёт в CSS-переменную: полоса под строкой показывает,
          // сколько предложения уже прочитано, и кадр остаётся собранным на любой
          // точке прокрутки.
          onUpdate: (self) => root.style.setProperty('--tape', self.progress.toFixed(3)),
        },
      });
    }, root);

    teardown = () => ctx.revert();
  });

  onDestroy(() => teardown?.());
</script>

<div class="ticker" bind:this={host}>
  <p class="sr-only">{SENTENCE}</p>

  <div class="ticker__viewport">
    <div class="ticker__track" bind:this={track} aria-hidden="true">
      {#each LINE as piece, i (i)}
        {#if piece.t === '·'}
          <span class="ticker__dot" style="margin-inline:{piece.gap ?? 20}px"></span>
        {:else if piece.t}
          <span
            class="ticker__word{piece.k ? ` ticker__word--${piece.k}` : ''}"
            style="margin-inline-start:{i === 0 ? 0 : (piece.gap ?? 14)}px"
          >
            {piece.t}
          </span>
        {:else if piece.m === 'curve'}
          <svg
            class="ticker__mark ticker__mark--lg"
            style="margin-inline:{piece.gap ?? 24}px"
            viewBox="0 0 128 64"
            fill="none"
            stroke="currentColor"
            stroke-width="3"
            stroke-linecap="round"
          >
            <path d="M4 52C22 52 26 12 46 12s24 40 44 40 20-28 34-32" />
            <circle cx="46" cy="12" r="4.5" fill="currentColor" stroke="none" />
            <circle cx="90" cy="52" r="4.5" fill="currentColor" stroke="none" />
          </svg>
        {:else if piece.m}
          <span class="ticker__mark ticker__mark--{piece.s ?? 'sm'}" style="margin-inline:{piece.gap ?? 20}px">
            <Icon name={piece.m} size={piece.s === 'lg' ? 56 : 30} />
          </span>
        {/if}
      {/each}
      <span class="ticker__end" aria-hidden="true"></span>
    </div>
  </div>

  <div class="ticker__rail" aria-hidden="true">
    <span class="ticker__rail-fill"></span>
  </div>

  <div class="wrap">
    <p class="ticker__foot micro">
      <Icon name="quote" size={16} />
      Строка собрана из того, что продукт делает с каждым импортированным документом.
    </p>
  </div>
</div>

<style>
  .ticker__dot {
    display: block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--action);
    flex: none;
  }

  /* Хвост нужен, чтобы последнее слово уходило за край, а не упиралось в него. */
  .ticker__end {
    width: var(--pad-page);
    flex: none;
  }
</style>
