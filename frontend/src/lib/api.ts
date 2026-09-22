import { env } from '$env/dynamic/public';
import type {
  AccountInfo,
  ClaimHistory,
  ComparisonTable,
  CorpusStats,
  DashboardData,
  DocumentReceipt,
  EvaluationRun,
  EvolutionExperiment,
  EvolutionProposal,
  ExportFormat,
  FeedbackResult,
  FindingApiStatus,
  FindingListItem,
  GoldCase,
  GraphSnapshot,
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
  const message = typeof body.detail === 'string' ? body.detail : `HTTP ${response.status}`;
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

async function requestRaw(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`${API_URL}${path}`, {
    credentials: 'include',
    ...init,
    headers: headersFor(init?.headers as Record<string, string> | undefined),
  });
  if (!response.ok) throw await failure(response);
  return response;
}

export const api = {
  status: () => request<SystemStatus>('/health/ready'),

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
  findings: (subject?: string, status?: FindingApiStatus) => {
    const params = new URLSearchParams();
    if (subject) params.set('subject', subject);
    if (status) params.set('status', status);
    const qs = params.toString();
    return request<FindingListItem[]>(`/api/v1/findings${qs ? `?${qs}` : ''}`);
  },
  conflicts: () => request<FindingListItem[]>('/api/v1/conflicts'),
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
  proposals: () => request<EvolutionProposal[]>('/api/v1/proposals'),
  experiments: (limit = 50) =>
    request<EvolutionExperiment[]>(`/api/v1/experiments?limit=${limit}`),
  runExperiment: (proposalId: string) =>
    request<EvolutionExperiment>(`/api/v1/proposals/${proposalId}/experiment?max_cases=1`, {
      method: 'POST',
    }),
  review: (proposalId: string, accepted: boolean) =>
    request<EvolutionProposal>(`/api/v1/proposals/${proposalId}/review`, {
      method: 'POST',
      body: JSON.stringify({ accepted }),
    }),
  evaluations: (limit = 200) => request<EvaluationRun[]>(`/api/v1/evaluations?limit=${limit}`),
  goldCases: () => request<GoldCase[]>('/api/v1/evaluations/gold'),
  claimHistory: (claimId: string) => request<ClaimHistory>(`/api/v1/claims/${claimId}/history`),
  compare: (question: string, entities: string[], dimensions: string[] = []) =>
    request<ComparisonTable>('/api/v1/compare', {
      method: 'POST',
      body: JSON.stringify({ question, entities, dimensions, language: 'ru' }),
    }),
  // Экспортируется серверная копия ответа по query_id: присланный клиентом
  // AnswerPayload из контракта убран (иначе ACL фильтровал бы клиентские данные).
  export: (queryId: string, format: ExportFormat) =>
    requestRaw('/api/v1/export', { method: 'POST', body: JSON.stringify({ query_id: queryId, format }) }),
  dashboard: () => request<DashboardData>('/api/v1/dashboard'),
};

export function apiUrl(path: string): string {
  return `${API_URL}${path}`;
}
