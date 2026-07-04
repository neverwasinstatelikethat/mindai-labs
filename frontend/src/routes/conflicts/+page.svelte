<script lang="ts">
  import { onMount } from 'svelte';
  import {
    AlertTriangle, BarChart3, ChevronDown, FileText,
    Lightbulb, LoaderCircle, Lock, Search, X,
  } from '@lucide/svelte';
  import AppHeader from '$lib/AppHeader.svelte';
  import { api } from '$lib/api';
  import { getRole } from '$lib/roleStore.svelte';
  import {
    hasPermission,
    type FindingListItem,
  } from '$lib/types';

  const currentRole = $derived(getRole());
  const canAccess = $derived(hasPermission(currentRole, 'knowledge:read'));

  // ── State ──────────────────────────────────────────────────────
  let conflicts = $state<FindingListItem[]>([]);
  let loading = $state(true);
  let error = $state('');
  let searchQuery = $state('');
  let expandedGroups = $state<Set<string>>(new Set());
  let expandedEvidence = $state<Set<string>>(new Set());

  // ── Derived ───────────────────────────────────────────────────
  // Group disputed findings by subject for side-by-side comparison
  const conflictGroups = $derived.by<{ subject: string; findings: FindingListItem[] }[]>(() => {
    const groups = new Map<string, FindingListItem[]>();
    for (const f of conflicts) {
      const key = f.subject || 'Без субъекта';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(f);
    }
    return Array.from(groups.entries()).map(([subject, findings]) => ({ subject, findings }));
  });

  const filteredGroups = $derived.by<{ subject: string; findings: FindingListItem[] }[]>(() => {
    if (!searchQuery.trim()) return conflictGroups;
    const q = searchQuery.trim().toLowerCase();
    return conflictGroups
      .map((g) => ({
        subject: g.subject,
        findings: g.findings.filter((f) =>
          f.statement.toLowerCase().includes(q) ||
          (f.predicate ?? '').toLowerCase().includes(q) ||
          f.evidence.some((e) => e.source_title.toLowerCase().includes(q) || e.quote.toLowerCase().includes(q))
        ),
      }))
      .filter((g) => g.findings.length > 0);
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

  function toggleGroup(key: string): void {
    const next = new Set(expandedGroups);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    expandedGroups = next;
  }

  function toggleEvidence(key: string): void {
    const next = new Set(expandedEvidence);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    expandedEvidence = next;
  }

  // ── Lifecycle ──────────────────────────────────────────────────
  onMount(async () => {
    try {
      conflicts = await api.conflicts();
    } catch (reason) {
      error = reason instanceof Error ? reason.message : 'Не удалось загрузить конфликты';
    } finally {
      loading = false;
    }
  });
</script>

<svelte:head><title>Конфликты — Научный Клубок</title></svelte:head>

<AppHeader compact />
<main class="workspace" id="main">
  <section class="page-heading">
    <div>
      <p class="section-label">База знаний</p>
      <h1>Конфликты и расхождения</h1>
    </div>
  </section>

  {#if !canAccess}
    <div class="permission-denied workspace-panel">
      <Lock size={36} />
      <h2>Доступ запрещён</h2>
      <p>Для просмотра конфликтов требуется роль «Исследователь» или выше.</p>
    </div>
  {:else if loading}
    <div class="workspace-panel loading-state">
      <LoaderCircle class="spin" size={28} />
      <p>Загрузка конфликтов из базы знаний…</p>
    </div>
  {:else if error}
    <div class="notice notice--error">
      <AlertTriangle size={19} />
      <div><strong>Ошибка загрузки</strong><p>{error}</p></div>
    </div>
  {:else if conflicts.length === 0}
    <div class="workspace-panel empty-state">
      <Lightbulb size={32} />
      <h2>Конфликтов не найдено</h2>
      <p>В базе знаний нет оспариваемых утверждений. Это может означать, что данные согласованы или корпус недостаточно большой.</p>
    </div>
  {:else}
    <!-- Статистика -->
    <div class="conflicts-stats">
      <div class="conflicts-stat">
        <strong>{conflicts.length}</strong>
        <span>Оспариваемых утверждений</span>
      </div>
      <div class="conflicts-stat">
        <strong>{conflictGroups.length}</strong>
        <span>Групп конфликтов</span>
      </div>
      <div class="conflicts-stat">
        <strong>{conflicts.reduce((sum, f) => sum + f.evidence.length, 0)}</strong>
        <span>Источников</span>
      </div>
    </div>

    <!-- Поиск -->
    <div class="conflicts-search">
      <Search size={18} />
      <input type="text" placeholder="Поиск по конфликтам…" bind:value={searchQuery} />
      {#if searchQuery}
        <button onclick={() => searchQuery = ''} aria-label="Очистить"><X size={16} /></button>
      {/if}
    </div>

    {#if filteredGroups.length === 0}
      <div class="workspace-panel empty-state">
        <Lightbulb size={32} />
        <h2>Ничего не найдено</h2>
        <p>Попробуйте изменить поисковый запрос.</p>
      </div>
    {:else}
      <div class="conflicts-list workspace-panel">
        {#each filteredGroups as group}
          {@const groupKey = group.subject}
          <article class="conflict-group">
            <button class="conflict-group__header" onclick={() => toggleGroup(groupKey)}>
              <div class="conflict-group__title">
                <AlertTriangle size={18} />
                <h3>{group.subject}</h3>
                <span class="conflict-group__count">{group.findings.length} утверждени{group.findings.length === 1 ? 'е' : group.findings.length < 5 ? 'я' : 'й'}</span>
              </div>
              <ChevronDown size={18} class={expandedGroups.has(groupKey) ? 'rotated' : ''} />
            </button>

            <div class="conflict-group__body">
              <p class="conflict-group__hint">
                Найдено {group.findings.length} оспариваем{group.findings.length === 1 ? 'е' : 'их'} утверждени{group.findings.length === 1 ? 'е' : 'й'} по этой теме. Сравните источники и доказательства ниже.
              </p>

              <div class="conflict-comparison">
                {#each group.findings as finding, fi}
                  <div class="conflict-card">
                    <div class="conflict-card__num">{fi + 1}</div>
                    <div class="conflict-card__content">
                      {#if finding.predicate}
                        <span class="conflict-card__predicate">{finding.predicate}</span>
                      {/if}
                      <p class="conflict-card__statement">{finding.statement}</p>

                      <div class="conflict-card__meta">
                        <span class="finding-row__conf" style={`color:${confColor(finding.confidence)}`}>
                          {pct(finding.confidence)}
                        </span>
                        <div class="conflict-card__bar">
                          <div style={`width:${pct(finding.confidence)};background:${confColor(finding.confidence)}`}></div>
                        </div>
                      </div>

                      <!-- Evidence -->
                      {#if finding.evidence.length > 0}
                        <div class="conflict-card__evidence">
                          <h5><FileText size={12} /> Источники ({finding.evidence.length})</h5>
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
                                {expandedEvidence.has(evKey) ? ev.quote : ev.quote.slice(0, 100) + (ev.quote.length > 100 ? '…' : '')}
                              </p>
                              {#if ev.quote.length > 100}
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
                        <div class="conflict-card__observations">
                          <h5><BarChart3 size={12} /> Данные</h5>
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
                    </div>
                  </div>
                {/each}
              </div>
            </div>
          </article>
        {/each}
      </div>
    {/if}
  {/if}
</main>

<style>
  .conflicts-stats {
    display: flex;
    gap: 12px;
    margin-bottom: 20px;
  }
  .conflicts-stat {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    padding: 18px;
    background: white;
    border: 1px solid var(--line);
    border-radius: 12px;
  }
  .conflicts-stat strong {
    font-size: 28px;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
  }
  .conflicts-stat span {
    font-size: 12px;
    color: var(--muted);
  }
  .conflicts-search {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 16px;
    background: white;
    border: 1px solid var(--line);
    border-radius: 10px;
    margin-bottom: 20px;
  }
  .conflicts-search input {
    flex: 1;
    border: 0;
    outline: 0;
    font-size: 14px;
    background: transparent;
    color: var(--ink);
  }
  .conflicts-search input::placeholder {
    color: var(--muted);
  }
  .conflicts-search button {
    border: 0;
    background: transparent;
    cursor: pointer;
    color: var(--muted);
    display: grid;
    place-items: center;
  }
  .conflicts-search button:hover {
    color: var(--red);
  }
  .conflicts-list {
    display: flex;
    flex-direction: column;
  }

  /* Conflict Group */
  .conflict-group {
    border: 1px solid var(--line);
    border-radius: 12px;
    margin-bottom: 16px;
    overflow: hidden;
  }
  .conflict-group:last-child {
    margin-bottom: 0;
  }
  .conflict-group__header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
    padding: 16px 20px;
    background: #fef3e2;
    border: 0;
    border-bottom: 1px solid var(--line);
    border-radius: 0;
    cursor: pointer;
    text-align: left;
    font: inherit;
    transition: background 0.15s;
  }
  .conflict-group__header:hover {
    background: #fde8c8;
  }
  .conflict-group__title {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .conflict-group__title :global(svg) {
    color: var(--amber);
    flex-shrink: 0;
  }
  .conflict-group__title h3 {
    margin: 0;
    font-size: 16px;
    color: var(--ink);
  }
  .conflict-group__count {
    font-size: 12px;
    color: var(--muted);
    background: white;
    padding: 2px 8px;
    border-radius: 5px;
    font-weight: 600;
  }
  .conflict-group__body {
    padding: 16px 20px;
  }
  .conflict-group__hint {
    margin: 0 0 16px;
    font-size: 13px;
    color: var(--muted);
    line-height: 1.5;
  }

  /* Conflict Comparison Cards */
  .conflict-comparison {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 14px;
  }
  .conflict-card {
    display: flex;
    gap: 12px;
    padding: 14px;
    background: var(--surface);
    border-radius: 10px;
    border-left: 3px solid var(--amber);
  }
  .conflict-card__num {
    display: grid;
    place-items: center;
    width: 24px;
    height: 24px;
    background: var(--amber);
    color: white;
    border-radius: 6px 3px 6px 3px;
    font-size: 11px;
    font-weight: 800;
    flex-shrink: 0;
  }
  .conflict-card__content {
    flex: 1;
    min-width: 0;
  }
  .conflict-card__predicate {
    display: inline-block;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--accent);
    margin-bottom: 6px;
  }
  .conflict-card__statement {
    margin: 0 0 10px;
    font-size: 14px;
    line-height: 1.5;
    color: var(--ink);
  }
  .conflict-card__meta {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 12px;
  }
  .finding-row__conf {
    font-size: 12px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }
  .conflict-card__bar {
    flex: 1;
    height: 5px;
    background: var(--line);
    border-radius: 3px;
    overflow: hidden;
  }
  .conflict-card__bar > div {
    height: 100%;
    border-radius: 3px;
    transition: width 0.3s;
  }

  /* Evidence in conflict cards */
  .conflict-card__evidence {
    margin-bottom: 12px;
  }
  .conflict-card__evidence h5 {
    display: flex;
    align-items: center;
    gap: 5px;
    margin: 0 0 8px;
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .conflict-card__observations h5 {
    display: flex;
    align-items: center;
    gap: 5px;
    margin: 0 0 8px;
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .evidence-item {
    padding: 8px 0;
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
    font-size: 12px;
    color: var(--ink);
  }
  .evidence-item__page {
    font-size: 10px;
    color: var(--muted);
    white-space: nowrap;
  }
  .evidence-item__quote {
    margin: 0;
    font-size: 11px;
    line-height: 1.5;
    color: #555f59;
  }
  .evidence-item__toggle {
    border: 0;
    background: transparent;
    color: var(--accent);
    font-size: 10px;
    font-weight: 600;
    cursor: pointer;
    padding: 3px 0 0;
  }

  /* Observations */
  .conflict-card__observations {
    margin-bottom: 0;
  }
  .observations-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
  }
  .observation-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 8px;
    border-radius: 5px;
    background: #e8f5f3;
    color: #0f766e;
    font-size: 10px;
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
    .conflicts-stats {
      flex-wrap: wrap;
    }
    .conflicts-stat {
      flex: 1 1 45%;
    }
    .conflict-comparison {
      grid-template-columns: 1fr;
    }
  }
</style>
