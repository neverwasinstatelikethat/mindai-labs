<script lang="ts">
  import { onMount } from 'svelte';
  import {
    AlertTriangle, BarChart3, CheckCircle2, ChevronDown, Download,
    FileText, Lightbulb, LoaderCircle, Lock, Network, Search, X,
  } from '@lucide/svelte';
  import AppHeader from '$lib/AppHeader.svelte';
  import GraphCanvas from '$lib/GraphCanvas.svelte';
  import { api } from '$lib/api';
  import { getRole } from '$lib/roleStore.svelte';
  import {
    hasPermission,
    type FindingApiStatus,
    type FindingListItem,
    type GraphNode,
    type GraphSnapshot,
  } from '$lib/types';

  const currentRole = $derived(getRole());
  const canExport = $derived(hasPermission(currentRole, 'export:run'));
  const canAccess = $derived(hasPermission(currentRole, 'knowledge:read'));

  // ── State ──────────────────────────────────────────────────────
  let findings = $state<FindingListItem[]>([]);
  let graph = $state<GraphSnapshot>({ nodes: [], edges: [], communities: [] });
  let loading = $state(true);
  let error = $state('');
  let searchQuery = $state('');
  let statusFilter = $state<'all' | FindingApiStatus>('all');
  let subjectFilter = $state<string>('');
  let selectedNode = $state<GraphNode | null>(null);
  let exporting = $state(false);
  let exportMsg = $state('');
  let expandedFindings = $state<Set<string>>(new Set());
  let expandedEvidence = $state<Set<string>>(new Set());

  // ── Derived ───────────────────────────────────────────────────
  const uniqueSubjects = $derived.by<string[]>(() => {
    const subjects = new Set<string>();
    for (const f of findings) {
      if (f.subject) subjects.add(f.subject);
    }
    return Array.from(subjects).sort();
  });

  const statusCounts = $derived({
    extracted: findings.filter((f) => f.status === 'extracted').length,
    validated: findings.filter((f) => f.status === 'validated').length,
    disputed: findings.filter((f) => f.status === 'disputed').length,
    superseded: findings.filter((f) => f.status === 'superseded').length,
  });

  const filteredFindings = $derived.by<FindingListItem[]>(() => {
    let result = findings;
    if (statusFilter !== 'all') {
      result = result.filter((f) => f.status === statusFilter);
    }
    if (subjectFilter) {
      result = result.filter((f) => f.subject === subjectFilter);
    }
    if (searchQuery.trim()) {
      const q = searchQuery.trim().toLowerCase();
      result = result.filter((f) =>
        f.statement.toLowerCase().includes(q) ||
        (f.subject ?? '').toLowerCase().includes(q) ||
        (f.predicate ?? '').toLowerCase().includes(q)
      );
    }
    return result;
  });

  // ── Helpers ───────────────────────────────────────────────────
  function pct(v: number): string {
    return `${Math.round(v * 100)}%`;
  }

  function confColor(v: number): string {
    if (v >= 0.7) return '#1f7650';
    if (v >= 0.4) return '#a66513';
    return '#ad3434';
  }

  const statusLabels: Record<FindingApiStatus, string> = {
    extracted: 'извлечено',
    validated: 'проверено',
    disputed: 'оспаривается',
    superseded: 'устарело',
  };

  function statusLabel(s: FindingApiStatus): string {
    return statusLabels[s] ?? s;
  }

  function toggleFinding(id: string): void {
    const next = new Set(expandedFindings);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    expandedFindings = next;
  }

  function toggleEvidence(key: string): void {
    const next = new Set(expandedEvidence);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    expandedEvidence = next;
  }

  async function handleExport(format: 'markdown' | 'json-ld') {
    exporting = true;
    exportMsg = '';
    try {
      const dummyAnswer = {
        query_id: 'findings-export',
        question: 'Экспорт находок',
        summary: 'Экспорт всех находок из базы знаний',
        intent: null,
        query_plan: { question: '', language: 'ru', mode: 'hybrid', entity_mentions: [], numeric_filters: [], countries: [] },
        tool_observations: [],
        findings: findings.map((f) => ({
          id: f.id,
          statement: f.statement,
          confidence: f.confidence,
          evidence: f.evidence,
          status: f.status === 'validated' ? 'consensus' as const : f.status === 'disputed' ? 'disputed' as const : 'hypothesis' as const,
          observations: [],
          version: 1,
          superseded_by: f.status === 'superseded' ? null : null,
          subject: f.subject,
          predicate: f.predicate,
          scope: {},
        })),
        conflicts: [],
        knowledge_gaps: [],
        recommendations: [],
        graph,
        trace: [],
        confidence: 0.8,
        model_mode: 'yandex',
      };
      const response = await api.export(dummyAnswer, format);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `findings-export.${format === 'markdown' ? 'md' : 'jsonld'}`;
      a.click();
      URL.revokeObjectURL(url);
      exportMsg = `Экспорт в формате ${format === 'markdown' ? 'Markdown' : 'JSON-LD'} выполнен`;
    } catch (reason) {
      exportMsg = reason instanceof Error ? reason.message : 'Ошибка экспорта';
    } finally {
      exporting = false;
    }
  }

  // ── Lifecycle ──────────────────────────────────────────────────
  onMount(async () => {
    try {
      const [findingsResult, graphResult] = await Promise.allSettled([
        api.findings(),
        api.graph(),
      ]);
      if (findingsResult.status === 'fulfilled') {
        findings = findingsResult.value;
      } else {
        throw findingsResult.reason;
      }
      if (graphResult.status === 'fulfilled') graph = graphResult.value;
    } catch (reason) {
      error = reason instanceof Error ? reason.message : 'Не удалось загрузить находки';
    } finally {
      loading = false;
    }
  });
