<script lang="ts">
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import { goto, invalidateAll } from '$app/navigation';
  import { tick } from 'svelte';
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
  let dirs = $state<Record<string, number>>({});

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

    if (rail && rail.scrollWidth > rail.clientWidth + 2) {
      rail.scrollLeft = a.left - s.left + rail.scrollLeft - (rail.clientWidth - a.width) / 2;
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
    const readTop = () => (scroller ? scroller.scrollTop : window.scrollY);
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

  // Бегущая строка въезжает с той стороны, с которой подвели курсор.
  function sense(event: PointerEvent, href: string) {
    const row =
      event.target instanceof Element ? event.target.closest<HTMLElement>('.flow__item') : null;
    if (!row) return;
    const box = row.getBoundingClientRect();
    dirs[href] = event.clientY > box.top + box.height / 2 ? -1 : 1;
  }

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
    <span>Научный Клубок</span>
  </a>

  {#if links.length > 0}
    <div class="pill-nav__links" bind:this={rail}>
      <div class="seg" bind:this={seg}>
        <span class="seg__ind" aria-hidden="true"></span>
        {#each links as link (link.href)}
          <a class="seg__item" href={link.href} aria-current={isCurrent(link.href) ? 'page' : undefined}>
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
      <button class="icon-btn pill-nav__burger" type="button" aria-expanded={sectionsOpen} aria-label="Разделы" onclick={() => (sectionsOpen = true)}>
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
          style="--flow-dir:{dirs[link.href] ?? 1}"
          aria-current={isCurrent(link.href) ? 'page' : undefined}
          onpointerenter={(event) => sense(event, link.href)}
          onclick={() => (sectionsOpen = false)}
        >
          <span class="flow__icon"><Icon name={link.icon} size={19} /></span>
          <span class="flow__rows">
            <span class="flow__label">{link.label}</span>
            <span class="flow__marquee">
              <span class="flow__strip">
                {#each [0, 1] as half (half)}
                  <span class="flow__chunk">{link.label} — {link.gloss ?? 'рабочий раздел'}</span>
                {/each}
              </span>
            </span>
          </span>
          <span class="flow__arrow"><Icon name="chevronRight" size={17} /></span>
        </a>
      {/each}
    </div>
  </Sheet>
{/if}

<style>
  .pill-nav__account {
    position: relative;
  }

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

  .flow__strip {
    display: flex;
    width: max-content;
  }

  .flow__chunk {
    padding-inline-end: var(--s8);
    white-space: nowrap;
  }

  /* Порог совпадает с переносом полосы в app.css: бургер появляется ровно там,
     где разделы уходят во вторую строку (≤1119px). */
  @media (min-width: 1120px) {
    .pill-nav__burger {
      display: none;
    }
  }

  @media (max-width: 700px) {
    .account__name {
      display: none;
    }
  }
</style>
