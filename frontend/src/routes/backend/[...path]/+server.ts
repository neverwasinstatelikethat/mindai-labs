import { env } from '$env/dynamic/private';
import type { RequestHandler } from './$types';
import { json } from '@sveltejs/kit';

// Базовый URL бэкенда.
// В Docker: INTERNAL_API_URL=http://backend:8000 (native fetch resolves Docker hostnames).
// В dev: fallback на 127.0.0.1:46617 (бэкенд маппит 8000→46617 на хосте).
const BACKEND_URL = (env.INTERNAL_API_URL || 'http://127.0.0.1:46617').replace(/\/+$/, '');

// Таймаут действует только на ожидание заголовков ответа. Тело не ограничивается:
// /api/v1/query/stream отдаёт SSE минутами, а LLM-запрос отвечает долго сам по себе.
const HEADER_TIMEOUT_MS = 15_000;

// Заголовки, которые нельзя переносить на уже раскодированное тело:
// content-encoding описывает байты, которые fetch уже развернул.
const HOP_BY_HOP_RESPONSE = ['transfer-encoding', 'content-encoding', 'content-length', 'connection'];

function encodePath(path: string): string {
  return path.split('/').map(encodeURIComponent).join('/');
}

const proxy: RequestHandler = async ({ params, request, url }) => {
  const target = `${BACKEND_URL}/${encodePath(params.path)}${url.search}`;

  const headers = new Headers(request.headers);
  headers.delete('host');
  headers.delete('content-length');
  headers.delete('connection');

  // Серверная аутентификация: личность берётся только из сессионного cookie,
  // который проксирует да не подменяется. Заголовков роли здесь больше нет.

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), HEADER_TIMEOUT_MS);

  let response: Response;
  try {
    // Нативный fetch вместо SvelteKit-обёрнутого, чтобы обойти валидацию tldts
    // для URLs с нестандартными TLD.
    response = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body,
      duplex: 'half',
      signal: controller.signal,
    } as RequestInit);
  } catch (reason) {
    clearTimeout(timer);
    const aborted = reason instanceof Error && reason.name === 'AbortError';
    const detail = aborted
      ? `Бэкенд не ответил за ${HEADER_TIMEOUT_MS / 1000} с.`
      : `Бэкенд недоступен: ${BACKEND_URL}`;
    return json({ detail, proxy_unreachable: true }, { status: 502 });
  }
  clearTimeout(timer);

  const responseHeaders = new Headers(response.headers);
  for (const name of HOP_BY_HOP_RESPONSE) responseHeaders.delete(name);

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