</script>

<svelte:head><title>Находки — Научный Клубок</title></svelte:head>

<AppHeader compact />
<main class="workspace" id="main">
  <section class="page-heading">
    <div>
      <p class="section-label">База знаний</p>
      <h1>Находки и утверждения</h1>
    </div>
    {#if canExport && findings.length > 0}
      <div class="findings-export">
        <button class="button button--quiet button--small" onclick={() => handleExport('markdown')} disabled={exporting}>
          <Download size={16} /> Markdown
        </button>
        <button class="button button--quiet button--small" onclick={() => handleExport('json-ld')} disabled={exporting}>
          <Download size={16} /> JSON-LD
        </button>
      </div>
    {/if}
  </section>

  {#if !canAccess}
    <div class="permission-denied workspace-panel">
      <Lock size={36} />
      <h2>Доступ запрещён</h2>
      <p>Для просмотра находок требуется роль «Исследователь» или выше.</p>
    </div>
  {:else if loading}
    <div class="workspace-panel loading-state">
      <LoaderCircle class="spin" size={28} />
      <p>Загрузка находок из базы знаний…</p>
    </div>
  {:else if error}
    <div class="notice notice--error">
      <AlertTriangle size={19} />
      <div><strong>Ошибка загрузки</strong><p>{error}</p></div>
    </div>
  {:else}
    {#if exportMsg}
      <div class="notice notice--success">
        <CheckCircle2 size={19} />
        <div><strong>Экспорт</strong><p>{exportMsg}</p></div>
      </div>
    {/if}

    <!-- Статистика по статусам -->
    <div class="findings-stats">
      <button class="findings-stat" class:active={statusFilter === 'all'} onclick={() => statusFilter = 'all'}>
        <strong>{findings.length}</strong>
        <span>Всего</span>
      </button>
      <button class="findings-stat" class:active={statusFilter === 'extracted'} onclick={() => statusFilter = statusFilter === 'extracted' ? 'all' : 'extracted'}>
        <strong>{statusCounts.extracted}</strong>
        <span>Извлечено</span>
      </button>
      <button class="findings-stat" class:active={statusFilter === 'validated'} onclick={() => statusFilter = statusFilter === 'validated' ? 'all' : 'validated'}>
        <strong>{statusCounts.validated}</strong>
        <span>Проверено</span>
      </button>
      <button class="findings-stat" class:active={statusFilter === 'disputed'} onclick={() => statusFilter = statusFilter === 'disputed' ? 'all' : 'disputed'}>
        <strong>{statusCounts.disputed}</strong>
        <span>Оспаривается</span>
      </button>
      <button class="findings-stat" class:active={statusFilter === 'superseded'} onclick={() => statusFilter = statusFilter === 'superseded' ? 'all' : 'superseded'}>
        <strong>{statusCounts.superseded}</strong>
        <span>Устарело</span>
      </button>
    </div>

    <!-- Фильтры: поиск + субъект -->
    <div class="findings-filters">
      <div class="findings-search">
        <Search size={18} />
        <input type="text" placeholder="Поиск по находкам…" bind:value={searchQuery} />
        {#if searchQuery}
          <button onclick={() => searchQuery = ''} aria-label="Очистить"><X size={16} /></button>
        {/if}
      </div>
      {#if uniqueSubjects.length > 0}
        <div class="findings-subject-filter">
          <select bind:value={subjectFilter}>
            <option value="">Все субъекты</option>
            {#each uniqueSubjects as subject}
              <option value={subject}>{subject}</option>
            {/each}
          </select>
          {#if subjectFilter}
            <button onclick={() => subjectFilter = ''} aria-label="Сбросить субъект"><X size={14} /></button>
          {/if}
        </div>
      {/if}
    </div>

    {#if filteredFindings.length === 0}
      <div class="workspace-panel empty-state">
        <Lightbulb size={32} />
        <h2>Находок не найдено</h2>
        <p>{#if findings.length === 0}База знаний пуста. Выполните запрос или загрузите документы.{:else}Попробуйте изменить фильтр или поисковый запрос.{/if}</p>
      </div>
    {:else}
      <div class="findings-list workspace-panel">
        {#each filteredFindings as finding, i}
          <article class="finding-row">
            <span class="finding-row__num">{i + 1}</span>
            <div class="finding-row__body">
              <!-- SPO triple -->
              {#if finding.subject || finding.predicate}
                <div class="finding-row__spo">
                  {#if finding.subject}
                    <span class="finding-row__subject">{finding.subject}</span>
                  {/if}
                  {#if finding.subject && finding.predicate}
                    <span class="finding-row__arrow">→</span>
                  {/if}
                  {#if finding.predicate}
                    <span class="finding-row__predicate">{finding.predicate}</span>
                  {/if}
                </div>
              {/if}

              <p class="finding-row__statement">{finding.statement}</p>

              <div class="finding-row__meta">
                <span class={`status status--${finding.status}`}>
                  {statusLabel(finding.status)}
                </span>
                <span class="finding-row__conf" style={`color:${confColor(finding.confidence)}`}>
                  Уверенность: {pct(finding.confidence)}
                </span>
                {#if finding.evidence.length > 0}
                  <span class="finding-row__ev-count">
                    <FileText size={12} /> {finding.evidence.length} источник{finding.evidence.length === 1 ? '' : finding.evidence.length < 5 ? 'а' : 'ов'}
                  </span>
                {/if}
                {#if finding.observations.length > 0}
                  <span class="finding-row__obs-count">
                    <BarChart3 size={12} /> {finding.observations.length} наблюд.
                  </span>
                {/if}
                <button class="finding-row__expand" onclick={() => toggleFinding(finding.id)}>
                  {expandedFindings.has(finding.id) ? 'Свернуть' : 'Подробнее'}
                  <ChevronDown size={13} class={expandedFindings.has(finding.id) ? 'rotated' : ''} />
                </button>
              </div>

              <!-- Expandable: Evidence + Observations -->
              {#if expandedFindings.has(finding.id)}
                <div class="finding-detail">
                  <!-- Evidence -->
                  {#if finding.evidence.length > 0}
                    <div class="finding-detail__section">
                      <h4><FileText size={14} /> Источники ({finding.evidence.length})</h4>
                      {#each finding.evidence as ev, ei}
                        {@const evKey = `${finding.id}-ev-${ei}`}
                        <div class="evidence-item">
                          <div class="evidence-item__header">
                            <strong>{ev.source_title || 'Источник'}</strong>
                            {#if ev.page}
                              <span class="evidence-item__page">Стр. {ev.page}</span>
                            {/if}
                          </div>
                          <p class="evidence-item__quote">
                            {expandedEvidence.has(evKey) ? ev.quote : ev.quote.slice(0, 120) + (ev.quote.length > 120 ? '…' : '')}
                          </p>
                          {#if ev.quote.length > 120}
                            <button class="evidence-item__toggle" onclick={() => toggleEvidence(evKey)}>
                              {expandedEvidence.has(evKey) ? 'Свернуть' : 'Показать полностью'}
                            </button>
                          {/if}
                        </div>
                      {/each}
                    </div>
                  {/if}

                  <!-- Observations -->
                  {#if finding.observations.length > 0}
                    <div class="finding-detail__section">
                      <h4><BarChart3 size={14} /> Числовые данные ({finding.observations.length})</h4>
                      <div class="observations-grid">
                        {#each finding.observations as obs}
                          <div class="observation-chip">
                            <span class="observation-chip__name">{obs.name}</span>
                            <span class="observation-chip__value">{obs.value}</span>
                            <span class="observation-chip__unit">{obs.unit}</span>
                          </div>
                        {/each}
                      </div>
                    </div>
                  {/if}

                  {#if finding.evidence.length === 0 && finding.observations.length === 0}
                    <p class="finding-detail__empty">Нет подробных данных для этой находки.</p>
                  {/if}
                </div>
              {/if}
            </div>
            <div class="finding-row__bar">
              <div style={`width:${pct(finding.confidence)};background:${confColor(finding.confidence)}`}></div>
            </div>
          </article>
        {/each}
      </div>

      <!-- Граф находок -->
      {#if graph.nodes.length > 0}
        <section class="workspace-panel map-layout" style="margin-top:20px">
          <div class="map-explainer">
            <p class="section-label">Визуализация</p>
            <h2>Граф связей</h2>
            <p>Кликните на узел, чтобы увидеть детали. Двойной клик — фокус на узле.</p>
          </div>
          <GraphCanvas
            {graph}
            selectedId={selectedNode?.id}
            onselect={(node) => selectedNode = node}
          />
        </section>
      {/if}
    {/if}
  {/if}
</main>

<style>
  .findings-export {
    display: flex;
    gap: 8px;
  }
  .findings-stats {
    display: flex;
    gap: 12px;
    margin-bottom: 20px;
  }
  .findings-stat {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    padding: 18px;
    background: white;
    border: 1px solid var(--line);
    border-radius: 12px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .findings-stat:hover {
    border-color: var(--cobalt);
  }
  .findings-stat.active {
    border-color: var(--accent);
    background: #fdf9f7;
  }
  .findings-stat strong {
    font-size: 28px;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
  }
  .findings-stat span {
    font-size: 12px;
    color: var(--muted);
  }
  .findings-filters {
    display: flex;
    gap: 12px;
    margin-bottom: 20px;
    align-items: stretch;
  }
  .findings-search {
    flex: 1;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 16px;
    background: white;
    border: 1px solid var(--line);
    border-radius: 10px;
  }
  .findings-search input {
    flex: 1;
    border: 0;
    outline: 0;
    font-size: 14px;
    background: transparent;
    color: var(--ink);
  }
  .findings-search input::placeholder {
    color: var(--muted);
  }
  .findings-search button {
    border: 0;
    background: transparent;
    cursor: pointer;
    color: var(--muted);
    display: grid;
    place-items: center;
  }
  .findings-search button:hover {
    color: var(--red);
  }
  .findings-subject-filter {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 12px;
    background: white;
    border: 1px solid var(--line);
    border-radius: 10px;
    min-width: 200px;
  }
  .findings-subject-filter select {
    flex: 1;
    border: 0;
    outline: 0;
    font-size: 14px;
    background: transparent;
    color: var(--ink);
    padding: 12px 0;
    cursor: pointer;
  }
  .findings-subject-filter button {
    border: 0;
    background: transparent;
    cursor: pointer;
    color: var(--muted);
    display: grid;
    place-items: center;
  }
  .findings-subject-filter button:hover {
    color: var(--red);
  }
  .findings-list {
    display: flex;
    flex-direction: column;
  }
  .finding-row {
    display: grid;
    grid-template-columns: 36px 1fr 120px;
    gap: 16px;
    padding: 18px 0;
    border-bottom: 1px solid var(--line);
    align-items: start;
  }
  .finding-row:last-child {
    border: 0;
  }
  .finding-row__num {
    display: grid;
    place-items: center;
    width: 28px;
    height: 28px;
    background: var(--ink);
    color: white;
    border-radius: 8px 3px 8px 3px;
    font-size: 11px;
    font-weight: 800;
  }
  .finding-row__body {
    min-width: 0;
  }
  .finding-row__spo {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    font-size: 11px;
    margin-bottom: 6px;
  }
  .finding-row__subject {
    font-weight: 700;
    color: var(--cobalt);
  }
  .finding-row__predicate {
    font-weight: 600;
    color: var(--accent);
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 0.04em;
  }
  .finding-row__arrow {
    color: var(--muted);
    font-size: 11px;
  }
  .finding-row__statement {
    margin: 0 0 8px;
    font-size: 15px;
    line-height: 1.5;
    color: var(--ink);
  }
  .finding-row__meta {
    display: flex;
    gap: 12px;
    font-size: 11px;
    align-items: center;
    flex-wrap: wrap;
  }
  .finding-row__conf {
    font-weight: 600;
  }
  .finding-row__ev-count,
  .finding-row__obs-count {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    color: var(--muted);
  }
  .finding-row__ev-count :global(svg),
  .finding-row__obs-count :global(svg) {
    flex-shrink: 0;
  }
  .finding-row__expand {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    border: 1px solid var(--line);
    background: white;
    border-radius: 6px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    color: var(--ink);
    transition: all 0.12s;
    margin-left: auto;
  }
  .finding-row__expand:hover {
    border-color: var(--cobalt);
    color: var(--cobalt);
  }
  .finding-row__bar {
    height: 6px;
    background: var(--surface);
    border-radius: 3px;
    overflow: hidden;
    margin-top: 6px;
  }
  .finding-row__bar > div {
    height: 100%;
    border-radius: 3px;
    transition: width 0.3s;
  }

  /* Finding Detail (expanded) */
  .finding-detail {
    margin-top: 12px;
    padding: 14px;
    background: var(--surface);
    border-radius: 10px;
  }
  .finding-detail__section {
    margin-bottom: 14px;
  }
  .finding-detail__section:last-child {
    margin-bottom: 0;
  }
  .finding-detail__section h4 {
    display: flex;
    align-items: center;
    gap: 6px;
    margin: 0 0 10px;
    font-size: 12px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .finding-detail__empty {
    margin: 0;
    font-size: 12px;
    color: var(--muted);
    font-style: italic;
  }

  /* Evidence Items */
  .evidence-item {
    padding: 10px 0;
    border-bottom: 1px solid var(--line);
  }
  .evidence-item:last-child {
    border: 0;
  }
  .evidence-item__header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
  }
  .evidence-item__header strong {
    font-size: 13px;
    color: var(--ink);
  }
  .evidence-item__page {
    font-size: 11px;
    color: var(--muted);
    white-space: nowrap;
  }
  .evidence-item__quote {
    margin: 0;
    font-size: 12px;
    line-height: 1.5;
    color: #555f59;
  }
  .evidence-item__toggle {
    border: 0;
    background: transparent;
    color: var(--accent);
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    padding: 4px 0 0;
  }

  /* Observations */
  .observations-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .observation-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 4px 10px;
    border-radius: 6px;
    background: #e8f5f3;
    color: #0f766e;
    font-size: 11px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .observation-chip__name {
    font-weight: 700;
  }
  .observation-chip__value {
    font-weight: 800;
  }
  .observation-chip__unit {
    font-weight: 400;
    opacity: 0.8;
  }

  :global(.rotated) {
    transform: rotate(180deg);
    transition: transform 0.15s;
  }

  @media (max-width: 768px) {
    .findings-stats {
      flex-wrap: wrap;
    }
    .findings-stat {
      flex: 1 1 45%;
    }
    .findings-filters {
      flex-direction: column;
    }
    .finding-row {
      grid-template-columns: 28px 1fr;
    }
    .finding-row__bar {
      grid-column: 2;
    }
  }
</style>
