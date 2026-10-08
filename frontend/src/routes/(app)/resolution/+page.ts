import { redirect } from '@sveltejs/kit';

// Очередь склеек больше не раздел: это фасет «Находок», поэтому старый адрес
// `/resolution` отдаёт серверный 308 на `/findings` с фасетом `?facet=merges`.
// Серверный, а не клиентский: закладки и ссылки из ответов получают итоговый
// адрес, а не пустую страницу с клиентским прыжком навигации.
//
// Отбор и глубокая ссылка важнее самого раздела: query и hash переносятся
// целиком, а `facet` дописывается только когда его явно не запросили, чтобы
// `/resolution?facet=findings` не превращался в очередь склеек.
export function load({ url }: { url: URL }): never {
  const next = new URL('/findings', url.origin);
  next.search = url.search;
  if (!next.searchParams.has('facet')) next.searchParams.set('facet', 'merges');
  if (url.hash) next.hash = url.hash;
  throw redirect(308, `${next.pathname}${next.search}${next.hash}`);
}
