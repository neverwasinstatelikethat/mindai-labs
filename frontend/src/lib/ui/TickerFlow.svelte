<script lang="ts">
  import { browser } from '$app/environment';
  import { onDestroy, onMount } from 'svelte';
  import Icon from './Icon.svelte';
  import type { IconName } from './icons';

  /**
   * Текучая строка: одно длинное предложение читается горизонтальным полотном,
   * привязанным к прокрутке (GSAP ScrollTrigger). Разделитель частей остаётся
   * элементом вёрстки (`.ticker__dot`), поэтому в текст фразы не попадают
   * служебные знаки. Наведение указателя и фокус внутри полотна останавливают
   * движение, чтобы фразу видно было целиком. До инициализации и при
   * prefers-reduced-motion полотно остаётся обычной прокручиваемой лентой.
   *
   * Полотно не получает точку таба: оно скрыто от ассистивных технологий
   * (`aria-hidden`), а то же предложение лежит рядом обычной строкой для
   * чтения. Лишний останов фокуса без действия уводил бы фокус с формы,
   * поэтому пауза держится на указателе, а движение снимается вместе с
   * размонтированием полосы.
   */
  type Piece = {
    t?: string;
    sep?: boolean;
    m?: IconName | 'curve';
    k?: 'soft' | 'hand';
    s?: 'sm' | 'lg';
    gap?: number;
  };

  const LINE: Piece[] = [
    { t: 'В каждом документе' },
    { m: 'pin', s: 'sm', gap: 26 },
    { t: 'проверяемое знание', k: 'hand', gap: 30 },
    { sep: true, gap: 34 },
    { t: 'фрагмент со страницы', k: 'soft', gap: 22 },
    { m: 'curve', s: 'lg', gap: 30 },
    { t: 'число с условиями', k: 'soft', gap: 22 },
    { m: 'target', s: 'sm', gap: 24 },
    { t: 'и доказательство' },
    { sep: true, gap: 34 },
    { t: 'решение эксперта', k: 'soft', gap: 20 },
    { m: 'graph', s: 'sm', gap: 26 },
    { t: 'новая версия утверждения', gap: 28 },
    { m: 'curve', s: 'lg', gap: 34 },
    { t: 'тезис без адреса в источнике' },
    { t: 'остаётся', k: 'soft', gap: 18 },
    { t: 'непроверенным', k: 'hand', gap: 26 },
  ];

  // Тот же текст отдельной строкой читается ассистивными технологиями: для него
  // разделитель и пауза ничего не значат, поэтому строка обычная.
  const SENTENCE =
    'В каждом документе есть проверяемое знание: фрагмент со страницы, число с условиями и доказательство, решение эксперта, новая версия утверждения. Тезис без адреса в источнике остаётся непроверенным.';

  let host = $state<HTMLDivElement | undefined>();
  let viewport = $state<HTMLDivElement | undefined>();
  let track = $state<HTMLDivElement | undefined>();

  // Закрепление полотна снимается одной функцией: её зовёт и unmount, и смена
  // пользовательского предпочтения движения.
  let releasePin: (() => void) | null = null;
  let destroyed = false;

  // Состояния только для того, что видит читатель: полотно закреплено (значит,
  // движение есть и его можно остановить) и полотно сейчас остановлено.
  let pinned = $state(false);
  let held = $state(false);

  async function mountPin(): Promise<void> {
    if (!host || !track || !viewport || releasePin) return;

    const [{ gsap }, { ScrollTrigger }] = await Promise.all([
      import('gsap'),
      import('gsap/ScrollTrigger'),
    ]);
    // Полоса могла размонтироваться, пока подкачивался GSAP.
    if (destroyed) return;
    gsap.registerPlugin(ScrollTrigger);

    const root = host;
    const rail = track;
    // Область чтения берётся сюда же, где проверяется: `viewport` остаётся
    // реактивным состоянием, поэтому снятие слушателей держит уже суженный
    // элемент, а не обращается к состоянию, которое к teardown может стать
    // неопределённым. Иначе слушатели повиснут на мёртвом узле.
    const surface = viewport;
    const distance = () => Math.max(0, rail.scrollWidth - window.innerWidth);

    // Полотно живёт, пока кадр двигается; наведение и фокус ставят именно его,
    // а не прокрутку страницы: фраза остаётся перед глазами, пока человек её
    // смотрит, и отпускается, когда человек уходит со строки.
    let tape: { pause(): void; resume(): void } | null = null;
    const hold = () => {
      tape?.pause();
      held = true;
    };
    const release = () => {
      tape?.resume();
      held = false;
    };

    const ctx = gsap.context(() => {
      // Класс закрепления держит состояние: он же включает clip и рельс в app.css.
      pinned = true;
      tape = gsap.to(rail, {
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
          // какая часть предложения уже открыта на экране, и кадр остаётся собранным
          // на любой точке прокрутки.
          onUpdate: (self) => root.style.setProperty('--tape', self.progress.toFixed(3)),
        },
      });
    }, root);

    // Пауза держится на окне чтения, а не на всей сцене: указатель должен
    // попасть именно на строку.
    surface.addEventListener('pointerenter', hold);
    surface.addEventListener('pointerleave', release);
    surface.addEventListener('focusin', hold);
    surface.addEventListener('focusout', release);

    releasePin = () => {
      surface.removeEventListener('pointerenter', hold);
      surface.removeEventListener('pointerleave', release);
      surface.removeEventListener('focusin', hold);
      surface.removeEventListener('focusout', release);
      ctx.revert();
      root.style.removeProperty('--tape');
      pinned = false;
      held = false;
      tape = null;
      releasePin = null;
    };
  }

  let motion: MediaQueryList | null = null;
  let onMotionChange: (() => void) | null = null;

  onMount(async () => {
    if (!browser || !host || !track) return;

    motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    if (!motion.matches) await mountPin();

    // Предпочтение движения переключается без перезагрузки: полотно либо
    // закрепляется, либо отдаёт горизонтальную прокрутку человеку.
    onMotionChange = () => {
      if (motion?.matches) releasePin?.();
      else void mountPin();
    };
    motion.addEventListener('change', onMotionChange);
  });

  onDestroy(() => {
    destroyed = true;
    if (motion && onMotionChange) motion.removeEventListener('change', onMotionChange);
    releasePin?.();
  });
</script>

<div class="ticker" class:ticker--pinned={pinned} class:ticker--held={held} bind:this={host}>
  <p class="sr-only">{SENTENCE}</p>

  <div class="ticker__viewport" bind:this={viewport}>
    <div class="ticker__track" bind:this={track} aria-hidden="true">
      {#each LINE as piece, i (i)}
        {#if piece.sep}
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
    {#if pinned}
      <p class="ticker__hint micro muted">
        {held
          ? 'полотно стоит, читайте спокойно'
          : 'указатель над строкой останавливает полотно'}
      </p>
    {/if}
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

  /* Пауза читается по состоянию рельса, а не по догадке: движение встало,
     полоса прогресса приглушена. */
  .ticker--held .ticker__rail {
    opacity: 0.45;
    transition: opacity var(--dur-fast) var(--ease-soft);
  }

  .ticker__rail {
    transition: opacity var(--dur-fast) var(--ease-soft);
  }

  .ticker__hint {
    margin-top: var(--s2);
  }
</style>
