import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

/**
 * Список оспоренных утверждений и очередь на решение эксперта переехали в
 * фасет «Числа» раздела «Гипотезы».
 *
 * Редирект постоянный: ссылки на этот адрес остаются в исторических ответах
 * агента и в закладках. `q`, если он был в ссылке, становится поиском по темам.
 */
export const load: PageLoad = ({ url }) => {
  const params = new URLSearchParams(url.searchParams);
  params.set('facet', 'numbers');
  const query = params.toString();
  throw redirect(308, query ? `/findings?${query}` : '/findings?facet=numbers');
};
