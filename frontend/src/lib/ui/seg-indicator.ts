// Единственная формула положения для общего индикатора полосы (.seg): её считают
// навигация разделов и любой переключатель вида. Раздельные реализации давали
// две полосы, которые на одном экране едут по-разному.

/** Текущий пункт полосы: фильтр жмётся как pressed, раздел — как current. */
export function segActive(rail: HTMLElement): HTMLElement | null {
  return rail.querySelector<HTMLElement>('[aria-pressed="true"], [aria-current="page"]');
}

/**
 * Ставит скользящую подсветку на пункт. Координаты берутся из видимого сдвига
 * элемента, а не из offsetLeft: полоса крутится по горизонтали, и индикатор
 * обязан ехать вместе с подписью.
 */
export function placeSegIndicator(rail: HTMLElement, active: HTMLElement | null): void {
  if (!active) {
    rail.style.setProperty('--ind-o', '0');
    return;
  }
  const box = active.getBoundingClientRect();
  const frame = rail.getBoundingClientRect();
  rail.style.setProperty('--ind-x', `${box.left - frame.left + rail.scrollLeft}px`);
  rail.style.setProperty('--ind-y', `${box.top - frame.top}px`);
  rail.style.setProperty('--ind-w', `${box.width}px`);
  rail.style.setProperty('--ind-h', `${box.height}px`);
  rail.style.setProperty('--ind-o', '1');
}

/** Показывает пункт целиком, если он встал за краем полосы. */
export function revealSegItem(rail: HTMLElement, active: HTMLElement, padding = 12): void {
  const box = active.getBoundingClientRect();
  const frame = rail.getBoundingClientRect();
  if (box.left < frame.left) rail.scrollLeft -= frame.left - box.left + padding;
  else if (box.right > frame.right) rail.scrollLeft += box.right - frame.right + padding;
}
