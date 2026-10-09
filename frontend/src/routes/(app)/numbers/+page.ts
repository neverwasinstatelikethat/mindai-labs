import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

/**
 * Раздел «Числа» удалён: он дублировал «Гипотезы», только с другим срезом тех же
 * находок. Числа, расхождения и пробелы стали фасетами `/findings`.
 *
 * Редирект постоянный: адрес раздела успели напечатать в ответах агента и
 * сохранить в закладки, а ссылка, которую человек уже ввёл, не имеет права
 * вести в пустоту. `q` и `entities` переходят вместе с человеком.
 */
export const load: PageLoad = ({ url }) => {
  const params = new URLSearchParams(url.searchParams);
  const facet = params.get('facet');
  if (facet === 'gaps') params.set('facet', 'gaps');
  else params.set('facet', 'numbers');
  throw redirect(308, `/findings?${params.toString()}`);
};
