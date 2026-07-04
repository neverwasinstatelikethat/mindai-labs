export type Mode = 'answer' | 'graph' | 'changes';

export type RoleId = 'researcher' | 'analyst' | 'project_manager' | 'administrator' | 'external_partner';

// ── Evidence & Findings ──────────────────────────────────────────

export interface Evidence {
  document_id: string;
  source_title: string;
  page?: number;
  sheet?: string;
  cell_range?: string;
  quote: string;
  score?: number;
}

export interface NumericObservation {
  property_name: string;
  operator: 'eq' | 'lt' | 'lte' | 'gt' | 'gte' | 'between';
  value?: number;
  min_value?: number;
  max_value?: number;
  unit: string;
  normalized_value?: number;
  normalized_min?: number;
  normalized_max?: number;
  normalized_unit: string;
  raw_text: string;
}

export interface Finding {
  id: string;
  statement: string;
  confidence: number;
  evidence: Evidence[];
  status: 'consensus' | 'disputed' | 'hypothesis';
  observations: NumericObservation[];
  version: number;
  superseded_by?: string | null;
  subject?: string | null;
  predicate?: string | null;
  scope?: Record<string, string>;
}

// ── Findings API (v1) ─────────────────────────────────────────────

export type FindingApiStatus = 'extracted' | 'validated' | 'disputed' | 'superseded';

export interface FindingObservation {
  name: string;
  value: number;
  unit: string;
}

export interface FindingListItem {
  id: string;
  statement: string;
  subject: string | null;
  predicate: string | null;
  confidence: number;
  status: FindingApiStatus;
  evidence: Evidence[];
  observations: FindingObservation[];
}

// ── Graph ────────────────────────────────────────────────────────

export interface GraphNode {
  id: string;
  label: string;
  type: string;
  confidence: number;
  metadata: Record<string, string | number | boolean>;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  relation: string;
  confidence: number;
}

export interface GraphSnapshot {
  nodes: GraphNode[];
  edges: GraphEdge[];
  communities: string[];
}

// ── Agents & Metrics ──────────────────────────────────────────────

export interface AgentEvent {
  agent: string;
  status: string;
  message: string;
  duration_ms: number;
}

export interface AgentMetric {
  agent: string;
  calls: number;
  successes: number;
  failures: number;
  success_rate: number;
  average_duration_ms: number;
  p50_duration_ms: number;
  p95_duration_ms: number;
}

export interface AgentMetricsResponse {
  generated_at: string;
  agents: AgentMetric[];
}

// ── Intent & Query Plan ───────────────────────────────────────────

export interface IntentClassification {
  primary: string;
  secondary: string[];
  entities: string[];
  constraints: string[];
  requires_external_action?: boolean;
  requires_user_confirmation?: boolean;
  confirmation_reason?: string | null;
}

export interface NumericFilter {
  property_name: string;
  operator: string;
  value?: number;
  min_value?: number;
  max_value?: number;
  unit: string;
}

export interface QueryPlan {
  question: string;
  language: string;
  mode: string;
  entity_mentions: string[];
  numeric_filters: NumericFilter[];
  countries: string[];
  year_from?: number;
  year_to?: number;
  max_hops?: number;
}

// ── Tool Observations ─────────────────────────────────────────────

export interface ToolObservation {
  action_id: string;
  tool: string;
  status: 'success' | 'warning' | 'error';
  summary: string;
  next_actions?: string[];
  artifacts?: string[];
  finding_ids?: string[];
  graph_node_ids?: string[];
  facts?: string[];
  root_cause_hint?: string | null;
  safe_retry?: string | null;
  stop_condition?: string | null;
}

// ── Evaluation ────────────────────────────────────────────────────

export interface EvaluationMetrics {
  citation_coverage: number;
  numeric_support: number;
  evidence_precision: number;
  overall: number;
}

export interface EvaluationRun {
  id: string;
  query_id: string;
  metrics: EvaluationMetrics;
  passed: boolean;
  created_at: string;
}

