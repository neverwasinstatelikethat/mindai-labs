import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

/**
 * Экран сравнения чисел переехал в фасет «Числа» раздела «Гипотезы».
 *
 * Редирект серверный и постоянный: старые ссылки живут в документах, в
 * закладках аналитиков и в ответах агента, поэтому 301/308, а не 307.
 * Входящие параметры уходят вместе с человеком: `q` остаётся поиском по темам,
 * `entities` — отбором по субъектам, всё остальное сохраняется как есть.
 */
export const load: PageLoad = ({ url }) => {
  const params = new URLSearchParams(url.searchParams);
  params.set('facet', 'numbers');
  const query = params.toString();
  throw redirect(308, query ? `/findings?${query}` : '/findings?facet=numbers');
};
