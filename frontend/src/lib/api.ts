import { env } from '$env/dynamic/public';
import type {
  AccountInfo,
  ActivityEntry,
  AuditEntry,
  AnswerHistoryPage,
  AnswerPayload,
  ClaimHistory,
  ComparisonTable,
  ConflictCandidate,
  ConflictReview,
  CorpusStats,
  DashboardData,
  DocumentReceipt,
  EntityMergeProposal,
  EvaluationRun,
  EvolutionExperiment,
  EvolutionProposal,
  ExpertDecision,
  FeedbackResult,
  FindingApiStatus,
  FindingListItem,
  GoldCase,
  GraphSnapshot,
  HypothesisSignal,
  LlmUsageSummary,
  MergeReviewAction,
  QueryResponse,
  SystemStatus,
} from './types';

const API_URL = env.PUBLIC_API_URL || '/backend';

export class ApiError extends Error {
  readonly status: number;
  // Различаем 401 (сессии нет — редиректим на вход) и 403 (сессия есть,
  // разрешения нет — объясняем на месте, не выбрасывая из рабочего экрана).
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

function headersFor(extra?: Record<string, string>): Record<string, string> {
  return { 'Content-Type': 'application/json', ...extra };
}

async function failure(response: Response): Promise<ApiError> {
  const body = await response.json().catch(() => ({ detail: response.statusText }));
  // Код ответа остаётся в `ApiError.status` (на нём различают 401 и 403), а в
  // строку для человека не попадает: «HTTP 500» не объясняет ни последствие, ни
  // следующий шаг. Без `detail` от сервиса отдаём общее состояние, а не код.
  const message =
    typeof body.detail === 'string' && body.detail.trim() !== ''
      ? body.detail
      : 'Сервис не ответил. Повторите действие; если повтор даст то же самое, обратитесь к администратору.';
  if (response.status === 401) onUnauthorized?.();
  return new ApiError(message, response.status);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    credentials: 'include',
    ...init,
    headers: headersFor(init?.headers as Record<string, string> | undefined),
  });
  if (!response.ok) throw await failure(response);
  return response.json() as Promise<T>;
}

// Окно списочного маршрута: сами строки в теле, полное число подходящих записей
// в X-Total-Count, а явное замечание о неполноте в X-Window-Note.
export type WindowPage<T> = {
  items: T;
  // Отсутствующее или непонятное значение отдаётся как null: экран обязан
  // отличать «полное число не прочитано» от «записей ноль».
  total: number | null;
  // Пустая строка значит, что замечания сервера не было, а не что оно пустое.
  windowNote: string;
};

