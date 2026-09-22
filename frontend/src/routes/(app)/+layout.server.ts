import { env } from '$env/dynamic/private';
import { error, redirect } from '@sveltejs/kit';
import type { LayoutServerLoad } from './$types';
import type { AccountInfo } from '$lib/types';

const BACKEND_URL = (env.INTERNAL_API_URL || 'http://127.0.0.1:46617').replace(/\/+$/, '');

// Guard рабочего пространства: сессия проверяется на сервере по httpOnly-cookie.
// Разница между «нет доступа» и «сервис не отвечает» сохраняется: при отказе
// бэкенда человек видит страницу ошибки, а не ложный редирект на вход.
export const load: LayoutServerLoad = async ({ request, fetch }) => {
  const cookie = request.headers.get('cookie');
  const url = new URL(request.url);
  // Клиентская навигация приходит как data-запрос (/…/__data.json?...): в
  // адрес возврата заголовки SvelteKit и служебный хвост не попадают.
  const pathname = url.pathname.replace(/\/__data\.json$/, '') || '/';
  const params = new URLSearchParams(url.search);
  params.delete('x-sveltekit-invalidated');
  params.delete('x-sveltekit-traverse');
  const query = params.toString();
  const target = query ? `${pathname}?${query}` : pathname;

  let response: Response;
  try {
    response = await fetch(`${BACKEND_URL}/api/v1/auth/me`, {
      headers: cookie ? { cookie } : {},
      cache: 'no-store',
    });
  } catch {
    error(502, 'Сервис не отвечает. Повторите переход через минуту.');
  }

  if (response.status === 401) {
    redirect(307, `/login?next=${encodeURIComponent(target)}`);
  }

  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    error(response.status, typeof body.detail === 'string' ? body.detail : 'Сервер отклонил запрос.');
  }

  return { account: (await response.json()) as AccountInfo };
};
