export type Capability =
  | 'knowledge:read'
  | 'query:ask'
  | 'feedback:give'
  | 'export:run'
  | 'evaluation:view'
  | 'proposal:review'
  | 'restricted:read'
  | 'audit:read';

// ── Evidence & Findings ──────────────────────────────────────────

export interface Evidence {
  document_id: string;
  source_title: string;
  page?: number | null;
  sheet?: string | null;
  cell_range?: string | null;
  char_start?: number | null;
  char_end?: number | null;
  quote: string;
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
  // Класс доступа приходит из контракта и не выводится из статуса: consensus /
  // disputed / hypothesis — про степень консенсуса источников, а не про права.
  data_class: DataClass;
  // След экспертной замены (contracts.Finding): по нему интерфейс показывает,
  // что версию правили, а не то, что она сама по себе свежая.
  reviewer_id?: string | null;
  review_date?: string | null;
  review_reason?: string | null;
}

// Зеркало contracts.DataClass: общий класс доступа для узлов графа, рёбер и
// утверждений.
export type DataClass = 'public' | 'internal' | 'restricted';

// ── Findings API (v1) ─────────────────────────────────────────────
// /api/v1/findings и /api/v1/conflicts отдают тот же Finding, что и ответ
// запроса: Finding.model_dump(). Заменённые версии отфильтровываются на сервере,
// поэтому «superseded» и «extracted» в ответе не существуют.

export type FindingApiStatus = 'consensus' | 'disputed' | 'hypothesis';

export type FindingListItem = Finding;

// ── Graph ────────────────────────────────────────────────────────

export interface GraphNode {
  id: string;
  label: string;
  type: string;
  confidence: number;
  data_class: DataClass;
  metadata: Record<string, string | number | boolean>;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  relation: string;
  confidence: number;
  data_class: DataClass;
}

export interface GraphSnapshot {
  nodes: GraphNode[];
  edges: GraphEdge[];
  communities: string[];
}

// ── Agents & Metrics ──────────────────────────────────────────────

export interface AgentEvent {
  agent: string;
  status: 'started' | 'completed' | 'revised' | 'failed';
  message: string;
  duration_ms: number;
}

export interface AgentMetric {
  agent: string;
  calls: number;
  successes: number;
  failures: number;
  // Повторы отдельны от исходов: success_rate считает исход последней попытки,
  // а число retry-ов показывает, какой ценой он достался.
  retries: number;
  success_rate: number;
  average_duration_ms: number;
  p50_duration_ms: number;
  p95_duration_ms: number;
}

// Schema LLM-ответа: в Python-контракте поле называется schema_name
// (schema конфликтует с зарезервированным именем Pydantic).
export interface LlmMetricSnapshot {
  schema_name: string;
  calls: number;
  failures: number;
  // Schema-repair попытки: модель дала ответ, не прошедший валидацию с первого раза.
  retries: number;
  average_duration_ms: number;
  p95_duration_ms: number;
}

export interface AgentMetricsResponse {
  generated_at: string;
  agents: AgentMetric[];
  llm: LlmMetricSnapshot[];
  total_prompt_tokens: number;
  total_completion_tokens: number;
  // Приём агентных прогонов и очередь модельных слотов: без этих чисел 429
  // выглядят как случайные отказы сервиса, а «сервис занят» нельзя показать.
  agent_runs_active: number;
  agent_runs_limit: number;
  agent_runs_refused: number;
  llm_calls_in_flight: number;
  llm_calls_waiting: number;
  llm_slots: number;
}

// ── Intent & Query Plan ───────────────────────────────────────────

export interface IntentClassification {
  primary:
    | 'fact_search' | 'literature_review' | 'technology_comparison'
    | 'contradiction_analysis' | 'gap_analysis' | 'expert_discovery'
    | 'graph_edit' | 'report_generation';
  secondary: string[];
  entities: string[];
  constraints: string[];
}

export interface NumericFilter {
  property_name: string;
  operator: 'eq' | 'lt' | 'lte' | 'gt' | 'gte' | 'between' | 'range';
  value?: number;
  min_value?: number;
  max_value?: number;
  unit: string;
}

export interface QueryPlan {
  question: string;
  language: 'ru' | 'en';
  mode: 'local' | 'global' | 'hybrid';
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
  // Доля выводов без поддержки вместо бывшей «точности доказательств»:
  // само-оценка модели и реальная трассировка — разные метрики.
  unsupported_claim_ratio: number;
  mean_finding_confidence: number;
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

// Зеркало ModelMode / ServiceState из domain/contracts.py: YandexGPT из бэкенда
// удалён, состояния 'fallback' больше нет — неполнота ответа приходит отдельным
// каналом degradation_reasons.
export type ModelMode = 'gigachat' | 'scripted' | 'unavailable';

export type ServiceState = 'ready' | 'configured' | 'fallback' | 'disabled';

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
  model_mode: ModelMode;
  // Явный канал деградации: почему ответ собран не полностью.
  degradation_reasons: string[];
}

