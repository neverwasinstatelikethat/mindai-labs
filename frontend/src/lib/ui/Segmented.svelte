<script lang="ts">
  import { tick } from 'svelte';
  import { placeSegIndicator, revealSegItem, segActive } from './seg-indicator';

  // Единственный способ «переключить вид» во всём интерфейсе: полоса с общим
  // скользящим индикатором. Залитая кнопка остаётся действием, выбор вида —
  // только пилюлей в полосе, иначе два режима читаются как два отдельных вызова.
  let {
    items,
    value = $bindable(),
    label,
    id,
    class: className = '',
    onchange,
  }: {
    items: { value: string; label: string; hint?: string }[];
    value?: string;
    label: string;
    id?: string;
    class?: string;
    onchange?: (next: string) => void;
  } = $props();

  let seg = $state<HTMLDivElement | undefined>();

  function place(): void {
    const rail = seg;
    if (!rail) return;
    placeSegIndicator(rail, segActive(rail));
  }

  // Выбранный вид может стоять за краем полосы на узком экране — его показываем,
  // но только по нажатию: прокрутка на каждом измерении дёргала бы полосу.
  function reveal(): void {
    const rail = seg;
    const active = rail ? segActive(rail) : null;
    if (rail && active) revealSegItem(rail, active);
  }

  function select(next: string): void {
    if (value === next) return;
    value = next;
    onchange?.(next);
    reveal();
  }

  // Индикатор позиционируется в пикселях, поэтому измеряется заново после
  // обновления разметки, при смене геометрии полосы, при её прокрутке и после
  // прихода шрифта: до этого подписи имеют одну ширину, после — другую.
  $effect(() => {
    void value;
    void items;
    void tick().then(place);
  });

  $effect(() => {
    const rail = seg;
    if (!rail) return;
    const observer = new ResizeObserver(() => place());
    observer.observe(rail);
    rail.addEventListener('scroll', place, { passive: true });
    void document.fonts?.ready.then(place);
    return () => {
      observer.disconnect();
      rail.removeEventListener('scroll', place);
    };
  });
</script>

<div class="seg {className}" {id} role="group" aria-label={label} bind:this={seg}>
  <span class="seg__ind" aria-hidden="true"></span>
  {#each items as item (item.value)}
    <button
      class="seg__item"
      type="button"
      title={item.hint}
      aria-pressed={value === item.value}
      onclick={() => select(item.value)}
    >
      {item.label}
    </button>
  {/each}
</div>
