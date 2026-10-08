import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

/**
 * Экран сравнения чисел переехал в раздел «Числа» (фасет сходимости).
 *
 * Редирект серверный и постоянный: старые ссылки живут в документах, в
 * закладках аналитиков и в ответах агента, поэтому 301/308, а не 307.
 * Входящие параметры уходят вместе с человеком: `q` остаётся поиском по темам,
 * `entities` — отбором по субъектам, всё остальное сохраняется как есть.
 */
export const load: PageLoad = ({ url }) => {
  const params = new URLSearchParams(url.searchParams);
  params.set('facet', 'topics');
  const query = params.toString();
  throw redirect(308, query ? `/numbers?${query}` : '/numbers');
};
