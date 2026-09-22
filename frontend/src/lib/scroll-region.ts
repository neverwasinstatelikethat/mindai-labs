/**
 * Прокручиваемость блока становится явной в обе стороны: `tabindex` (то есть
 * прокрутка стрелками с клавиатуры) ставится блоку только когда содержимое
 * действительно выходит за его границы. Без замера подсказка висела бы и на
 * целиком видимом списке, а «обрезано» надо отличать от «больше нет».
 *
 * Блок обязан называть себя сам — `aria-label` или `role`: экшен добавляет
 * только доступность с клавиатуры, семантику региона он не угадывает.
 */
export function scrollRegion(node: HTMLElement) {
  const sync = (): void => {
    const scrolls =
      node.scrollHeight - node.clientHeight > 1 || node.scrollWidth - node.clientWidth > 1;
    if (scrolls) node.tabIndex = 0;
    else node.removeAttribute('tabindex');
  };

  // ResizeObserver ловит смену бокса блока (вьюпорт, перенос метки),
  // MutationObserver — приход и уход содержимого: при закреплённой высоте
  // собственный бокс блока от этого не меняется.
  const resize = new ResizeObserver(sync);
  const mutations = new MutationObserver(sync);
  resize.observe(node);
  mutations.observe(node, { childList: true, subtree: true, characterData: true });
  sync();

  return {
    destroy(): void {
      resize.disconnect();
      mutations.disconnect();
    },
  };
}
