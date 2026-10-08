import { redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

/**
 * Отзывы и экспертные решения перестали быть отдельным разделом: вердикт
 * ставится на месте, там же, где видно числа, то есть в «Числах».
 *
 * `claim` (идентификатор утверждения из старой ссылки) сохраняется: экран
 * открывает шторку вердикта на этом утверждении, поэтому глубокая ссылка
 * приводит человека к тому же объекту, что и раньше.
 */
export const load: PageLoad = ({ url }) => {
  const params = new URLSearchParams(url.searchParams);
  params.set('facet', 'topics');
  const query = params.toString();
  throw redirect(308, query ? `/numbers?${query}` : '/numbers');
};
