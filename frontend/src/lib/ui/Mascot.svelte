<script lang="ts">
  /**
   * Маскот «Клубка» — живое присутствие агента: голова-сфера из текучего
   * аврора-света, лицо белыми векторными линиями. Состояние приходит извне
   * (покой, слушает, думает, отвечает), взгляд следует за курсором, моргание
   * своё. Приглушённое движение отключает дрейф, орбиту и моргание, оставляя
   * читаемое лицо.
   */
  type MascotState = 'idle' | 'listen' | 'think' | 'say';

  let {
    mood = 'idle',
    size = 56,
    look = true,
    label = '',
  }: {
    mood?: MascotState;
    size?: number;
    look?: boolean;
    label?: string;
  } = $props();

  let host = $state<HTMLSpanElement | undefined>();
  let pointer = $state({ x: 0, y: 0 });

  function onmove(event: PointerEvent) {
    if (!look || !host) return;
    const box = host.getBoundingClientRect();
    pointer = {
      x: (event.clientX - box.left) / box.width - 0.5,
      y: (event.clientY - box.top) / box.height - 0.5,
    };
  }

  function onleave() {
    pointer = { x: 0, y: 0 };
  }
</script>

<span
  class="mascot"
  data-state={mood}
  style="width:{size}px;height:{size}px;--mx:{pointer.x};--my:{pointer.y}"
  bind:this={host}
  onpointermove={onmove}
  onpointerleave={onleave}
  role={label ? 'img' : undefined}
  aria-label={label || undefined}
  aria-hidden={label ? undefined : 'true'}
>
  <svg viewBox="0 0 120 120" width="100%" height="100%" focusable="false">
    <defs>
      <radialGradient id="nk-core" cx="46%" cy="40%" r="64%">
        <stop offset="0%" stop-color="var(--aura-cyan)" stop-opacity="0.98" />
        <stop offset="44%" stop-color="var(--aura-blue)" stop-opacity="0.95" />
        <stop offset="100%" stop-color="var(--aura-violet)" stop-opacity="0.55" />
      </radialGradient>
      <filter id="nk-fog" x="-40%" y="-40%" width="180%" height="180%">
        <feGaussianBlur stdDeviation="8" />
      </filter>
      <filter id="nk-soft" x="-25%" y="-25%" width="150%" height="150%">
        <feGaussianBlur stdDeviation="2.2" />
      </filter>
    </defs>

    <g class="m-mascot__fog" filter="url(#nk-fog)">
      <circle cx="60" cy="58" r="33" fill="url(#nk-core)" opacity="0.55" />
      <ellipse class="m-mascot__drift m-mascot__drift--a" cx="45" cy="47" rx="20" ry="15" fill="var(--aura-cyan)" opacity="0.5" />
      <ellipse class="m-mascot__drift m-mascot__drift--b" cx="76" cy="68" rx="18" ry="14" fill="var(--aura-violet)" opacity="0.55" />
    </g>

    <circle class="m-mascot__body" cx="60" cy="58" r="30" fill="url(#nk-core)" />
    <ellipse class="m-mascot__spec" cx="48" cy="41" rx="9" ry="6" fill="var(--surface)" opacity="0.34" filter="url(#nk-soft)" />

    <g class="m-mascot__orbit">
      <circle cx="60" cy="17" r="2.4" fill="var(--aura-cyan)" />
      <circle cx="99" cy="70" r="1.8" fill="var(--aura-violet)" />
      <circle cx="24" cy="86" r="1.5" fill="var(--aura-blue)" />
    </g>

    <g class="m-mascot__ring">
      <circle cx="60" cy="58" r="38" fill="none" stroke="var(--aura-ring)" stroke-width="1.4" />
    </g>

    <g
      class="m-mascot__face"
      fill="none"
      stroke="var(--aura-face)"
      stroke-width="3.2"
      stroke-linecap="round"
    >
      <path class="m-mascot__brow m-mascot__brow--l" d="M42.5 45.5c3.6-4 9.4-4.4 13.2-1.6" />
      <path class="m-mascot__brow m-mascot__brow--r" d="M64.3 43.9c3.8-2.8 9.6-2.4 13.2 1.6" />
      <g class="m-mascot__eyes" fill="var(--aura-face)" stroke="none">
        <circle cx="48.5" cy="55" r="3.2" />
        <circle cx="71.5" cy="55" r="3.2" />
      </g>
      <path class="m-mascot__nose" d="M59.5 59.5v9.2q0 2.2 2.6 2.2h3.6" />
    </g>
  </svg>
</span>

<style>
  .mascot {
    display: block;
    flex: none;
    line-height: 0;
    /* Взгляд догоняет курсор, но не бежит за ним: коэффициент маленький. */
    --mx: 0;
    --my: 0;
  }

  .mascot svg {
    overflow: visible;
  }

  .m-mascot__face {
    transform: translate(calc(var(--mx) * 4px), calc(var(--my) * 3px));
    transition: transform 420ms var(--ease-soft);
  }

  .m-mascot__eyes {
    /* Параллакс ведёт вся группа лица; глаза остаются под анимацией моргания. */
    transform-box: fill-box;
    transform-origin: center;
  }

  .m-mascot__ring {
    opacity: 0;
    transform-box: fill-box;
    transform-origin: center;
    transition: opacity var(--dur) var(--ease-soft);
  }

  .m-mascot__orbit {
    opacity: 0;
    transform-box: fill-box;
    transform-origin: 60px 58px;
    transition: opacity var(--dur) var(--ease-soft);
  }

  .m-mascot__brow {
    transition: transform var(--dur) var(--ease-enter);
  }

  [data-state='listen'] .m-mascot__ring,
  [data-state='think'] .m-mascot__ring,
  [data-state='say'] .m-mascot__ring {
    opacity: 0.9;
  }

  [data-state='listen'] .m-mascot__brow,
  [data-state='think'] .m-mascot__brow {
    transform: translateY(-1.6px);
  }

  [data-state='think'] .m-mascot__orbit {
    opacity: 1;
  }

  @media (prefers-reduced-motion: no-preference) {
    .mascot svg {
      animation: m-float var(--dur-float) var(--ease-soft) infinite;
    }

    .m-mascot__drift--a {
      animation: m-drift 11s var(--ease-soft) infinite;
    }

    .m-mascot__drift--b {
      animation: m-drift 13s var(--ease-soft) infinite reverse;
    }

    .m-mascot__eyes {
      animation: m-blink var(--dur-blink) steps(1, end) infinite;
    }

    [data-state='think'] .m-mascot__orbit {
      animation: m-orbit 3.4s linear infinite;
    }

    [data-state='say'] .m-mascot__ring,
    [data-state='listen'] .m-mascot__ring {
      animation: m-ring 2.6s var(--ease-soft) infinite;
    }
  }

  @keyframes m-float {
    0%,
    100% {
      transform: translateY(1.5px);
    }

    50% {
      transform: translateY(-2.5px);
    }
  }

  @keyframes m-drift {
    0%,
    100% {
      transform: translate(0, 0) scale(1);
    }

    50% {
      transform: translate(9px, -7px) scale(1.1);
    }
  }

  @keyframes m-blink {
    0%,
    92%,
    100% {
      transform: scaleY(1);
    }

    94%,
    96% {
      transform: scaleY(0.08);
    }
  }

  @keyframes m-orbit {
    to {
      transform: rotate(360deg);
    }
  }

  @keyframes m-ring {
    0%,
    100% {
      transform: scale(1);
      opacity: 0.55;
    }

    50% {
      transform: scale(1.06);
      opacity: 0.95;
    }
  }
</style>
