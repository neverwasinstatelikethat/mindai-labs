<script lang="ts">
  import { onMount } from 'svelte';
  import {
    AlertTriangle, Check, CheckCircle2, ChevronDown,
    Download, FileText, GitMerge, Info, LoaderCircle, Lock,
    Search, ShieldCheck, Target, TrendingUp, Upload, X, Zap,
    BarChart3, FlaskConical, ListChecks, Network,
  } from '@lucide/svelte';
  import AppHeader from '$lib/AppHeader.svelte';
  import GraphCanvas from '$lib/GraphCanvas.svelte';
  import ChatPanel from '$lib/ChatPanel.svelte';
  import { api } from '$lib/api';
  import { getRole } from '$lib/roleStore.svelte';
  import {
    hasPermission,
    type AgentMetric, type ClaimHistory, type CorpusStats, type EntityMergeProposal,
    type EvaluationRun, type EvolutionExperiment, type EvolutionProposal,
    type GoldCase, type GraphNode, type GraphSnapshot, type NumericObservation, type PipelineBenchmark,
    type RetrievalBenchmark, type SystemStatus,
  } from '$lib/types';

  type Tab = 'answer' | 'map' | 'review' | 'import' | 'evaluation';

  const currentRole = $derived(getRole());
  const canExport = $derived(hasPermission(currentRole, 'export:run'));
  const canReview = $derived(hasPermission(currentRole, 'proposal:review'));
  const canSeeEvaluation = $derived(hasPermission(currentRole, 'evaluation:view'));

  const tabs = $derived(
    [
      { id: 'answer' as Tab, label: 'Запросы', hint: 'чат с агентами' },
      { id: 'map' as Tab, label: 'Карта связей', hint: 'как факты связаны' },
      ...(canReview ? [{ id: 'review' as Tab, label: 'Проверка', hint: 'решения эксперта' }] : []),
      { id: 'import' as Tab, label: 'Импорт', hint: 'наполнение базы' },
      ...(canSeeEvaluation ? [{ id: 'evaluation' as Tab, label: 'Оценка', hint: 'бенчмарки' }] : []),
    ]
  );

  // ── State ──────────────────────────────────────────────────────
  let tab = $state<Tab>('answer');
  let graph: GraphSnapshot = $state({ nodes: [], edges: [], communities: [] });
  let proposals: EvolutionProposal[] = $state([]);
  let mergeProposals: EntityMergeProposal[] = $state([]);
  let experiments: EvolutionExperiment[] = $state([]);
  let evaluations: EvaluationRun[] = $state([]);
  let goldCases: GoldCase[] = $state([]);
  let corpusStats: CorpusStats | null = $state(null);
  let corpusLoading = $state(true);
  let corpusError = $state('');
  let selectedNode: GraphNode | null = $state(null);
  let status: SystemStatus | null = $state(null);
  let error = $state('');
  let uploading = $state(false);
  let ingestMessage = $state('');
  let exporting = $state(false);
  let benchLoading = $state(false);
  let retrievalResult: RetrievalBenchmark | null = $state(null);
  let pipelineResult: PipelineBenchmark | null = $state(null);
  let claimHistoryData: ClaimHistory | null = $state(null);
  let claimHistoryLoading = $state(false);
  let claimHistoryFor: string | null = $state(null);

  const activeTab = $derived(tabs.find((item) => item.id === tab) ?? tabs[0]);

  // ── Helpers ──────────────────────────────────────────────────────
  const intentLabels: Record<string, string> = {
    fact_search: 'Поиск фактов', literature_review: 'Обзор литературы',
    technology_comparison: 'Сравнение технологий', contradiction_analysis: 'Анализ противоречий',
    gap_analysis: 'Анализ пробелов', expert_discovery: 'Поиск экспертов',
    graph_edit: 'Редактирование графа', report_generation: 'Генерация отчёта',
  };

  function formatObservation(obs: NumericObservation): string {
    const name = obs.property_name;
    const unit = obs.unit;
    if (obs.operator === 'between' && obs.min_value != null && obs.max_value != null) {
      return `${name}: ${obs.min_value}–${obs.max_value} ${unit}`;
    }
    const opMap: Record<string, string> = { eq: '=', lt: '<', lte: '≤', gt: '>', gte: '≥' };
    const op = opMap[obs.operator] ?? obs.operator;
    return `${name} ${op} ${obs.value ?? ''} ${unit}`;
  }

  function formatTime(ms: number): string {
    if (ms < 1000) return `${Math.round(ms)} мс`;
    return `${(ms / 1000).toFixed(1)} с`;
  }

  function pct(v: number): string { return `${Math.round(v * 100)}%`; }

  // ── Lifecycle ────────────────────────────────────────────────────
  onMount(async () => {
    const [statusResult, graphResult, proposalResult, experimentResult, corpusResult, evalsResult, goldResult] =
      await Promise.allSettled([
        api.status(), api.graph(), api.proposals(),
        api.experiments(), api.corpusStats(), api.evaluations(), api.goldCases(),
      ]);
    if (statusResult.status === 'fulfilled') status = statusResult.value;
    if (graphResult.status === 'fulfilled') graph = graphResult.value;
    if (proposalResult.status === 'fulfilled') proposals = proposalResult.value;
    if (experimentResult.status === 'fulfilled') experiments = experimentResult.value;
    if (corpusResult.status === 'fulfilled') {
      corpusStats = corpusResult.value;
    } else {
      corpusError = 'Не удалось загрузить статистику корпуса';
    }
    corpusLoading = false;
    if (evalsResult.status === 'fulfilled') evaluations = evalsResult.value;
    if (goldResult.status === 'fulfilled') goldCases = goldResult.value;
    if (canReview) {
      const mergeResult = await Promise.allSettled([api.mergeProposals()]);
      if (mergeResult[0].status === 'fulfilled') mergeProposals = mergeResult[0].value;
    }
  });

  // ── Actions ──────────────────────────────────────────────────────
  async function reviewProposal(proposal: EvolutionProposal, accepted: boolean) {
    try {
      const updated = await api.review(proposal.id, accepted);
      proposals = proposals.map((item) => item.id === updated.id ? updated : item);
    } catch (reason) {
      error = reason instanceof Error ? reason.message : 'Не удалось проверить предложение';
    }
  }

  async function runExperiment(proposal: EvolutionProposal) {
    try {
      const experiment = await api.runExperiment(proposal.id);
      experiments = [experiment, ...experiments.filter((item) => item.proposal_id !== proposal.id)];
    } catch (reason) {
      error = reason instanceof Error ? reason.message : 'Не удалось запустить эксперимент';
    }
  }

  async function reviewMerge(proposal: EntityMergeProposal, action: 'accept' | 'reject') {
    try {
      const updated = await api.reviewMerge(proposal.id, action);
      mergeProposals = mergeProposals.map((item) => item.id === updated.id ? updated : item);
    } catch (reason) {
      error = reason instanceof Error ? reason.message : 'Не удалось проверить слияние';
    }
  }

  async function loadClaimHistory(findingId: string) {
    claimHistoryLoading = true; claimHistoryFor = findingId;
    try { claimHistoryData = await api.claimHistory(findingId); }
    catch { claimHistoryData = null; }
    finally { claimHistoryLoading = false; }
  }

  async function uploadDocument(event: Event) {
    const inputEl = event.currentTarget as HTMLInputElement;
    const file = inputEl.files?.[0];
    if (!file) return;
    uploading = true; error = '';
    try {
      const receipt = await api.upload(file);
      ingestMessage = receipt.status === 'duplicate'
        ? 'Документ уже есть в базе: дубликат не создан.'
        : `Документ обработан. Извлечено утверждений: ${receipt.extracted_claims}.`;
      graph = await api.graph();
      corpusStats = await api.corpusStats();
      corpusError = '';
    } catch (reason) {
      error = reason instanceof Error ? reason.message : 'Не удалось импортировать документ';
    } finally { uploading = false; inputEl.value = ''; }
  }

  async function runRetrievalBenchmark() {
    benchLoading = true;
    try { retrievalResult = await api.retrievalBenchmark(); }
    catch (reason) { error = reason instanceof Error ? reason.message : 'Бенчмарк не удался'; }
    finally { benchLoading = false; }
  }

  async function runPipelineBenchmark() {
    benchLoading = true;
    try { pipelineResult = await api.pipelineBenchmark(); }
    catch (reason) { error = reason instanceof Error ? reason.message : 'Бенчмарк не удался'; }
    finally { benchLoading = false; }
  }

  function getErrorHint(msg: string): string {
    if (msg.includes('500') || msg.includes('Internal Server Error')) return 'Внутренняя ошибка сервера. Попробуйте упростить запрос или повторить позже.';
    if (msg.includes('403') || msg.includes('Forbidden')) return 'Недостаточно прав для этого действия. Проверьте роль пользователя.';
    if (msg.includes('404') || msg.includes('Not Found')) return 'Запрошенный ресурс не найден.';
    if (msg.includes('422') || msg.includes('Validation')) return 'Ошибка валидации запроса. Проверьте формулировку вопроса.';
    if (msg.includes('timeout') || msg.includes('Timeout')) return 'Превышено время ожидания. Сервер занят, попробуйте ещё раз.';
    if (msg.includes('Failed to fetch') || msg.includes('NetworkError')) return 'Не удалось подключиться к серверу. Проверьте, что бэкенд запущен.';
    return msg;
  }
