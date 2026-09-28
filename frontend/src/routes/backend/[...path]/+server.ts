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

// Причина отмены по таймауту: с ней fetch отклоняется этим объектом, а не
// безликим AbortError, и путь ошибки отличает молчание бэкенда от отмены
// человеком. AbortError сам по себе оба случая не различает.
const HEADER_TIMEOUT = new Error('ожидание заголовков бэкенда истекло');

// Причина отмены из-за уходившего клиента (сокет закрыт до ответа бэкенда).
const CLIENT_GONE = new Error('клиент закрыл соединение');

// Заголовки, которые нельзя переносить на уже раскодированное тело:
// content-encoding описывает байты, которые fetch уже развернул.
const HOP_BY_HOP_RESPONSE = ['transfer-encoding', 'content-encoding', 'content-length', 'connection'];

// Часть входящего соединения, которая нужна прокси. @types/node в этом проекте не
// подключены, а adapter-node кладёт в `event.platform` только сам запрос
// (см. @sveltejs/adapter-node/ambient.d.ts), поэтому описываем её локально.
type CloseWatcher = {
  once: (event: 'close', listener: () => void) => void;
  removeListener: (event: 'close', listener: () => void) => void;
};

type NodePlatform = { req?: { socket?: CloseWatcher } };

function encodePath(path: string): string {
  return path.split('/').map(encodeURIComponent).join('/');
}

const proxy: RequestHandler = async (event) => {
  const { params, request, url } = event;
  const target = `${BACKEND_URL}/${encodePath(params.path)}${url.search}`;

  const headers = new Headers(request.headers);
  headers.delete('host');
  headers.delete('content-length');
  headers.delete('connection');

  // Серверная аутентификация: личность берётся только из сессионного cookie,
  // который проксирует да не подменяется. Заголовков роли здесь больше нет.

  const controller = new AbortController();
  let clientGone = false;
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort(HEADER_TIMEOUT);
  }, HEADER_TIMEOUT_MS);

  // Отмена браузерного запроса переносится на запрос к бэкенду. Без этого строки
  // закрытая вкладка и «Остановить проход» трогали только запись ответа: fetch к
  // бэкенду доживал до таймаута заголовков, а прогон на бэкенде — до своего
  // дедлайна, удерживая слот агентного контура (AGENT_MAX_CONCURRENT_RUNS) уже
  // ненужной работой; следующие аналитики получали 429.
  const abortFromClient = () => {
    clientGone = true;
    controller.abort(request.signal.reason ?? CLIENT_GONE);
  };
  if (request.signal.aborted) {
    abortFromClient();
  } else {
    request.signal.addEventListener('abort', abortFromClient, { once: true });
  }

  // Для POST с уже прочитанным телом SvelteKit не отменяет `request.signal` при
  // обрыве соединения: сигнал привязан к входящему телу, а не к ответу. Про уход
  // клиента в боевом контуре знает только сокет — закрытый сокет означает, что
  // ответ доставить некому, и восходящий запрос тоже больше не нужен. В dev
  // (`vite`) `event.platform` нет: там остаются сигнал запроса и отмена тела.
  // Слушатель снимается, как только заголовки получены, — сокет переживает
  // несколько запросов подряд (keep-alive), и висящие замыкания на нём не нужны.
  const socket = (event.platform as NodePlatform | undefined)?.req?.socket;
  if (socket) socket.once('close', abortFromClient);
  const detachSocket = () => socket?.removeListener('close', abortFromClient);

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
  } catch {
    clearTimeout(timer);
    detachSocket();
    // Человек ушёл раньше ответа бэкенда: списывать это на бэкенд нельзя, и читать
    // ответ всё равно некому — 499 («клиент закрыл соединение») вместо 502 про
    // отказ сервиса, чтобы лог прокси не путал отмену с недоступным бэкендом.
    if (clientGone) {
      return new Response(null, { status: 499 });
    }
    // Здесь остаются два разных отказа бэкенда: молчание до заголовков и
    // незакрытое соединение. Одна строка на обоих была бы неправдой.
    const detail = timedOut
      ? `Бэкенд не ответил за ${HEADER_TIMEOUT_MS / 1000} с.`
      : `Бэкенд недоступен: ${BACKEND_URL}`;
    return json({ detail, proxy_unreachable: true }, { status: 502 });
  }
  // Таймаут снимается, как только заголовки пришли: тело SSE не ограничено этими
  // 15 секундами. Слушатель сокета больше не нужен — с этого момента ответом
  // владеет SvelteKit: при закрытом соединении он отменяет переданное ему тело, и
  // восходящий поток закрывается (проверено на потоке в 20 с). `request.signal`
  // остаётся висеть: для GET с дочитываемым телом он дёргается и в этой фазе.
  clearTimeout(timer);
  detachSocket();

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