// ── Query Response ────────────────────────────────────────────────

export interface AnswerPayload {
  query_id: string;
  question: string;
  summary: string;
  intent?: IntentClassification | null;
  query_plan: QueryPlan;
  tool_observations: ToolObservation[];
  findings: Finding[];
  conflicts: string[];
  knowledge_gaps: string[];
  recommendations: string[];
  graph: GraphSnapshot;
  trace: AgentEvent[];
  confidence: number;
  model_mode: string;
}

export interface QueryResponse {
  answer: AnswerPayload;
  evaluation: EvaluationRun;
}

// ── Evolution ─────────────────────────────────────────────────────

export interface EvolutionProposal {
  id: string;
  source_query_id: string;
  kind: string;
  title: string;
  change: string;
  status: 'proposed' | 'accepted' | 'rejected';
  impact: string[];
  created_at?: string;
}

export interface PipelineVariantMetrics {
  source_recall: number;
  citation_coverage: number;
  pass_rate: number;
  average_latency_ms: number;
}

export interface EvolutionExperiment {
  id: string;
  proposal_id: string;
  cases: number;
  baseline: PipelineVariantMetrics;
  candidate: PipelineVariantMetrics;
  delta_pass_rate: number;
  decision: 'promote' | 'reject';
  created_at?: string;
}

// ── Entity Resolution ─────────────────────────────────────────────

export interface EntityMergeProposal {
  id: string;
  source: string;
  target: string;
  confidence: number;
  rationale: string;
  status: 'proposed' | 'accepted' | 'rejected' | 'reverted';
  created_at?: string;
}

// ── System Status & Documents ─────────────────────────────────────

export interface SystemStatus {
  status: 'ready' | 'degraded';
  model_mode: 'unavailable' | 'yandex' | 'scripted';
  services: Record<string, string>;
}

export interface DocumentReceipt {
  document_id: string;
  checksum: string;
  status: 'created' | 'duplicate';
  extracted_claims: number;
}

export interface CorpusStats {
  documents: number;
  chunks: number;
  claims: number;
  entities: number;
  semantic_documents: number;
}

// ── Evaluation Harness Types ──────────────────────────────────────

export interface GoldCase {
  id: string;
  language: string;
  question: string;
  source_path: string;
  expected_source_titles: string[];
}

export interface RankingMetrics {
  recall_at_3: number;
  precision_at_3: number;
  mrr: number;
  ndcg_at_3: number;
}

export interface RetrievalCaseResult {
  case_id: string;
  expected_sources: string[];
  retrieved_sources: string[];
  reciprocal_rank: number;
}

export interface RetrievalBenchmark {
  gold_cases: number;
  corpus_documents: number;
  top_k: number;
  hybrid: RankingMetrics;
  lexical_baseline: RankingMetrics;
  cases: RetrievalCaseResult[];
  leakage_checks: Record<string, boolean>;
  passed: boolean;
}

export interface PipelineCaseResult {
  case_id: string;
  question: string;
  expected_sources: string[];
  retrieved_sources: string[];
  latency_ms: number;
  passed: boolean;
}

export interface PipelineBenchmark {
  cases: number;
  agentic_graphrag: PipelineVariantMetrics;
  lexical_baseline: RankingMetrics;
  results: PipelineCaseResult[];
  passed: boolean;
}

// ── FT-20/21: RBAC / ACL ──────────────────────────────────────────

export interface RoleInfo {
  role: string;
  permissions: string[];
  data_classes: string[];
}

export interface PrincipalInfo {
  user_id: string;
  role: string;
  permissions: string[];
  allowed_data_classes: string[];
}

export interface AuditEvent {
  actor_id: string;
  action: string;
  object_id: string;
  outcome: string;
  correlation_id?: string;
  created_at: string;
}

// ── FT-08: Claim Versioning ───────────────────────────────────────

