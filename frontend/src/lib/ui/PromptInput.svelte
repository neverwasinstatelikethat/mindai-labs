<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';
  import Mascot from './Mascot.svelte';

  /**
   * Композер запроса: поле, которое растёт вместе с текстом, на узком экране
   * остаётся полосой на всю ширину, а на рабочем листе прикрепляется ко дну.
   * Кольцо ауры и маскот следят за состоянием: покой → слушает (фокус) →
   * думает (отправка). Отправка — Enter, перенос строки — Shift+Enter.
   */
  let {
    value = $bindable(''),
    placeholder = 'Что найти в корпусе?',
    label = 'Вопрос к корпусу',
    busy = false,
    disabled = false,
    variant = 'inline',
    mascot = true,
    autofocus = false,
    suggestions = [] as string[],
    hint = '',
    rows = 1,
    name = 'prompt',
    onsubmit,
    leading,
    tools,
  }: {
    value?: string;
    placeholder?: string;
    label?: string;
    busy?: boolean;
    disabled?: boolean;
    variant?: 'hero' | 'inline' | 'docked';
    mascot?: boolean;
    autofocus?: boolean;
    suggestions?: string[];
    hint?: string;
    rows?: number;
    name?: string;
    onsubmit?: (value: string) => void;
    leading?: Snippet;
    tools?: Snippet;
  } = $props();

  let box = $state<HTMLTextAreaElement | undefined>();
  let focused = $state(false);

  const id = $derived(`p-${name}`);

  const canSend = $derived(Boolean(value.trim()) && !busy && !disabled);
  const mood = $derived.by(() => {
    if (busy) return 'think' as const;
    if (focused) return 'listen' as const;
    return 'idle' as const;
  });

  // Поле тянется по содержимому, но не дальше трети экрана: дальше скролл
  // внутри textarea, чтобы композер не выталкивал лист.
  const growsNatively = $derived(typeof CSS !== 'undefined' && CSS.supports('field-sizing', 'content'));
  $effect(() => {
    value;
    if (!box || growsNatively) return;
    box.style.height = 'auto';
    box.style.height = `${Math.min(box.scrollHeight, 280)}px`;
  });

  $effect(() => {
    if (autofocus && box) {
      const node = box;
      queueMicrotask(() => node.focus());
    }
  });

  function send() {
    if (!canSend) return;
    onsubmit?.(value.trim());
  }

  function onkeydown(event: KeyboardEvent) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  }

  function pick(suggestion: string) {
    value = suggestion;
    box?.focus();
  }
</script>

<form
  class="prompt prompt--{variant}"
  onsubmit={(event) => {
    event.preventDefault();
    send();
  }}
>
  {#if mascot}
    <span class="prompt__lead"><Mascot {mood} size={variant === 'hero' ? 48 : 40} /></span>
  {/if}

  <label class="sr-only" for={id}>{label}</label>
  <textarea
    id={id}
    class="prompt__input"
    {placeholder}
    {rows}
    {disabled}
    bind:this={box}
    bind:value
    onfocus={() => (focused = true)}
    onblur={() => (focused = false)}
    onkeydown={onkeydown}
    aria-busy={busy || undefined}
  ></textarea>

  <div class="prompt__bar">
    <div class="prompt__tools">
      {@render leading?.()}
      {#each suggestions as suggestion (suggestion)}
        <button class="chip" type="button" onclick={() => pick(suggestion)}>
          <Icon name="sparkles" size={14} />
          {suggestion}
        </button>
      {/each}
      {@render tools?.()}
    </div>

    {#if hint}
      <span class="prompt__hint">{hint}</span>
    {:else}
      <span class="prompt__hint">Enter — отправить · Shift + Enter — строка</span>
    {/if}

    <button
      class="prompt__send"
      type="submit"
      disabled={!canSend}
      aria-busy={busy || undefined}
      aria-label="Отправить запрос"
    >
      {#if busy}
        <span class="spinner spinner--quiet" aria-hidden="true"></span>
      {:else}
        <Icon name="arrowUp" size={20} />
      {/if}
    </button>
  </div>
</form>
