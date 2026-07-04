import { env } from '$env/dynamic/private';
import type { RequestHandler } from './$types';

// Базовый URL бэкенда.
// В Docker: INTERNAL_API_URL=http://backend:8000 (native fetch resolves Docker hostnames).
// В dev: fallback на 127.0.0.1:18000 (бэкенд маппит 8000→18000 на хосте).
const BACKEND_URL = env.INTERNAL_API_URL || 'http://127.0.0.1:18000';

const proxy: RequestHandler = async ({ params, request, url }) => {
  const target = `${BACKEND_URL}/${params.path}${url.search}`;

  // Копируем заголовки из исходного запроса
  const headers = new Headers(request.headers);
  headers.delete('host');
  headers.delete('content-length');
  headers.delete('connection');

  // Гарантируем наличие ролевых заголовков для бэкенда
  if (!headers.get('X-User-Role')) {
    headers.set('X-User-Role', 'researcher');
  }
  if (!headers.get('X-User-Id')) {
    headers.set('X-User-Id', 'anonymous');
  }

  // Используем нативный fetch вместо SvelteKit-обёрнутого,
  // чтобы обойти валидацию tldts для URLs с нестандартными TLD
  const response = await fetch(target, {
    method: request.method,
    headers,
    body: request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body,
    duplex: 'half',
  } as RequestInit);

  // Пробрасываем ответ, сохраняя статус и заголовки
  const responseHeaders = new Headers(response.headers);
  responseHeaders.delete('transfer-encoding');

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders,
  });
};

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
