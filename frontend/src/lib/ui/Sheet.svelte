<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    title,
    description = '',
    onclose,
    width = '720px',
    children,
    footer,
  }: {
    title: string;
    description?: string;
    onclose: () => void;
    width?: string;
    children?: Snippet;
    footer?: Snippet;
  } = $props();

  const id = $derived(`sheet-${title.replace(/\s+/g, '-').toLowerCase()}`);
  let panel = $state<HTMLDivElement | null>(null);

  // Ловушка фокуса: под шторкой остаётся рабочий экран, и Tab не имеет права
  // уводить фокус туда — диалог перечисляет свои контролы при каждом нажатии,
  // потому что содержимое (и список действий) меняется на глазах.
  const FOCUSABLE =
    'a[href], button:not([disabled]), input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function trap(event: KeyboardEvent): void {
    if (event.key !== 'Tab' || !panel) return;
    const items = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (!items.length) {
      event.preventDefault();
      panel.focus();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (!panel.contains(active)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
      return;
    }
    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  $effect(() => {
    const previous = document.activeElement as HTMLElement | null;
    document.body.style.overflow = 'hidden';
    // data-autofocus важнее порядка разметки, иначе фокус уезжал бы на крестик.
    const preferred = panel?.querySelector<HTMLElement>('[data-autofocus]');
    (preferred ?? panel?.querySelector<HTMLElement>(FOCUSABLE) ?? panel)?.focus();
    return () => {
      document.body.style.overflow = '';
      previous?.focus();
    };
  });
</script>

<svelte:window
  onkeydown={(event) => {
    if (event.key === 'Escape') onclose();
  }}
/>

<div
  class="veil"
  role="presentation"
  onclick={(event) => {
    if (event.target === event.currentTarget) onclose();
  }}
>
  <div
    class="sheet"
    role="dialog"
    aria-modal="true"
    aria-labelledby={`${id}-title`}
    aria-describedby={description ? `${id}-desc` : undefined}
    style="--sheet-width: {width}"
    tabindex="-1"
    bind:this={panel}
    onkeydown={trap}
  >
    <header class="sheet__head">
      <div>
        <h2 class="h3" id={`${id}-title`}>{title}</h2>
        {#if description}<p class="micro muted" id={`${id}-desc`}>{description}</p>{/if}
      </div>
      <button class="icon-btn" type="button" aria-label="Закрыть" onclick={onclose}>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true">
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>
    </header>

    <div class="sheet__body">
      {@render children?.()}
    </div>

    {#if footer}
      <footer class="sheet__foot">
        {@render footer()}
      </footer>
    {/if}
  </div>
</div>

<style>
  .veil {
    place-items: center;
  }

  .sheet {
    width: min(100%, var(--sheet-width, 720px));
  }

  /* Контейнер ловит фокус только как запасной стоп для Tab, когда в диалоге
     нет ни одного контрола: собственной обводки ему не нужно, у контролов она есть. */
  .sheet:focus:not(:focus-visible) {
    outline: none;
  }

  .sheet__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--s5);
    margin-bottom: var(--s5);
  }

  .sheet__body {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  .sheet__foot {
    display: flex;
    justify-content: flex-end;
    gap: var(--s3);
    margin-top: var(--s6);
    padding-top: var(--s5);
    border-top: 1px solid var(--line-soft);
  }
</style>
