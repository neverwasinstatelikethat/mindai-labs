<script lang="ts">
  import { onMount } from 'svelte';
  import {
    AlertTriangle, LoaderCircle, Lock, Network, Search,
  } from '@lucide/svelte';
  import AppHeader from '$lib/AppHeader.svelte';
  import GraphCanvas from '$lib/GraphCanvas.svelte';
  import { api } from '$lib/api';
  import { getRole } from '$lib/roleStore.svelte';
  import { hasPermission, type GraphNode, type GraphSnapshot } from '$lib/types';

  const currentRole = $derived(getRole());
  const canAccess = $derived(hasPermission(currentRole, 'knowledge:read'));

  let graph = $state<GraphSnapshot>({ nodes: [], edges: [], communities: [] });
  let loading = $state(true);
  let error = $state('');
  let selectedNode = $state<GraphNode | null>(null);
  let nodeSearch = $state('');

  const nodeTypeCounts = $derived.by<Record<string, number>>(() => {
    const counts: Record<string, number> = {};
    for (const node of graph.nodes) {
      counts[node.type] = (counts[node.type] ?? 0) + 1;
    }
    return counts;
  });

  const typeLabels: Record<string, string> = {
    material: 'Материал', process: 'Метод', equipment: 'Оборудование', condition: 'Условие',
    claim: 'Вывод', experiment: 'Эксперимент', publication: 'Источник', expert: 'Эксперт',
    location: 'Локация', organization: 'Организация', chunk: 'Фрагмент',
  };

  const filteredSearchResults = $derived.by<GraphNode[]>(() => {
    const q = nodeSearch.trim().toLowerCase();
    if (!q) return [];
    return graph.nodes.filter((n) => {
      if (n.label.toLowerCase().includes(q)) return true;
      if (n.type.toLowerCase().includes(q)) return true;
      if (typeLabels[n.type]?.toLowerCase().includes(q)) return true;
      return false;
    }).slice(0, 15);
  });

  onMount(async () => {
    try {
      graph = await api.graph();
    } catch (reason) {
      error = reason instanceof Error ? reason.message : 'Не удалось загрузить граф знаний';
    } finally {
      loading = false;
    }
  });
</script>

<svelte:head><title>Граф знаний — Научный Клубок</title></svelte:head>

<AppHeader compact />
<main class="workspace" id="main">
  <section class="page-heading">
    <div>
      <p class="section-label">Визуализация</p>
      <h1>Граф знаний</h1>
    </div>
    {#if !loading && !error}
      <div class="graph-node-count">
        <strong>{graph.nodes.length}</strong> узлов · <strong>{graph.edges.length}</strong> связей
      </div>
    {/if}
  </section>

  {#if !canAccess}
    <div class="permission-denied workspace-panel">
      <Lock size={36} />
      <h2>Доступ запрещён</h2>
      <p>Для просмотра графа знаний требуется роль «Исследователь» или выше.</p>
    </div>
  {:else if loading}
    <div class="workspace-panel loading-state">
      <LoaderCircle class="spin" size={28} />
      <p>Загрузка графа знаний…</p>
    </div>
  {:else if error}
    <div class="notice notice--error">
      <AlertTriangle size={19} />
      <div><strong>Ошибка загрузки</strong><p>{error}</p></div>
    </div>
  {:else}
    <!-- Статистика по типам узлов -->
    {#if Object.keys(nodeTypeCounts).length > 0}
      <div class="graph-type-stats">
        {#each Object.entries(nodeTypeCounts) as [type, count]}
          <div class="graph-type-stat">
            <span class="graph-type-stat__label">{typeLabels[type] ?? type}</span>
            <strong>{count}</strong>
          </div>
        {/each}
      </div>
    {/if}

    <!-- Поиск узлов -->
    <div class="graph-search-bar">
      <Search size={18} />
      <input type="text" placeholder="Поиск по узлам графа…" bind:value={nodeSearch} />
      {#if nodeSearch}
        <span class="graph-search-count">{filteredSearchResults.length} найдено</span>
      {/if}
    </div>
    {#if nodeSearch && filteredSearchResults.length > 0}
      <div class="graph-search-results">
        {#each filteredSearchResults as node}
          <button class="graph-search-result" onclick={() => selectedNode = node}>
            <span>{node.label}</span>
            <small>{typeLabels[node.type] ?? node.type}</small>
          </button>
        {/each}
      </div>
    {/if}

    <!-- Граф -->
    <section class="workspace-panel map-layout">
      <div class="map-explainer">
        <p class="section-label">Интерактивная карта</p>
        <h2>Структура знаний</h2>
        <p>Сообщества показаны свернутыми кластерами. Кликните для разворачивания. Двойной клик по узлу — фокусный режим. Используйте поиск, чтобы найти нужный элемент.</p>
      </div>
      {#if graph.nodes.length > 0}
        <GraphCanvas
          {graph}
          selectedId={selectedNode?.id}
          onselect={(node) => selectedNode = node}
        />
      {:else}
        <div class="graph-empty">
          <Network size={40} />
          <p>Граф знаний пуст. Загрузите документы или выполните запрос.</p>
        </div>
      {/if}
    </section>
  {/if}
</main>

<style>
  .graph-node-count {
    font-size: 14px;
    color: var(--muted);
  }
  .graph-node-count strong {
    color: var(--ink);
    font-variant-numeric: tabular-nums;
  }
  .graph-type-stats {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-bottom: 20px;
  }
  .graph-type-stat {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 14px;
    background: white;
    border: 1px solid var(--line);
    border-radius: 8px;
  }
  .graph-type-stat__label {
    font-size: 12px;
    color: var(--muted);
  }
  .graph-type-stat strong {
    font-size: 14px;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
  }
  .graph-search-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 16px;
    background: white;
    border: 1px solid var(--line);
    border-radius: 10px;
    margin-bottom: 12px;
  }
  .graph-search-bar input {
    flex: 1;
    border: 0;
    outline: 0;
    font-size: 14px;
    background: transparent;
    color: var(--ink);
  }
  .graph-search-bar input::placeholder {
    color: var(--muted);
  }
  .graph-search-count {
    font-size: 12px;
    color: var(--muted);
    white-space: nowrap;
  }
  .graph-search-results {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 20px;
  }
  .graph-search-result {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 14px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: white;
    cursor: pointer;
    transition: all 0.12s;
  }
  .graph-search-result:hover {
    border-color: var(--cobalt);
    background: #f0f4ff;
  }
  .graph-search-result span {
    font-size: 13px;
    color: var(--ink);
  }
  .graph-search-result small {
    font-size: 11px;
    color: var(--muted);
  }
  .graph-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    padding: 60px 20px;
    color: var(--muted);
    text-align: center;
  }
</style>
