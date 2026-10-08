<script lang="ts">
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import { goto, invalidateAll } from '$app/navigation';
  import { tick } from 'svelte';
  import { api } from '../api';
  import { session } from '../sessionStore.svelte';
  import Icon from './Icon.svelte';
  import type { IconName } from './icons';
  import Sheet from './Sheet.svelte';
  import Button from './Button.svelte';

  export interface NavLink {
    href: string;
    label: string;
    icon: IconName;
    /** Короткое пояснение раздела — едет бегущей строкой при наведении. */
    gloss?: string;
  }

  let { links = [] }: { links?: NavLink[] } = $props();

  let menuOpen = $state(false);
  let sectionsOpen = $state(false);
  let busy = $state(false);
  let dock = $state(false);
  let nav = $state<HTMLElement | undefined>();
  let rail = $state<HTMLDivElement | undefined>();
  let seg = $state<HTMLDivElement | undefined>();
  let accountBox = $state<HTMLDivElement | undefined>();
  let accountBtn = $state<HTMLButtonElement | undefined>();

  const current = $derived(page.url.pathname);

  function isCurrent(href: string): boolean {
    return current === href || current.startsWith(`${href}/`);
  }

  // Подсветка одна на всю полосу: она едет от раздела к разделу, а не
  // перекрашивает пункты по одному.
  function placeIndicator() {
    if (!seg) return;
    const active = seg.querySelector<HTMLElement>('[aria-current="page"]');
    if (!active) {
      seg.style.setProperty('--ind-o', '0');
      return;
    }
    const a = active.getBoundingClientRect();
    const s = seg.getBoundingClientRect();
    seg.style.setProperty('--ind-x', `${a.left - s.left + seg.scrollLeft}px`);
    seg.style.setProperty('--ind-y', `${a.top - s.top}px`);
    seg.style.setProperty('--ind-w', `${a.width}px`);
    seg.style.setProperty('--ind-h', `${a.height}px`);
    seg.style.setProperty('--ind-o', '1');

    const railEl = rail;
    if (railEl && railEl.scrollWidth > railEl.clientWidth + 2) {
      const r = railEl.getBoundingClientRect();
      // Активный раздел должен остаться видимым целиком: это коридор прокрутки.
      const ax = a.left - r.left + railEl.scrollLeft;
      const maxScroll = Math.max(0, railEl.scrollWidth - railEl.clientWidth);
      const lo = Math.max(0, Math.min(ax + a.width - railEl.clientWidth, maxScroll));
      const hi = Math.max(lo, Math.min(ax, maxScroll));
      if (railEl.scrollLeft < lo || railEl.scrollLeft > hi) {
        // Полоса останавливается на границе пункта, а не в середине слова:
        // обрезанный хвост прилипает к логотипу и читается как часть названия
        // продукта (StormIdea + подпись активного раздела).
        const current = railEl.scrollLeft;
        const edges = [
          0,
          ...Array.from(railEl.querySelectorAll<HTMLElement>('.seg__item'), (el) =>
            el.getBoundingClientRect().left - r.left + current,
          ),
        ];
        const inside = edges.filter((edge) => edge >= lo && edge <= hi);
        const chosen = inside.length
          ? inside.reduce((best, edge) =>
              Math.abs(edge - current) < Math.abs(best - current) ? edge : best,
            )
          : lo;
        railEl.scrollLeft = chosen;
      }
    }
  }

  $effect(() => {
    current;
    void tick().then(placeIndicator);
  });

  // Полоса текуча: когда разделов больше, чем места, она прокручивается;
  // после прокрутки экрана навигация собирается в док.
  //
  // Высота шапки — измерение, а не константа: на ширине 1119px и меньше полоса
  // переносится в две строки, и отступы, посчитанные от 72px, прячут якоря под
  // шапку. Значение публикуется в --topbar-h на <html> и живёт вместе с навигацией.
  $effect(() => {
    if (!browser) return;
    const railEl = rail;
    const segEl = seg;
    const navEl = nav;

    let lastWidth = window.innerWidth;
    let published = 0;

    function publishHeight() {
      if (!navEl) return;
      const width = window.innerWidth;
      const height = navEl.offsetHeight;
      // Док короче поднятой полосы: при той же ширине значение только растёт,
      // иначе контент подпрыгивает в момент сворачивания навигации.
      if (width === lastWidth && height <= published) return;
      lastWidth = width;
      published = height;
      document.documentElement.style.setProperty('--topbar-h', `${height}px`);
    }

    function remeasure() {
      railEl?.classList.toggle('pill-nav__links--flow', railEl.scrollWidth > railEl.clientWidth + 2);
      placeIndicator();
      publishHeight();
    }

    const scroller = document.querySelector<HTMLElement>('.app-body');
    // Прокрутку ведёт документ; контейнер учитывается на случай холста со
    // собственным скроллом.
    const readTop = () => Math.max(window.scrollY, scroller ? scroller.scrollTop : 0);
    const onScroll = () => {
      dock = readTop() > 24;
    };

    const observer = new ResizeObserver(remeasure);
    if (segEl) observer.observe(segEl);
    if (navEl) observer.observe(navEl);
    onScroll();
    publishHeight();
    window.addEventListener('scroll', onScroll, { passive: true, capture: true });
    window.addEventListener('resize', remeasure);
    return () => {
      observer.disconnect();
      window.removeEventListener('scroll', onScroll, { capture: true } as EventListenerOptions);
      window.removeEventListener('resize', remeasure);
    };
  });

  // Раскрытие аккаунта — обычное раскрытие (кнопка + список), а не ARIA-меню:
  // Tab сам обходит пункты, Escape закрывает и возвращает фокус кнопке.
  function closeMenu(): void {
    menuOpen = false;
    accountBtn?.focus();
  }

  function onTriggerKeydown(event: KeyboardEvent): void {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    menuOpen = true;
    void tick().then(() => accountBox?.querySelector<HTMLElement>('.menu__item')?.focus());
  }

  function dismissOnLeave(event: FocusEvent): void {
    if (!menuOpen) return;
    const next = event.relatedTarget;
    if (next instanceof Node && accountBox?.contains(next)) return;
    menuOpen = false;
  }

  async function signOut(): Promise<void> {
    busy = true;
    try {
      await session.logout();
      menuOpen = false;
      await goto('/login');
      await invalidateAll();
    } finally {
      busy = false;
    }
  }

