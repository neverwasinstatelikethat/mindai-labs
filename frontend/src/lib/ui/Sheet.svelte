<script module lang="ts">
  // Счётчик общий для всех шторок документа: заголовок идентификатором быть не
  // может, две шторки «Источник находки» (в списке и в карте) давали один id, и
  // aria-labelledby первой вёл на вторую.
  let serial = 0;
</script>

<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';

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

  const uid = `sheet-${++serial}`;
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
    aria-labelledby={`${uid}-title`}
    aria-describedby={description ? `${uid}-desc` : undefined}
    style="--sheet-width: {width}"
    tabindex="-1"
    bind:this={panel}
    onkeydown={trap}
  >
    <header class="sheet__head">
      <div>
        <h2 class="h3" id={`${uid}-title`}>{title}</h2>
        {#if description}<p class="micro muted" id={`${uid}-desc`}>{description}</p>{/if}
      </div>
      <button class="icon-btn" type="button" aria-label="Закрыть" onclick={onclose}>
        <Icon name="close" size={18} />
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
    gap: var(--s4);
    padding: clamp(var(--s5), 3vw, var(--s7)) clamp(var(--s5), 3vw, var(--s7)) var(--s3);
    border-bottom: 1px solid var(--line-soft);
  }

  /* Заголовок шторки — часто полная формулировка находки: перенос по любому
     слову, иначе крестик уезжает к середине строки. */
  .sheet__head h2 {
    margin: 0;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .sheet__head p {
    margin: var(--s1) 0 0;
    max-width: var(--maxw-measure);
  }

  /* Прокрутку ведёт тело: шапка и действия остаются видимыми. */
  .sheet__body {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    padding: clamp(var(--s4), 2.5vw, var(--s5)) clamp(var(--s5), 3vw, var(--s7));
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  .sheet__foot {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: var(--s3);
    padding: var(--s4) clamp(var(--s5), 3vw, var(--s7))
      calc(var(--s4) + env(safe-area-inset-bottom));
    border-top: 1px solid var(--line-soft);
  }
</style>
