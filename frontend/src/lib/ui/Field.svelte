<script lang="ts">
  import type { FullAutoFill } from 'svelte/elements';

  let {
    label,
    name,
    type = 'text',
    value = $bindable(''),
    placeholder = '',
    hint = '',
    error = '',
    required = false,
    autocomplete = 'off' as FullAutoFill,
    maxlength,
    min,
    step,
    disabled = false,
    rows = 4,
    autofocus = false,
    inputmode,
    class: className = '',
    onenter,
  }: {
    label: string;
    name: string;
    type?: 'text' | 'email' | 'password' | 'search' | 'number' | 'textarea';
    value?: string;
    placeholder?: string;
    hint?: string;
    error?: string;
    required?: boolean;
    autocomplete?: FullAutoFill;
    maxlength?: number;
    min?: number;
    step?: number;
    disabled?: boolean;
    rows?: number;
    autofocus?: boolean;
    inputmode?: 'text' | 'email' | 'numeric';
    class?: string;
    onenter?: () => void;
  } = $props();

  const id = $derived(`f-${name}`);

  // Фокус по запросу — действием, чтобы не развешивать autofocus-атрибут.
  function auto(node: HTMLElement, enabled: boolean): void {
    if (enabled) node.focus();
  }
  const describedBy = $derived([hint ? `${id}-hint` : '', error ? `${id}-err` : ''].filter(Boolean).join(' '));
  let revealPassword = $state(false);
  const resolvedType = $derived(type === 'password' && revealPassword ? 'text' : type);
</script>

<div class="field {className}">
  <label class="field__label" for={id}>
    {label}
    {#if required}<span class="muted" aria-hidden="true"> *</span>{/if}
  </label>

  {#if type === 'textarea'}
    <textarea
      id={id}
      {name}
      {rows}
      {placeholder}
      {disabled}
      use:auto={autofocus}
      class="textarea"
      aria-invalid={error ? 'true' : undefined}
      aria-describedby={describedBy || undefined}
      bind:value
    ></textarea>
  {:else}
    <div class="field__control">
      <input
        id={id}
        {name}
        type={resolvedType}
        {placeholder}
        {required}
        {autocomplete}
        {maxlength}
        {min}
        {step}
        {inputmode}
        {disabled}
        use:auto={autofocus}
        class="input"
        aria-invalid={error ? 'true' : undefined}
        aria-describedby={describedBy || undefined}
        bind:value
        onkeydown={(event) => {
          if (event.key === 'Enter' && onenter) {
            event.preventDefault();
            onenter();
          }
        }}
      />
      {#if type === 'password'}
        <button
          class="field__toggle"
          type="button"
          aria-pressed={revealPassword}
          aria-label={revealPassword ? 'Скрыть пароль' : 'Показать пароль'}
          onclick={() => (revealPassword = !revealPassword)}
        >
          <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            {#if revealPassword}
              <path d="M4 4l16 16" /><path d="M9.6 6.9A9.8 9.8 0 0 1 12 6.5c6 0 9.5 5.5 9.5 5.5a17 17 0 0 1-2.9 3.4M6.2 8.6A16.6 16.6 0 0 0 2.5 12S6 17.5 12 17.5c1 0 2-.2 2.9-.5" />
            {:else}
              <path d="M2.5 12S6 6.5 12 6.5 21.5 12 21.5 12 18 17.5 12 17.5 2.5 12 2.5 12Z" /><circle cx="12" cy="12" r="2.8" />
            {/if}
          </svg>
        </button>
      {/if}
    </div>
  {/if}

  {#if error}
    <p class="field__error" id="{id}-err">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true">
        <path d="M12 4.5l8.5 15H3.5l8.5-15Z" /><path d="M12 10v4M12 16.8h.01" />
      </svg>
      {error}
    </p>
  {:else if hint}
    <p class="field__hint" id="{id}-hint">{hint}</p>
  {/if}
</div>

<style>
  .field__control {
    position: relative;
    display: flex;
  }

  .field__toggle {
    position: absolute;
    top: 50%;
    right: var(--s3);
    display: grid;
    place-items: center;
    width: 38px;
    height: 38px;
    transform: translateY(-50%);
    border: 0;
    border-radius: var(--r-pill);
    background: none;
    color: var(--ink-3);
    cursor: pointer;
  }

  .field__toggle:hover {
    background: var(--line-soft);
    color: var(--ink);
  }

  /* Под кнопкой показа пароля текст не должен уходить: запас справа — геометрия
     этого поля, а не общего слоя. */
  .field__control:has(.field__toggle) .input {
    padding-inline-end: 62px;
  }
</style>
