<script lang="ts">
  import { onMount } from 'svelte';
  import {
    AlertTriangle, BarChart3, CheckCircle2, Clock, Database, FileText,
    LoaderCircle, Network, TrendingUp, Zap,
  } from '@lucide/svelte';
  import AppHeader from '$lib/AppHeader.svelte';
  import { api } from '$lib/api';
  import { type ActivityEntry, type AgentMetric, type DashboardData } from '$lib/types';

  let data: DashboardData | null = $state(null);
  let loading = $state(true);
  let error = $state('');

  const chartMax = $derived.by(() => {
    if (!data) return 1;
    return Math.max(data.documents, data.claims, data.entities, data.evidence, 1);
  });

  onMount(async () => {
    try {
      data = await api.dashboard();
    } catch (reason) {
      error = reason instanceof Error ? reason.message : 'Не удалось загрузить данные';
    } finally {
      loading = false;
    }
  });

  function formatTime(ts: string): string {
    try {
      return new Date(ts).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
    } catch {
      return ts;
    }
  }

  function formatDateTime(ts: string): string {
    try {
      return new Date(ts).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
    } catch {
      return ts;
    }
  }

  function actionLabel(action: string): string {
    const labels: Record<string, string> = {
      'query.run': 'Запрос',
      'feedback.submit': 'Отзыв',
      'proposal.review': 'Проверка',
      'compare.run': 'Сравнение',
      'export.run': 'Экспорт',
    };
    return labels[action] ?? action;
  }
</script>

<svelte:head><title>Дашборд — Научный Клубок</title></svelte:head>

<AppHeader compact />
<main class="workspace" id="main">
  <section class="page-heading">
    <div>
      <p class="section-label">Панель администратора</p>
      <h1>Дашборд системы</h1>
    </div>
  </section>

  {#if loading}
    <div class="workspace-panel loading-state">
      <LoaderCircle class="spin" size={28} />
      <p>Загрузка данных дашборда…</p>
    </div>
  {:else if error}
    <div class="notice notice--error">
      <AlertTriangle size={19} />
      <div><strong>Ошибка загрузки</strong><p>{error}</p></div>
    </div>
  {:else if data}
    <!-- Coverage stats -->
    <div class="dash-grid">
      <div class="dash-card">
        <Database size={22} />
        <div>
          <strong>{data.documents}</strong>
          <span>Документов</span>
        </div>
      </div>
      <div class="dash-card">
        <FileText size={22} />
        <div>
          <strong>{data.claims}</strong>
          <span>Утверждений</span>
        </div>
      </div>
      <div class="dash-card">
        <Network size={22} />
        <div>
          <strong>{data.entities}</strong>
          <span>Сущностей</span>
        </div>
      </div>
      <div class="dash-card">
        <CheckCircle2 size={22} />
        <div>
          <strong>{data.evidence}</strong>
          <span>Доказательств</span>
        </div>
      </div>
      <div class="dash-card dash-card--warn">
        <AlertTriangle size={22} />
        <div>
          <strong>{data.conflicts}</strong>
          <span>Противоречий</span>
        </div>
      </div>
      <div class="dash-card dash-card--info">
        <TrendingUp size={22} />
        <div>
          <strong>{data.gaps}</strong>
          <span>Пробелов</span>
        </div>
      </div>
    </div>

    <!-- Coverage bar chart (CSS-based) -->
    <section class="workspace-panel dash-section">
      <h2><BarChart3 size={20} /> Состав корпуса</h2>
      <div class="dash-chart">
        <div class="dash-chart__row">
          <span class="dash-chart__label">Документы</span>
          <div class="dash-chart__bar-wrap">
            <div class="dash-chart__bar" style={`width:${(data.documents / chartMax) * 100}%`}></div>
          </div>
          <strong>{data.documents}</strong>
        </div>
        <div class="dash-chart__row">
          <span class="dash-chart__label">Утверждения</span>
          <div class="dash-chart__bar-wrap">
            <div class="dash-chart__bar dash-chart__bar--copper" style={`width:${(data.claims / chartMax) * 100}%`}></div>
          </div>
          <strong>{data.claims}</strong>
        </div>
        <div class="dash-chart__row">
          <span class="dash-chart__label">Сущности</span>
          <div class="dash-chart__bar-wrap">
            <div class="dash-chart__bar dash-chart__bar--teal" style={`width:${(data.entities / chartMax) * 100}%`}></div>
          </div>
          <strong>{data.entities}</strong>
        </div>
        <div class="dash-chart__row">
          <span class="dash-chart__label">Доказательства</span>
          <div class="dash-chart__bar-wrap">
            <div class="dash-chart__bar dash-chart__bar--blue" style={`width:${(data.evidence / chartMax) * 100}%`}></div>
          </div>
          <strong>{data.evidence}</strong>
        </div>
      </div>
    </section>

    <div class="dash-two-col">
      <!-- Recent Activity -->
      <section class="workspace-panel dash-section">
        <h2><Clock size={20} /> Недавняя активность</h2>
        {#if data.recent_activity.length}
          <div class="activity-list">
            {#each data.recent_activity as entry}
              <div class="activity-item">
                <div class="activity-item__icon" data-outcome={entry.outcome}>
                  {entry.outcome === 'success' ? '✓' : entry.outcome === 'denied' ? '✕' : '○'}
                </div>
                <div class="activity-item__body">
                  <strong>{actionLabel(entry.action)}</strong>
                  <span>{entry.object_id?.slice(0, 36) ?? '—'}</span>
                </div>
                <div class="activity-item__meta">
                  <small>{entry.actor_id}</small>
                  <time>{formatTime(entry.created_at)}</time>
                </div>
              </div>
            {/each}
          </div>
        {:else}
          <div class="empty-state small"><Clock size={28} /><p>Нет записей активности.</p></div>
        {/if}
      </section>

      <!-- Agent Metrics -->
      <section class="workspace-panel dash-section">
        <h2><Zap size={20} /> Метрики агентов</h2>
        {#if data.agent_metrics?.agents?.length}
          <div class="agent-metrics-table">
            {#each data.agent_metrics.agents as metric}
              <div class="agent-metric-row">
                <div class="agent-metric-name">
                  <strong>{metric.agent}</strong>
                </div>
                <div class="agent-metric-stats">
                  <span title="Вызовов">{metric.calls}</span>
                  <span title="Успех">{Math.round(metric.success_rate * 100)}%</span>
                  <span title="p50">{Math.round(metric.p50_duration_ms)}мс</span>
                  <span title="p95">{Math.round(metric.p95_duration_ms)}мс</span>
                </div>
                <div class="agent-metric-bar">
                  <div class="agent-metric-bar__fill" style={`width:${metric.success_rate * 100}%`}></div>
                </div>
              </div>
            {/each}
          </div>
        {:else}
          <div class="empty-state small"><Zap size={28} /><p>Метрики недоступны.</p></div>
        {/if}
      </section>
    </div>
  {:else}
    <div class="workspace-panel empty-state">
      <Database size={32} />
      <h2>Нет данных</h2>
      <p>Дашборд пуст. Возможно, корпус ещё не наполнен.</p>
    </div>
  {/if}
</main>