</script>

<svelte:head><title>Исследование — Научный Клубок</title></svelte:head>

<AppHeader compact />
<main class="workspace" id="main">
  {#if error}
    <div class="error-card">
      <div class="error-card__icon"><AlertTriangle size={24} /></div>
      <div class="error-card__body">
        <strong>Ошибка</strong>
        <p>{getErrorHint(error)}</p>
      </div>
      <button class="button button--quiet button--small" onclick={() => error = ''}>
        <X size={16} /> Закрыть
      </button>
    </div>
  {/if}

  <!-- ── Tab Navigation ────────────────────────────────────────────── -->
  <nav class="workspace-tabs" aria-label="Разделы исследования" style={`grid-template-columns:repeat(${tabs.length},1fr)`}>
    {#each tabs as item}
      <button class:active={tab === item.id} onclick={() => tab = item.id} aria-current={tab === item.id ? 'page' : undefined}>
        <strong>{item.label}</strong><span>{item.hint}</span>
        {#if item.id === 'review' && proposals.filter((p) => p.status === 'proposed').length}
          <em>{proposals.filter((p) => p.status === 'proposed').length}</em>
        {/if}
      </button>
    {/each}
  </nav>

  <header class="mobile-tab-title"><strong>{activeTab.label}</strong><span>{activeTab.hint}</span></header>

  <!-- ════════════════ TAB: ЗАПРОСЫ (CHAT) — always mounted, hidden when inactive to preserve state ════════════════ -->
  <section class="workspace-panel" style="padding:24px" class:hidden={tab !== 'answer'}>
    <ChatPanel />
  </section>

  <!-- ════════════════ TAB: MAP ════════════════ -->
  {#if tab === 'map'}
    <section class="workspace-panel map-layout">
      <div class="map-explainer">
        <p class="section-label">Карта связей</p>
        <h2>Граф знаний</h2>
        <p>Сообщества показаны свернутыми кластерами. Кликните для разворачивания. Двойной клик по узлу — фокусный режим. Используйте поиск, чтобы найти нужный элемент.</p>
      </div>
      <GraphCanvas
        {graph}
        selectedId={selectedNode?.id}
        onselect={(node) => selectedNode = node}
      />
    </section>

  <!-- ════════════════ TAB: REVIEW ════════════════ -->
  {:else if tab === 'review'}
    <section class="workspace-panel">
      {#if !canReview}
        <div class="permission-denied">
          <Lock size={32} />
          <h2>Нет доступа</h2>
          <p>Для просмотра и проверки предложений требуется роль «Руководитель проекта» или выше.</p>
        </div>
      {:else}
        <div class="panel-intro">
          <div><p class="section-label">Экспертная проверка</p><h2>Модель предлагает — эксперт решает</h2></div>
          <p>Принятие или отклонение не стирает историю: решение сохраняется в audit log.</p>
        </div>

        <div class="review-section">
          <h3><FileText size={18} /> Предложения эволюции ({proposals.length})</h3>
          {#if proposals.length}
            <div class="review-list">
              {#each proposals as proposal}
                {@const experiment = experiments.find((item) => item.proposal_id === proposal.id)}
                <article>
                  <div>
                    <span class={`status status--${proposal.status}`}>
                      {proposal.status === 'proposed' ? 'ожидает решения' : proposal.status === 'accepted' ? 'принято' : 'отклонено'}
                    </span>
                    <small>{proposal.kind}</small>
                  </div>
                  <div>
                    <h4>{proposal.title}</h4>
                    <p>{proposal.change}</p>
                    <span class="impact">Затрагивает: {proposal.impact.join(' · ')}</span>
                    {#if experiment}
                      <p class="experiment-result">
                        A/B: baseline {pct(experiment.baseline.pass_rate)} → candidate {pct(experiment.candidate.pass_rate)} · {experiment.decision}
                      </p>
                    {/if}
                  </div>
                  {#if proposal.status === 'proposed'}
                    <div class="review-actions">
                      {#if !experiment}
                        <button class="button button--primary button--small" onclick={() => runExperiment(proposal)}>Запустить A/B</button>
                      {:else if experiment.decision === 'promote'}
                        <button class="button button--primary button--small" onclick={() => reviewProposal(proposal, true)}><Check size={16} /> Принять</button>
                      {/if}
                      <button class="button button--quiet button--small" onclick={() => reviewProposal(proposal, false)}><X size={16} /> Отклонить</button>
                    </div>
                  {/if}
                </article>
              {/each}
            </div>
          {:else}
            <div class="empty-state small"><ShieldCheck size={28} /><p>Очередь эволюции пуста.</p></div>
          {/if}
        </div>

        <div class="review-section">
          <h3><GitMerge size={18} /> Предложения слияния сущностей ({mergeProposals.length})</h3>
          {#if mergeProposals.length}
            <div class="review-list">
              {#each mergeProposals as mp}
                <article>
                  <div>
                    <span class={`status status--${mp.status}`}>{mp.status === 'proposed' ? 'ожидает' : mp.status === 'accepted' ? 'принято' : mp.status === 'rejected' ? 'отклонено' : 'отменено'}</span>
                    <small>confidence {Math.round(mp.confidence * 100)}%</small>
                  </div>
                  <div>
                    <div class="merge-preview">
                      <span class="merge-node">{mp.source}</span>
                      <GitMerge size={18} />
                      <span class="merge-node merge-node--target">{mp.target}</span>
                    </div>
                    <p>{mp.rationale}</p>
                  </div>
                  {#if mp.status === 'proposed'}
                    <div class="review-actions">
                      <button class="button button--primary button--small" onclick={() => reviewMerge(mp, 'accept')}><Check size={16} /> Принять</button>
                      <button class="button button--quiet button--small" onclick={() => reviewMerge(mp, 'reject')}><X size={16} /> Отклонить</button>
                    </div>
                  {/if}
                </article>
              {/each}
            </div>
          {:else}
            <div class="empty-state small"><GitMerge size={28} /><p>Нет предложений слияния.</p></div>
          {/if}
        </div>
      {/if}
    </section>

  <!-- ════════════════ TAB: IMPORT ════════════════ -->
  {:else if tab === 'import'}
    <section class="workspace-panel import-layout">
      <div class="panel-intro">
        <div><p class="section-label">Наполнение базы знаний</p><h2>Добавьте документ — остальное видно по шагам</h2></div>
        <p>Исходный файл остаётся неизменным. Каждый извлечённый факт сохраняет ссылку на страницу, лист или ячейку.</p>
      </div>
      {#if corpusLoading}
        <div class="corpus-stats corpus-stats--loading">
          <LoaderCircle class="spin" size={20} />
          <span>Загрузка статистики корпуса…</span>
        </div>
      {:else if corpusError}
        <div class="corpus-stats corpus-stats--error">
          <AlertTriangle size={18} />
          <span>{corpusError}</span>
        </div>
      {:else if corpusStats}
        <div class="corpus-stats">
          <h4><BarChart3 size={16} /> Статистика корпуса</h4>
          <div class="corpus-stats__grid">
            <div class="corpus-stat"><strong>{corpusStats.documents}</strong><span>Документов</span></div>
            <div class="corpus-stat"><strong>{corpusStats.chunks}</strong><span>Фрагментов</span></div>
            <div class="corpus-stat"><strong>{corpusStats.claims}</strong><span>Утверждений</span></div>
            <div class="corpus-stat"><strong>{corpusStats.entities}</strong><span>Сущностей</span></div>
            <div class="corpus-stat"><strong>{corpusStats.semantic_documents}</strong><span>Семант. док.</span></div>
          </div>
        </div>
      {/if}

      <label class="upload-zone">
        <input type="file" accept=".pdf,.docx,.xlsx,.json,.txt" disabled={uploading} onchange={uploadDocument} />
        <Upload size={28} />
        <strong>{uploading ? 'Документ обрабатывается…' : 'Выберите документ'}</strong>
        <span>PDF, DOCX, XLSX, JSON или TXT</span>
      </label>
      {#if ingestMessage}
        <div class="notice notice--success"><CheckCircle2 size={20} /><div><strong>Импорт завершён</strong><p>{ingestMessage}</p></div></div>
      {/if}

      <ol class="pipeline">
        <li><span>1</span><div><strong>Регистрация</strong><p>Checksum защищает от дублей.</p></div></li>
        <li><span>2</span><div><strong>Разбор</strong><p>Страницы, листы и ячейки становятся адресуемыми фрагментами.</p></div></li>
        <li><span>3</span><div><strong>Извлечение</strong><p>LLM предлагает сущности, утверждения, числа и условия.</p></div></li>
        <li><span>4</span><div><strong>Проверка</strong><p>Контракты и единицы валидируются до записи.</p></div></li>
        <li><span>5</span><div><strong>Индексация</strong><p>Знания становятся доступны поиску и карте связей.</p></div></li>
      </ol>
    </section>

  <!-- ════════════════ TAB: EVALUATION ════════════════ -->
  {:else if tab === 'evaluation'}
    <section class="workspace-panel evaluation-layout">
      {#if !canSeeEvaluation}
        <div class="permission-denied">
          <Lock size={32} />
          <h2>Нет доступа</h2>
          <p>Для просмотра оценок требуется роль «Аналитик» или выше.</p>
        </div>
      {:else}
        <div class="panel-intro">
          <div><p class="section-label">Оценка качества</p><h2>Бенчмарки: наш подход vs обычный поиск</h2></div>
          <p>Сравнение гибридного Agentic GraphRAG с лексическим baseline по метрикам ранжирования и качества пайплайна.</p>
        </div>

        <div class="bench-actions">
          <button class="button button--primary button--small" onclick={runRetrievalBenchmark} disabled={benchLoading}>
            {#if benchLoading}<LoaderCircle class="spin" size={16} /> Запуск…{:else}<Zap size={16} />{/if}
            Бенчмарк поиска
          </button>
          <button class="button button--primary button--small" onclick={runPipelineBenchmark} disabled={benchLoading}>
            {#if benchLoading}<LoaderCircle class="spin" size={16} /> Запуск…{:else}<FlaskConical size={16} />{/if}
            Бенчмарк пайплайна
          </button>
        </div>

        {#if retrievalResult}
          <div class="bench-result">
            <h3><Search size={18} /> Ранжирование поиска</h3>
            <table class="bench-table">
              <thead>
                <tr><th>Метрика</th><th>Гибридный (наш)</th><th>Лексический baseline</th><th>Преимущество</th></tr>
              </thead>
              <tbody>
                <tr><td>Recall@3</td><td>{pct(retrievalResult.hybrid.recall_at_3)}</td><td>{pct(retrievalResult.lexical_baseline.recall_at_3)}</td><td class="bench-delta">{pct(retrievalResult.hybrid.recall_at_3 - retrievalResult.lexical_baseline.recall_at_3)}</td></tr>
                <tr><td>Precision@3</td><td>{pct(retrievalResult.hybrid.precision_at_3)}</td><td>{pct(retrievalResult.lexical_baseline.precision_at_3)}</td><td class="bench-delta">{pct(retrievalResult.hybrid.precision_at_3 - retrievalResult.lexical_baseline.precision_at_3)}</td></tr>
                <tr><td>MRR</td><td>{retrievalResult.hybrid.mrr.toFixed(3)}</td><td>{retrievalResult.lexical_baseline.mrr.toFixed(3)}</td><td class="bench-delta">{(retrievalResult.hybrid.mrr - retrievalResult.lexical_baseline.mrr).toFixed(3)}</td></tr>
                <tr><td>NDCG@3</td><td>{retrievalResult.hybrid.ndcg_at_3.toFixed(3)}</td><td>{retrievalResult.lexical_baseline.ndcg_at_3.toFixed(3)}</td><td class="bench-delta">{(retrievalResult.hybrid.ndcg_at_3 - retrievalResult.lexical_baseline.ndcg_at_3).toFixed(3)}</td></tr>
              </tbody>
            </table>
            <div class="bench-meta">
              <span>Gold cases: {retrievalResult.gold_cases}</span>
              <span>Corpus: {retrievalResult.corpus_documents}</span>
              <span>Top-K: {retrievalResult.top_k}</span>
              <span class={`status status--${retrievalResult.passed ? 'consensus' : 'disputed'}`}>{retrievalResult.passed ? 'пройден' : 'не пройден'}</span>
            </div>
          </div>
        {/if}

        {#if pipelineResult}
          <div class="bench-result">
            <h3><FlaskConical size={18} /> Бенчмарк пайплайна</h3>
            <table class="bench-table">
              <thead>
                <tr><th>Метрика</th><th>Agentic GraphRAG</th><th>Лексический baseline</th></tr>
              </thead>
              <tbody>
                <tr><td>Source recall</td><td>{pct(pipelineResult.agentic_graphrag.source_recall)}</td><td>—</td></tr>
                <tr><td>Citation coverage</td><td>{pct(pipelineResult.agentic_graphrag.citation_coverage)}</td><td>—</td></tr>
                <tr><td>Pass rate</td><td>{pct(pipelineResult.agentic_graphrag.pass_rate)}</td><td>—</td></tr>
                <tr><td>Avg latency</td><td>{formatTime(pipelineResult.agentic_graphrag.average_latency_ms)}</td><td>—</td></tr>
              </tbody>
            </table>
            <div class="bench-meta">
              <span>Cases: {pipelineResult.cases}</span>
              <span class={`status status--${pipelineResult.passed ? 'consensus' : 'disputed'}`}>{pipelineResult.passed ? 'пройден' : 'не пройден'}</span>
            </div>
          </div>
        {/if}

        <div class="eval-history">
          <h3><ListChecks size={18} /> История оценок ({evaluations.length})</h3>
          {#if evaluations.length}
            <table class="bench-table">
              <thead>
                <tr><th>ID</th><th>Покрытие</th><th>Числа</th><th>Точность</th><th>Общая</th><th>Статус</th></tr>
              </thead>
              <tbody>
                {#each evaluations.slice(0, 10) as ev}
                  <tr>
                    <td class="mono">{ev.id.slice(0, 8)}</td>
                    <td>{pct(ev.metrics.citation_coverage)}</td>
                    <td>{pct(ev.metrics.numeric_support)}</td>
                    <td>{pct(ev.metrics.evidence_precision)}</td>
                    <td><strong>{pct(ev.metrics.overall)}</strong></td>
                    <td><span class={`status status--${ev.passed ? 'consensus' : 'disputed'}`}>{ev.passed ? '✓' : '✗'}</span></td>
                  </tr>
                {/each}
              </tbody>
            </table>
          {:else}
            <div class="empty-state small"><ListChecks size={28} /><p>Оценок пока нет. Запустите бенчмарк.</p></div>
          {/if}
        </div>

        {#if goldCases.length}
          <div class="gold-cases">
            <h3><CheckCircle2 size={18} /> Gold test cases ({goldCases.length})</h3>
            <div class="gold-cases__list">
              {#each goldCases as gc}
                <div class="gold-case">
                  <span class="gold-case__lang">{gc.language}</span>
                  <span class="gold-case__q">{gc.question}</span>
                  <small>{gc.expected_source_titles.length} ожид. источ.</small>
                </div>
              {/each}
            </div>
          </div>
        {/if}
      {/if}
    </section>
  {/if}
</main>
