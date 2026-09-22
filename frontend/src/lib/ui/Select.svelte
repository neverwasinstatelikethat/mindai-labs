<script lang="ts">
  let {
    label,
    name,
    value = $bindable(''),
    options,
    hint = '',
    error = '',
    disabled = false,
    class: className = '',
  }: {
    label: string;
    name: string;
    value?: string;
    options: { value: string; label: string }[];
    hint?: string;
    error?: string;
    disabled?: boolean;
    class?: string;
  } = $props();

  const id = $derived(`s-${name}`);
  // Подсказка и ошибка объявлены вслух так же, как в Field: экранная лупа
  // обязана вести от подписи к пояснению, а не искать их глазами.
  const describedBy = $derived([hint ? `${id}-hint` : '', error ? `${id}-err` : ''].filter(Boolean).join(' '));
</script>

<div class="field {className}">
  <label class="field__label" for={id}>{label}</label>
  <select
    id={id}
    {name}
    {disabled}
    class="select"
    aria-invalid={error ? 'true' : undefined}
    aria-describedby={describedBy || undefined}
    bind:value
  >
    {#each options as option (option.value)}
      <option value={option.value}>{option.label}</option>
    {/each}
  </select>
  {#if error}
    <p class="field__error" id="{id}-err">{error}</p>
  {:else if hint}
    <p class="field__hint" id="{id}-hint">{hint}</p>
  {/if}
</div>