// X-Window-Note едет percent-encoded UTF-8, потому что HTTP-заголовки передаются
// в latin-1: читается через decodeURIComponent. Нераскодированное значение
// остаётся как пришло: замечание сервера важнее приведённого вида.
function windowNoteOf(response: Response): string {
  const raw = response.headers.get('X-Window-Note')?.trim() ?? '';
  if (raw === '') return '';
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

// Списки с окном: тело остаётся массивом, поэтому для таких маршрутов — отдельный
// вариант с заголовками; `request()` читает только тело и остальные вызовы не
// трогает.
async function requestWithTotal<T>(path: string, init?: RequestInit): Promise<WindowPage<T>> {
  const response = await fetch(`${API_URL}${path}`, {
    credentials: 'include',
    ...init,
    headers: headersFor(init?.headers as Record<string, string> | undefined),
  });
  if (!response.ok) throw await failure(response);
  const items = (await response.json()) as T;
  const raw = response.headers.get('X-Total-Count');
  const parsed = raw === null ? Number.NaN : Number(raw);
  const total = Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
  return { items, total, windowNote: windowNoteOf(response) };
}

// /health/ready отвечает 503, когда модели нет, и тело при этом остаётся честным
// SystemStatus. Бросать на любом не-2xx здесь значит терять различие, ради которого
// экран держит два состояния: «сервис отвечает ограниченно» и «показания не пришли».
async function requestStatus(): Promise<SystemStatus> {
  const response = await fetch(`${API_URL}/health/ready`, { credentials: 'include' });
  const body = (await response.json().catch(() => null)) as SystemStatus | null;
  if (body && typeof body.status === 'string' && typeof body.model_mode === 'string') {
    return body;
  }
  if (response.ok) {
    throw new ApiError('Ответ о состоянии сервиса прочитан неверно', response.status);
  }
  throw await failure(response);
}

export const api = {
  status: () => requestStatus(),

  // ── Сессия ──────────────────────────────────────────────────────
  me: () => request<AccountInfo>('/api/v1/auth/me'),
  register: (input: { email: string; display_name: string; password: string }) =>
    request<AccountInfo>('/api/v1/auth/register', { method: 'POST', body: JSON.stringify(input) }),
  login: (input: { email: string; password: string }) =>
    request<AccountInfo>('/api/v1/auth/login', { method: 'POST', body: JSON.stringify(input) }),
  logout: () => request<{ status: 'logged_out' }>('/api/v1/auth/logout', { method: 'POST' }),
  changePassword: (input: { current_password: string; new_password: string }) =>
    request<AccountInfo>('/api/v1/auth/password', { method: 'POST', body: JSON.stringify(input) }),
  updateProfile: (displayName: string) =>
    request<AccountInfo>('/api/v1/auth/profile', {
      method: 'PATCH',
      body: JSON.stringify({ display_name: displayName }),
    }),

  // ── Знания и запросы ────────────────────────────────────────────
  graph: () => request<GraphSnapshot>('/api/v1/graph'),
  // Находки: окно режет сервер (`limit` 1..1000 со своим потолком, `offset` от 0),
  // поэтому полное число подходящих утверждений читается из X-Total-Count, а
  // оговорка о неполном чтении — из X-Window-Note, а не из длины массива.
  findings: (
    subject?: string,
    status?: FindingApiStatus,
    limit?: number,
    offset?: number,
  ) => {
    const params = new URLSearchParams();
    if (subject) params.set('subject', subject);
    if (status) params.set('status', status);
    if (limit !== undefined) params.set('limit', String(limit));
    if (offset !== undefined) params.set('offset', String(offset));
    const qs = params.toString();
    return requestWithTotal<FindingListItem[]>(`/api/v1/findings${qs ? `?${qs}` : ''}`);
  },
  // Аналитические гипотезы — отдельные типизированные сигналы с источниками;
  // они не читаются из списка извлечённых утверждений.
  hypotheses: (limit = 50, offset = 0) =>
    requestWithTotal<HypothesisSignal[]>(`/api/v1/hypotheses?limit=${limit}&offset=${offset}`),
  hypothesis: (id: string) =>
    request<HypothesisSignal>(`/api/v1/hypotheses/${encodeURIComponent(id)}`),
  // Расхождения (находки со статусом «оспаривается») тоже приходят окном: без
  // `limit` сервер отдал бы первые 200 и экран назвал бы их всеми, поэтому
  // полное число читается заголовком, а не длиной массива.
  conflicts: (limit = 200, offset = 0) =>
    requestWithTotal<FindingListItem[]>(`/api/v1/conflicts?limit=${limit}&offset=${offset}`),
  // Очередь противоречий: пары тезисов, которые ждёт эксперт. Окно режет
  // сервер, поэтому полное число пар читается из заголовка, а не из длины
  // массива: показаны первые, а всего в очереди — другое число.
  conflictCandidates: (limit = 50, offset = 0) =>
    requestWithTotal<ConflictCandidate[]>(
      `/api/v1/conflicts/candidates?limit=${limit}&offset=${offset}`,
    ),
  // Решение по паре требует экспертного права: 403 проверяется раньше поиска
  // пары, 404 — пары нет в текущем корпусе, 409 — такое решение уже записано.
  conflictReview: (candidateId: string, confirmed: boolean) =>
    request<ConflictReview>(`/api/v1/conflicts/candidates/${encodeURIComponent(candidateId)}/review`, {
      method: 'POST',
      body: JSON.stringify({ confirmed }),
    }),
  corpusStats: () => request<CorpusStats>('/api/v1/corpus/stats'),
  upload: async (file: File) => {
    const form = new FormData();
    form.append('file', file);
    form.append('language', 'ru');
    const response = await fetch(`${API_URL}/api/v1/documents/upload`, {
      method: 'POST',
      body: form,
      credentials: 'include',
    });
    if (!response.ok) throw await failure(response);
    return response.json() as Promise<DocumentReceipt>;
  },
  query: (question: string) =>
    request<QueryResponse>('/api/v1/query', {
      method: 'POST',
      body: JSON.stringify({ question, language: 'ru', mode: 'hybrid' }),
    }),
  answerHistory: (limit = 30, offset = 0) =>
    request<AnswerHistoryPage>(`/api/v1/answers?limit=${limit}&offset=${offset}`),
  savedAnswer: (queryId: string) =>
    request<AnswerPayload>(`/api/v1/answers/${encodeURIComponent(queryId)}`),
  // feedback возвращает FeedbackResult: решение эксперта сохраняется всегда, а
  // proposal генерирует модель — без живого LLM его нет, и это не ошибка.
  // supersede срабатывает только при verdict='correct' вместе с finding_id
  // и correction, поэтому исправление обязано указывать на конкретное
  // утверждение, а не на ответ целиком.
  feedback: (input: {
    query_id: string;
    finding_id: string | null;
    verdict: 'accept' | 'reject' | 'correct';
    comment: string;
    correction?: string;
  }) => request<FeedbackResult>('/api/v1/feedback', { method: 'POST', body: JSON.stringify(input) }),
  // Очередь предложений читается сервером окном (`limit`/`offset`, потолок
  // страницы 200), полное число подходящих записей приходит в X-Total-Count:
  // экран обязан называть прочитанное и полное число раздельно, молчаливый срез
  // на потолке страницы запрещён (Р3).
  proposals: (limit = 200, offset = 0) =>
    requestWithTotal<EvolutionProposal[]>(`/api/v1/proposals?limit=${limit}&offset=${offset}`),
  // Durable-ленты с серверным окном (`limit` + `offset`): тело — массив
  // страницы, полное число подходящих записей — в X-Total-Count. Экран,
  // показывающий ленту как журнал, обязан называть прочитанное и полное
  // число раздельно: молчаливый срез запрещён.
  experiments: (limit = 50, offset = 0) =>
    requestWithTotal<EvolutionExperiment[]>(
      `/api/v1/experiments?limit=${limit}&offset=${offset}`,
    ),
  // Выборку A/B задаёт сервер (MIN_AB_CASES): один кейс не различает варианты, а
  // решение о смене политики агента принимается именно по этому прогону.
  runExperiment: (proposalId: string) =>
    request<EvolutionExperiment>(`/api/v1/proposals/${proposalId}/experiment`, {
      method: 'POST',
    }),
  review: (proposalId: string, accepted: boolean) =>
    request<EvolutionProposal>(`/api/v1/proposals/${proposalId}/review`, {
      method: 'POST',
      body: JSON.stringify({ accepted }),
    }),
  evaluations: (limit = 200, offset = 0) =>
    requestWithTotal<EvaluationRun[]>(`/api/v1/evaluations?limit=${limit}&offset=${offset}`),
  goldCases: () => request<GoldCase[]>('/api/v1/evaluations/gold'),
  claimHistory: (claimId: string) => request<ClaimHistory>(`/api/v1/claims/${claimId}/history`),
  compare: (question: string, entities: string[], dimensions: string[] = []) =>
    request<ComparisonTable>('/api/v1/compare', {
      method: 'POST',
      body: JSON.stringify({ question, entities, dimensions, language: 'ru' }),
    }),
  dashboard: () => request<DashboardData>('/api/v1/dashboard'),
  // Журнал собственного аккаунта для профиля: /audit требует audit:read и
  // отдаёт акты всех, а здесь actor_id определяет сервер по сессии. Лента
  // читается окном: полное число актов — в X-Total-Count, а не в длине страницы.
  myActivity: (limit = 50, offset = 0) =>
    requestWithTotal<ActivityEntry[]>(`/api/v1/me/activity?limit=${limit}&offset=${offset}`),
  // Журнал корпуса — под тем же audit:read, что и право в профиле: без него
  // обещание «журнал действий аккаунтов» нечем было бы проверить. Окно и
  // полное число — те же, что у ленты собственных актов.
  audit: (limit = 50, offset = 0) =>
    requestWithTotal<AuditEntry[]>(`/api/v1/audit?limit=${limit}&offset=${offset}`),
  // Очередь склеек сущностей под правом `proposal:review`: в `rationale` бывают
  // ссылки на закрытые источники, поэтому список отдаётся не всем подряд. Окно
  // режет сервер, полное число пар — в X-Total-Count.
  mergeProposals: (limit = 50, offset = 0) =>
    requestWithTotal<EntityMergeProposal[]>(
      `/api/v1/entity-resolution/proposals?limit=${limit}&offset=${offset}`,
    ),
  // Принятие, отказ и откат склейки. Идемпотентность держит сервер: повтор того
  // же решения отвечает 409, исчезнувшая пара — 404, отсутствие права — 403.
  mergeReview: (proposalId: string, action: MergeReviewAction) =>
    request<EntityMergeProposal>(
      `/api/v1/entity-resolution/proposals/${encodeURIComponent(proposalId)}/review`,
      { method: 'POST', body: JSON.stringify({ action }) },
    ),
  // История собственных экспертных решений: `/audit` закрыт `audit:read` и отдаёт
  // акты всех аккаунтов, а `actor_id` здесь подставляет сервер по сессии.
  myDecisions: (limit = 50, offset = 0) =>
    requestWithTotal<ExpertDecision[]>(`/api/v1/decisions?limit=${limit}&offset=${offset}`),
  // «Сколько стоили мои вопросы»: права не нужны, виден только свой аккаунт.
  myUsage: (days = 30) => request<LlmUsageSummary>(`/api/v1/me/usage?days=${days}`),
};

export function apiUrl(path: string): string {
  return `${API_URL}${path}`;
}
