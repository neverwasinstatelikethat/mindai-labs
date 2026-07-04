import { env } from '$env/dynamic/public';
import { getRoleHeaders } from './roleStore.svelte';
import type {
  AgentMetricsResponse,
  AuditEvent,
  ClaimHistory,
  ComparisonTable,
  CorpusStats,
  DashboardData,
  DocumentReceipt,
  EntityMergeProposal,
  EvaluationRun,
  EvolutionExperiment,
  EvolutionProposal,
  FindingApiStatus,
  FindingListItem,
  GoldCase,
  GraphSnapshot,
  Notification,
  PipelineBenchmark,
  PrincipalInfo,
  QueryResponse,
  RetrievalBenchmark,
  RoleInfo,
  SystemStatus,
} from './types';

const API_URL = env.PUBLIC_API_URL || '/backend';

function buildHeaders(extra?: Record<string, string>): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    ...getRoleHeaders(),
    ...extra,
  };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: buildHeaders(init?.headers as Record<string, string> | undefined),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
}

async function requestRaw(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: buildHeaders(init?.headers as Record<string, string> | undefined),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response;
}

export const api = {
  status: () => request<SystemStatus>('/health/ready'),
  demo: () => request<QueryResponse>('/api/v1/demo'),
  agentMetrics: () => request<AgentMetricsResponse>('/api/v1/agents/metrics'),
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
      headers: getRoleHeaders(),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({ detail: response.statusText }));
      throw new Error(body.detail || `HTTP ${response.status}`);
    }
    return response.json() as Promise<DocumentReceipt>;
  },
  query: (question: string) =>
    request<QueryResponse>('/api/v1/query', {
      method: 'POST',
      body: JSON.stringify({ question, language: 'ru', mode: 'hybrid' }),
    }),
  validateQuery: (plan: unknown) =>
    request<unknown>('/api/v1/queries/validate', {
      method: 'POST',
      body: JSON.stringify(plan),
    }),
  feedback: (queryId: string, findingId: string | null, verdict: string, comment: string) =>
    request<EvolutionProposal>('/api/v1/feedback', {
      method: 'POST',
      body: JSON.stringify({ query_id: queryId, finding_id: findingId, verdict, comment }),
    }),
  proposals: () => request<EvolutionProposal[]>('/api/v1/proposals'),
  experiments: () => request<EvolutionExperiment[]>('/api/v1/experiments'),
  runExperiment: (proposalId: string) =>
    request<EvolutionExperiment>(`/api/v1/proposals/${proposalId}/experiment?max_cases=1`, {
      method: 'POST',
    }),
  review: (proposalId: string, accepted: boolean) =>
    request<EvolutionProposal>(`/api/v1/proposals/${proposalId}/review`, {
      method: 'POST',
      body: JSON.stringify({ accepted }),
    }),
  mergeProposals: () => request<EntityMergeProposal[]>('/api/v1/entity-resolution/proposals'),
  reviewMerge: (proposalId: string, action: 'accept' | 'reject' | 'revert') =>
    request<EntityMergeProposal>(`/api/v1/entity-resolution/proposals/${proposalId}/review`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    }),
  evaluations: () => request<EvaluationRun[]>('/api/v1/evaluations'),
  goldCases: () => request<GoldCase[]>('/api/v1/evaluations/gold'),
  retrievalBenchmark: () =>
    request<RetrievalBenchmark>('/api/v1/evaluations/retrieval-benchmark', {
      method: 'POST',
    }),
  pipelineBenchmark: () =>
    request<PipelineBenchmark>('/api/v1/evaluations/pipeline-benchmark', {
      method: 'POST',
    }),
  roles: () => request<RoleInfo[]>('/api/v1/roles'),
  principal: () => request<PrincipalInfo>('/api/v1/principal'),
  audit: () => request<AuditEvent[]>('/api/v1/audit'),
  claimHistory: (claimId: string) =>
    request<ClaimHistory>(`/api/v1/claims/${claimId}/history`),
  compare: (question: string, entities: string[], dimensions: string[] = []) =>
    request<ComparisonTable>('/api/v1/compare', {
      method: 'POST',
      body: JSON.stringify({ question, entities, dimensions, language: 'ru' }),
    }),
  export: async (answer: unknown, format: 'markdown' | 'json-ld') => {
    const response = await requestRaw('/api/v1/export', {
      method: 'POST',
      body: JSON.stringify({ answer, format }),
    });
    return response;
  },
  dashboard: () => request<DashboardData>('/api/v1/dashboard'),
  notifications: () => request<Notification[]>('/api/v1/notifications'),
  subscribe: (topic: string) =>
    request<{ status: string; topic: string }>('/api/v1/subscriptions', {
      method: 'POST',
      body: JSON.stringify({ topic, subscriber_id: 'web' }),
    }),
};