</script>

<nav class="pill-nav" class:pill-nav--dock={dock} aria-label="Основная навигация" bind:this={nav}>
  <a class="pill-nav__brand" href="/">
    <span class="brand-mark" aria-hidden="true">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
        <circle cx="8" cy="8" r="3" /><circle cx="16" cy="15" r="3" /><path d="M10.2 10.2l3.6 3.2" />
      </svg>
    </span>
    <span>StormIdea</span>
  </a>

  {#if links.length > 0}
    <div class="pill-nav__links" bind:this={rail}>
      <div class="seg" bind:this={seg}>
        <span class="seg__ind" aria-hidden="true"></span>
        {#each links as link (link.href)}
          <a
            class="seg__item"
            href={link.href}
            aria-current={isCurrent(link.href) ? 'page' : undefined}
            title={link.gloss}
          >
            {link.label}
          </a>
        {/each}
      </div>
    </div>
  {:else}
    <span class="grow"></span>
  {/if}

  <div class="pill-nav__end">
    {#if links.length > 0}
      <!-- «Разделы» доступен на любой ширине: это единственное место, где целиком
           видно, что делает каждый раздел, — прятать его за спиной у того, кто
           впервые открыл продукт на широком экране, нельзя. -->
      <button class="icon-btn pill-nav__burger" type="button" aria-expanded={sectionsOpen} aria-label="Все разделы и что они делают" onclick={() => (sectionsOpen = true)}>
        <Icon name="menu" size={19} />
      </button>
    {/if}

    {#if session.signedIn}
      <div class="pill-nav__account" bind:this={accountBox} onfocusout={dismissOnLeave}>
        <button
          class="account"
          type="button"
          bind:this={accountBtn}
          aria-expanded={menuOpen}
          aria-controls={menuOpen ? 'account-menu' : undefined}
          onclick={() => (menuOpen = !menuOpen)}
          onkeydown={onTriggerKeydown}
        >
          <span class="avatar" aria-hidden="true">{session.initials}</span>
          <span class="account__name">{session.name.split(' ')[0]}</span>
          <Icon name="chevronDown" size={16} />
        </button>

        {#if menuOpen}
          <div class="menu" id="account-menu">
            <div class="menu__head">
              <p class="h4">{session.name}</p>
              <p class="micro muted">{session.account?.email}</p>
            </div>
            <a class="menu__item" href="/account" onclick={() => (menuOpen = false)}>
              <Icon name="user" size={17} /> Профиль и пароль
            </a>
            <button class="menu__item" type="button" disabled={busy} aria-busy={busy || undefined} onclick={() => void signOut()}>
              <Icon name="logout" size={17} /> Выйти
            </button>
          </div>
        {/if}
      </div>
    {:else}
      <a class="navlink" href="/login">Войти</a>
      <Button href="/register" variant="ink" size="sm">Создать аккаунт</Button>
    {/if}
  </div>
</nav>

<svelte:window
  onkeydown={(event) => {
    if (menuOpen && event.key === 'Escape') closeMenu();
  }}
/>

{#if menuOpen}
  <div class="pill-nav__scrim" role="presentation" onclick={() => (menuOpen = false)}></div>
{/if}

{#if sectionsOpen}
  <Sheet title="Разделы рабочего пространства" onclose={() => (sectionsOpen = false)}>
    <div class="flow">
      {#each links as link (link.href)}
        <a
          class="flow__item"
          href={link.href}
          aria-current={isCurrent(link.href) ? 'page' : undefined}
          onclick={() => (sectionsOpen = false)}
        >
          <span class="flow__icon"><Icon name={link.icon} size={19} /></span>
          <span class="flow__rows">
            <span class="flow__label">{link.label}</span>
            <!-- Пояснение читается сразу: бегущая строка прятала единственный
                 ответ на вопрос «а это для чего». -->
            <span class="flow__gloss">{link.gloss ?? 'рабочий раздел'}</span>
          </span>
          <span class="flow__arrow"><Icon name="chevronRight" size={17} /></span>
        </a>
      {/each}

    </div>
  </Sheet>
{/if}

<style>
  /* Бренд — живой контрол, а не подпись: на широком экране ссылка была 30 px,
     пальцем по ней попасть нельзя. Цель поднимается геометрией, кегль текста
     остаётся своим. Ниже 641px цель держит правило тач-целей из app.css (44px),
     поэтому правка работает только там, где его не сработает. */
  @media (min-width: 641px) {
    .pill-nav__brand {
      min-block-size: 32px;
    }
  }

  .pill-nav__account {
    position: relative;
  }

  /* Телефон держит тот же закон, что и 641–1119 px: полоса разделов — одна
     строка, которая не влезает, и она прокручивается (класс `--flow` включает
     маску краёв, а прокрутка удерживает активный раздел в поле зрения).
     Перенос в три строки поднимал шапку до 170 px: пятая часть экрана уходила
     на оболочку раньше, чем человек видел содержимое раздела, и полоса
     переставала быть той же капсулой, что на широком экране. Полный список с
     пояснениями каждого раздела остаётся в «Разделах». */

  /* Показание корпуса — pill-контрол той же природы, что и полоса: свой край,
     свои числа моноширинные, пояснение спрятано в подсказке. */
  .pill-nav__scrim {
    position: fixed;
    inset: 0;
    z-index: calc(var(--z-nav) - 1);
  }

  .account__name {
    font-size: var(--t-small);
    font-weight: 500;
    max-width: 12ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* «Разделы» не прячется на широком экране: шторка — единственное место, где
     целиком видно назначение каждого раздела. */
  @media (max-width: 640px) {
    .account__name {
      display: none;
    }
  }
</style>