export interface ClaimHistoryEntry {
  finding_id: string;
  version: number;
  statement: string;
  status: string;
  superseded_by: string | null;
}

export interface ClaimHistory {
  claim_id: string;
  versions: ClaimHistoryEntry[];
}

// ── FT-12/26: Comparison ──────────────────────────────────────────

export interface ComparisonCell {
  value?: string | null;
  unit?: string | null;
  evidence?: string | null;
  confidence: number;
}

export interface ComparisonRow {
  item: string;
  cells: Record<string, ComparisonCell>;
}

export interface ComparisonTable {
  question: string;
  headers: string[];
  rows: ComparisonRow[];
}

export interface ComparisonRequest {
  question: string;
  entities: string[];
  dimensions: string[];
  language: string;
}

// ── FT-23: Export ─────────────────────────────────────────────────

export interface ExportRequest {
  answer: AnswerPayload;
  format: 'markdown' | 'json-ld';
}

// ── FT-25: Dashboard ──────────────────────────────────────────────

export interface ActivityEntry {
  action: string;
  actor_id: string;
  object_id: string;
  outcome: string;
  created_at: string;
}

export interface DashboardData {
  documents: number;
  claims: number;
  entities: number;
  evidence: number;
  conflicts: number;
  gaps: number;
  recent_activity: ActivityEntry[];
  agent_metrics: AgentMetricsResponse;
}

// ── FT-24: Notifications ──────────────────────────────────────────

export interface Notification {
  id: string;
  topic: string;
  message: string;
  created_at: string;
  type?: 'info' | 'warning' | 'success';
}

export interface SubscriptionRequest {
  topic: string;
  subscriber_id: string;
}

// ── Role Helpers ──────────────────────────────────────────────────

export const ROLE_LABELS: Record<string, string> = {
  researcher: 'Исследователь',
  analyst: 'Аналитик',
  project_manager: 'Руководитель проекта',
  administrator: 'Администратор',
  external_partner: 'Внешний партнёр',
};

export const ROLE_DESCRIPTIONS: Record<string, string> = {
  researcher: 'Базовые запросы и обратная связь',
  analyst: 'Дополнительно: экспорт и оценка качества',
  project_manager: 'Дополнительно: проверка предложений и закрытые данные',
  administrator: 'Полный доступ: аудит и управление пользователями',
  external_partner: 'Только публичные данные',
};

export const ROLE_PERMISSIONS: Record<string, string[]> = {
  researcher: ['Запросы', 'Обратная связь', 'Граф знаний'],
  analyst: ['Запросы', 'Обратная связь', 'Граф знаний', 'Экспорт', 'Оценка качества', 'Сравнение'],
  project_manager: ['Запросы', 'Обратная связь', 'Граф знаний', 'Экспорт', 'Оценка качества', 'Сравнение', 'Проверка предложений', 'Закрытые данные'],
  administrator: ['Запросы', 'Обратная связь', 'Граф знаний', 'Экспорт', 'Оценка качества', 'Сравнение', 'Проверка предложений', 'Закрытые данные', 'Аудит', 'Управление пользователями'],
  external_partner: ['Граф знаний (публичные данные)'],
};

export const ROLE_RANK: Record<string, number> = {
  external_partner: 0,
  researcher: 1,
  analyst: 2,
  project_manager: 3,
  administrator: 4,
};

export function hasPermission(role: string, permission: string): boolean {
  const rank = ROLE_RANK[role] ?? 0;
  switch (permission) {
    case 'evaluation:view':
    case 'export:run':
      return rank >= ROLE_RANK['analyst'];
    case 'proposal:review':
    case 'restricted:read':
      return rank >= ROLE_RANK['project_manager'];
    case 'audit:read':
    case 'user:manage':
      return rank >= ROLE_RANK['administrator'];
    case 'knowledge:read':
    case 'query:ask':
    case 'feedback:give':
      return rank >= ROLE_RANK['researcher'];
    default:
      return false;
  }
}