export interface QueryResponse {
  answer: AnswerPayload;
  evaluation: EvaluationRun;
  // Тот же correlation_id, что в заголовке X-Correlation-Id и в журнале аудита:
  // по нему разбор инцидента связывает ответ с записью.
  correlation_id: string;
}

// ── Evolution ─────────────────────────────────────────────────────

export interface EvolutionProposal {
  id: string;
  source_query_id: string;
  // Тот же перечень, что в contracts.EvolutionProposal.kind: A/B-прогон умеет
  // только prompt и rule, и на клиенте это должно быть видно из типа.
  kind: 'prompt' | 'rule' | 'alias' | 'gold_case';
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
  p95_latency_ms: number;
  retries_per_case: number;
  total_tokens: number;
}

export interface EvolutionExperiment {
  id: string;
  proposal_id: string;
  cases: number;
  baseline: PipelineVariantMetrics;
  candidate: PipelineVariantMetrics;
  delta_pass_rate: number;
  // Зеркало contracts.EvolutionExperiment.regressions: id кейсов, которые
  // кандидат ухудшил относительно базовой версии конвейера.
  regressions: string[];
  decision: 'promote' | 'reject';
  created_at?: string;
}

// ── System Status & Documents ─────────────────────────────────────

export interface SystemStatus {
  status: 'ready' | 'degraded';
  model_mode: ModelMode;
  // Контуры аккаунтов: 'in-memory' означает, что регистрация и вход живут
  // только до перезапуска процесса — показываем это честно.
  accounts?: 'postgres' | 'in-memory';
  // Тот же выбор для серверного состояния (копии ответов, журнал аудита,
  // экспертные решения, прогоны оценки и A/B, лента уведомлений): на памяти
  // история обнуляется перезапуском, и интерфейс не вправе обещать обратное.
  state_backend?: 'postgres' | 'in-memory';
  // Причина деградации словами — вместо обещаний «история решений сохранена».
  degradation_reasons?: string[];
  // Пропускная способность агентного контура: 429 обязаны быть объяснимы.
  agent_runs_limit?: number;
  agent_runs_active?: number;
  services: Record<string, ServiceState>;
}

export interface DocumentReceipt {
  document_id: string;
  checksum: string;
  status: 'created' | 'duplicate';
  extracted_claims: number;
  // Промпт извлечения ограничен бюджетом: недосланный хвост документа обязан
  // быть виден, иначе приём числа из него выглядит как «в корпусе такого нет».
  prompt_truncated: boolean;
  omitted_characters: number;
}

export interface CorpusStats {
  documents: number;
  chunks: number;
  claims: number;
  entities: number;
  semantic_documents: number;
  vectors_indexed: number;
}

// ── Evaluation Harness Types ──────────────────────────────────────

export interface GoldCase {
  id: string;
  language: string;
  question: string;
  source_path: string;
  expected_source_titles: string[];
}

// ── FT-20/21: RBAC / ACL ──────────────────────────────────────────

// Аккаунт и сессия: единственный источник прав — /api/v1/auth/me.
export interface AccountInfo {
  id: string;
  email: string;
  display_name: string;
  review_enabled: boolean;
  created_at: string;
  capabilities: Capability[];
  data_classes: DataClass[];
}

// ── FT-08: Claim Versioning ───────────────────────────────────────

export interface ClaimHistoryEntry {
  finding_id: string;
  version: number;
  statement: string;
  status: FindingApiStatus;
  superseded_by: string | null;
  reviewer_id: string | null;
  review_date: string | null;
  review_reason: string | null;
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

// ── FT-23: Export ─────────────────────────────────────────────────

export type ExportFormat = 'markdown' | 'json-ld' | 'pdf';

// ── Обратная связь и решения эксперта ─────────────────────────────

// contracts.FeedbackResult: предложение генерирует модель, а решение эксперта
// записывается всегда — без живого LLM proposal отсутствует, и это штатный
// сценарий, а не ошибка.
export interface FeedbackResult {
  proposal: EvolutionProposal | null;
  superseded: Finding | null;
  degradation_reasons: string[];
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
  // Пробелы сверх лимита выборки: их количество сервер знает, а списка нет.
  gaps_omitted: number;
  recent_activity: ActivityEntry[];
  agent_metrics: AgentMetricsResponse;
}

// ── Доступ ────────────────────────────────────────────────────────

// Единственный источник истины по доступу — сервер: /api/v1/auth/me отдаёт
// capabilities и data_classes подтверждённой сессии. На клиенте не остаётся
// зеркала ролей: только человекочитаемые имена для получения и объяснения 403.
export const CAPABILITY_LABELS: Record<Capability, string> = {
  'knowledge:read': 'Граф и находки',
  'query:ask': 'Запросы',
  'feedback:give': 'Обратная связь',
  'export:run': 'Экспорт и сравнение',
  'evaluation:view': 'Оценка качества',
  'proposal:review': 'Проверка предложений',
  'restricted:read': 'Закрытые данные',
  'audit:read': 'Аудит',
};

export const DATA_CLASS_LABELS: Record<DataClass, string> = {
  public: 'Открытые',
  internal: 'Внутренние',
  restricted: 'Закрытые',
};
