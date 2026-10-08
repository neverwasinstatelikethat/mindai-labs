<script lang="ts">
  import type { FullAutoFill } from 'svelte/elements';
  import Icon from './Icon.svelte';

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

  // Идентификатор детерминирован: поле рендерится на сервере, а счётчик
  // экземпляров разошёлся бы между серверной разметкой и гидратацией.
  // Два поля с одним `name` на экране разделяет вызывающая сторона.
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
          <Icon name={revealPassword ? 'eyeOff' : 'eye'} size={19} />
        </button>
      {/if}
    </div>
  {/if}

  {#if error}
    <p class="field__error" id="{id}-err">
      <Icon name="alert" size={15} />
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
