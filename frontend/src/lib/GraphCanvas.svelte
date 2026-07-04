<script lang="ts">
  import type { GraphNode, GraphEdge, GraphSnapshot } from './types';
  import {
    ZoomIn, ZoomOut, Maximize, Search, Network, X, ChevronDown,
    Eye, EyeOff, MousePointer2, Focus, GitBranch, Lightbulb,
  } from '@lucide/svelte';

  interface Props {
    graph: GraphSnapshot;
    selectedId?: string;
    onselect?: (node: GraphNode | null) => void;
    onAskNode?: (node: GraphNode) => void;
  }

  let { graph, selectedId, onselect, onAskNode }: Props = $props();

  // ── Константы ──────────────────────────────────────────────────
  const W = 900;
  const H = 540;
  const cx = W / 2;
  const cy = H / 2;

  // Точные цвета из product design
  const colors: Record<string, string> = {
    material: '#2563eb', process: '#0891b2', equipment: '#d97706', condition: '#7c3aed',
    claim: '#059669', experiment: '#dc2626', publication: '#4b5563', expert: '#db2777',
    location: '#65a30d', organization: '#ca8a04', chunk: '#8b7355',
  };

  const typeLabels: Record<string, string> = {
    material: 'Материал', process: 'Метод', equipment: 'Оборудование', condition: 'Условие',
    claim: 'Вывод', experiment: 'Эксперимент', publication: 'Источник', expert: 'Эксперт',
    location: 'Локация', organization: 'Организация', chunk: 'Фрагмент',
  };

  // ── State ──────────────────────────────────────────────────────
  let zoom = $state(1);
  let panX = $state(0);
  let panY = $state(0);
  let isDragging = $state(false);
  let dragStartX = $state(0);
  let dragStartY = $state(0);
  let dragStartPanX = $state(0);
  let dragStartPanY = $state(0);
  let expandedCommunities = $state<Set<string>>(new Set());
  let searchQuery = $state('');
  let searchVisible = $state(false);
  let hoveredNodeId = $state<string | null>(null);
  let showLegend = $state(true);
  let mode = $state<'select' | 'focus' | 'path'>('select');
  let focusNodeId = $state<string | null>(null);
  let pathStartId = $state<string | null>(null);
  let pathEndId = $state<string | null>(null);

  // ── Community detection ────────────────────────────────────────
  interface Community {
    type: string;
    label: string;
    color: string;
    nodes: GraphNode[];
    memberIds: Set<string>;
  }

  const communities = $derived.by<Community[]>(() => {
    // Пытаемся группировать по community из metadata, иначе по типу
    const hasCommunityMeta = graph.nodes.some((n) => n.metadata['community']);
    const groups = new Map<string, GraphNode[]>();
    if (hasCommunityMeta) {
      for (const node of graph.nodes) {
        const comm = String(node.metadata['community'] ?? node.type);
        const arr = groups.get(comm) ?? [];
        arr.push(node);
        groups.set(comm, arr);
      }
    } else {
      for (const node of graph.nodes) {
        const arr = groups.get(node.type) ?? [];
        arr.push(node);
        groups.set(node.type, arr);
      }
    }
    return Array.from(groups.entries()).map(([type, nodes]) => ({
      type,
      label: hasCommunityMeta ? type : (typeLabels[type] ?? type),
      color: colors[nodes[0].type] ?? '#64748b',
      nodes,
      memberIds: new Set(nodes.map((n) => n.id)),
    }));
  });

  function findCommunityType(nodeId: string): string {
    for (const c of communities) {
      if (c.memberIds.has(nodeId)) return c.type;
    }
    return 'unknown';
  }

  // ── Visible nodes ──────────────────────────────────────────────
  interface VNode {
    id: string;
    label: string;
    type: string;
    color: string;
    isCommunity: boolean;
    memberCount: number;
    origNode?: GraphNode;
  }

  const visibleNodes = $derived.by<VNode[]>(() => {
    // Focus mode: только выбранный узел и его соседи
    if (mode === 'focus' && focusNodeId) {
      const focusNode = graph.nodes.find((n) => n.id === focusNodeId);
      if (!focusNode) return [];
      const neighborIds = new Set<string>();
      for (const e of graph.edges) {
        if (e.source === focusNodeId) neighborIds.add(e.target);
        if (e.target === focusNodeId) neighborIds.add(e.source);
      }
      const result: VNode[] = [{
        id: focusNodeId, label: focusNode.label, type: focusNode.type,
        color: colors[focusNode.type] ?? '#64748b', isCommunity: false, memberCount: 1, origNode: focusNode,
      }];
      for (const nid of neighborIds) {
        const n = graph.nodes.find((x) => x.id === nid);
        if (n) result.push({
          id: n.id, label: n.label, type: n.type, color: colors[n.type] ?? '#64748b',
          isCommunity: false, memberCount: 1, origNode: n,
        });
      }
      return result.slice(0, 12);
    }

    // Обычный режим: mega-nodes для свёрнутых, индивидуальные для развёрнутых
    const result: VNode[] = [];
    for (const comm of communities) {
      if (expandedCommunities.has(comm.type)) {
        for (const node of comm.nodes) {
          result.push({
            id: node.id, label: node.label, type: node.type, color: comm.color,
            isCommunity: false, memberCount: 1, origNode: node,
          });
        }
      } else {
        result.push({
          id: `__c_${comm.type}`, label: comm.label, type: comm.type, color: comm.color,
          isCommunity: true, memberCount: comm.nodes.length,
        });
      }
    }
    return result;
  });

  const visibleNodeIds = $derived(new Set(visibleNodes.map((n) => n.id)));

  // ── Visible edges ──────────────────────────────────────────────
  interface VEdge {
    id: string; source: string; target: string; label: string;
    isInter: boolean; count: number;
  }

  const visibleEdges = $derived.by<VEdge[]>(() => {
    const map = new Map<string, VEdge>();
    for (const edge of graph.edges) {
      const sComm = findCommunityType(edge.source);
      const tComm = findCommunityType(edge.target);
      const sExp = expandedCommunities.has(sComm) || mode === 'focus';
      const tExp = expandedCommunities.has(tComm) || mode === 'focus';
      let sId: string, tId: string, isInter = false;
      if (sExp) sId = edge.source; else { sId = `__c_${sComm}`; isInter = true; }
      if (tExp) tId = edge.target; else { tId = `__c_${tComm}`; isInter = true; }
      if (sId === tId) continue;
      if (!visibleNodeIds.has(sId) || !visibleNodeIds.has(tId)) continue;
      const key = `${sId}->${tId}`;
      const ex = map.get(key);
      if (ex) ex.count++;
      else map.set(key, { id: key, source: sId, target: tId, label: isInter ? '' : edge.relation, isInter, count: 1 });
    }
    return Array.from(map.values());
  });

  // ── Radial layout (детерминированный, как в оригинале) ─────────
  const positions = $derived.by<Map<string, { x: number; y: number }>>(() => {
    const nodes = visibleNodes;
    if (nodes.length === 0) return new Map();
    const pos = new Map<string, { x: number; y: number }>();

    // Focus mode: центр + соседи по кругу
    if (mode === 'focus' && focusNodeId) {
      pos.set(focusNodeId, { x: cx, y: cy });
      const others = nodes.filter((n) => n.id !== focusNodeId);
      const r = 160;
      for (let i = 0; i < others.length; i++) {
        const a = (i / Math.max(others.length, 1)) * 2 * Math.PI - Math.PI / 2;
        pos.set(others[i].id, { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) });
      }
      return pos;
    }

    // Группировка видимых узлов по сообществам
    const groups: { type: string; isComm: boolean; items: VNode[] }[] = [];
    for (const comm of communities) {
      if (expandedCommunities.has(comm.type)) {
        groups.push({ type: comm.type, isComm: false, items: visibleNodes.filter((n) => !n.isCommunity && comm.memberIds.has(n.id)) });
      } else {
        const mn = visibleNodes.find((n) => n.id === `__c_${comm.type}`);
        if (mn) groups.push({ type: comm.type, isComm: true, items: [mn] });
      }
    }

    const angularStep = (2 * Math.PI) / Math.max(groups.length, 1);
    const baseRadius = 170;
    for (let gi = 0; gi < groups.length; gi++) {
      const baseAngle = gi * angularStep - Math.PI / 2;
      const gx = cx + baseRadius * Math.cos(baseAngle);
      const gy = cy + baseRadius * Math.sin(baseAngle);
      const items = groups[gi].items;
      if (groups[gi].isComm) {
        pos.set(items[0].id, { x: gx, y: gy });
      } else {
        for (let ni = 0; ni < items.length; ni++) {
          const ring = Math.floor(ni / 4);
          const inRing = ni % 4;
          const rr = ring === 0 ? 0 : 55 + ring * 45;
          const spread = ring === 0 ? 0 : angularStep * 0.32;
          const a = ring === 0 ? baseAngle : baseAngle + (inRing - 1.5) * spread;
          pos.set(items[ni].id, { x: gx + rr * Math.cos(a), y: gy + rr * Math.sin(a) });
        }
      }
    }
    return pos;
  });

  // ── Path finding (BFS) ─────────────────────────────────────────
  const pathEdges = $derived.by<Set<string>>(() => {
    if (mode !== 'path' || !pathStartId || !pathEndId) return new Set();
    const adj = new Map<string, string[]>();
    for (const e of graph.edges) {
      const s = adj.get(e.source) ?? []; s.push(e.target); adj.set(e.source, s);
      const t = adj.get(e.target) ?? []; t.push(e.source); adj.set(e.target, t);
    }
    const visited = new Set<string>([pathStartId]);
    const parent = new Map<string, string>();
    const queue = [pathStartId];
    while (queue.length > 0) {
      const cur = queue.shift()!;
      if (cur === pathEndId) break;
      for (const next of adj.get(cur) ?? []) {
        if (!visited.has(next)) { visited.add(next); parent.set(next, cur); queue.push(next); }
      }
    }
    const result = new Set<string>();
    let cur: string | undefined = pathEndId;
    while (cur && cur !== pathStartId) {
      const prev = parent.get(cur);
      if (!prev) break;
      for (const e of visibleEdges) {
        if ((e.source === prev && e.target === cur) || (e.source === cur && e.target === prev)) {
          result.add(e.id); break;
        }
      }
      cur = prev;
    }
    return result;
  });

  // ── Search ──────────────────────────────────────────────────────
  const searchResults = $derived.by<GraphNode[]>(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return [];
    return graph.nodes.filter((n) => {
      if (n.label.toLowerCase().includes(q)) return true;
      if (n.type.toLowerCase().includes(q)) return true;
      if (typeLabels[n.type]?.toLowerCase().includes(q)) return true;
      for (const v of Object.values(n.metadata)) { if (String(v).toLowerCase().includes(q)) return true; }
      return false;
    }).slice(0, 20);
  });
  const matchingIds = $derived(new Set(searchResults.map((n) => n.id)));
  const matchingCommTypes = $derived(new Set(searchResults.map((n) => findCommunityType(n.id))));

  // ── Highlighting ───────────────────────────────────────────────
  const hlId = $derived(hoveredNodeId ?? selectedId ?? null);
  const neighborIds = $derived.by<Set<string>>(() => {
    if (!hlId) return new Set();
    const ids = new Set<string>([hlId]);
    for (const e of visibleEdges) {
      if (e.source === hlId) ids.add(e.target);
      if (e.target === hlId) ids.add(e.source);
    }
    return ids;
  });

  const pathNodeIds = $derived.by<Set<string>>(() => {
    if (mode !== 'path' || !pathStartId || !pathEndId) return new Set();
    const adj = new Map<string, string[]>();
    for (const e of graph.edges) {
      const s = adj.get(e.source) ?? []; s.push(e.target); adj.set(e.source, s);
      const t = adj.get(e.target) ?? []; t.push(e.source); adj.set(e.target, t);
    }
    const visited = new Set<string>([pathStartId]);
    const parent = new Map<string, string>();
    const queue = [pathStartId];
    while (queue.length > 0) {
      const cur = queue.shift()!;
      if (cur === pathEndId) break;
      for (const next of adj.get(cur) ?? []) {
        if (!visited.has(next)) { visited.add(next); parent.set(next, cur); queue.push(next); }
      }
    }
    const result = new Set<string>();
    let cur: string | undefined = pathEndId;
    while (cur) { result.add(cur); if (cur === pathStartId) break; cur = parent.get(cur); }
    return result;
  });

  function nodeOpacity(id: string): number {
    if (searchQuery.trim()) {
      if (id.startsWith('__c_')) return matchingCommTypes.has(id.replace('__c_', '')) ? 1 : 0.15;
      return matchingIds.has(id) ? 1 : 0.12;
    }
    if (mode === 'path' && pathNodeIds.size > 0) {
      return pathNodeIds.has(id) ? 1 : 0.15;
    }
    if (hlId) return (id === hlId || neighborIds.has(id)) ? 1 : 0.22;
    return 1;
  }
  function edgeOpacity(e: VEdge): number {
    if (searchQuery.trim()) return 0.08;
    if (pathEdges.has(e.id)) return 1;
    if (mode === 'path' && pathEdges.size > 0) return 0.1;
    if (hlId && (e.source === hlId || e.target === hlId)) return 1;
    if (hlId) return 0.12;
    return e.isInter ? 0.35 : 0.5;
  }
  function edgeWidth(e: VEdge): number {
    if (pathEdges.has(e.id)) return 3;
    if (e.isInter) return Math.min(4, 1.5 + e.count * 0.4);
    return 1.5;
  }
  function nodeRadius(vn: VNode): number {
    if (vn.isCommunity) return Math.min(42, 22 + vn.memberCount * 1.8);
    return 28;
  }

  // ── Pan & Zoom ─────────────────────────────────────────────────
  function handleWheel(e: WheelEvent) { e.preventDefault(); zoom = Math.max(0.5, Math.min(3, zoom * (e.deltaY > 0 ? 0.9 : 1.1))); }
  function zoomIn() { zoom = Math.min(3, zoom * 1.2); }
  function zoomOut() { zoom = Math.max(0.5, zoom * 0.83); }
  function handleMouseDown(e: MouseEvent) {
    if (e.button !== 0) return;
    if ((e.target as HTMLElement).closest('.graph-node, .gc-search, .gc-tool, .legend-item, .gc-detail, .gc-toolbar')) return;
    isDragging = true; dragStartX = e.clientX; dragStartY = e.clientY; dragStartPanX = panX; dragStartPanY = panY;
  }
  function handleMouseMove(e: MouseEvent) { if (isDragging) { panX = dragStartPanX + (e.clientX - dragStartX); panY = dragStartPanY + (e.clientY - dragStartY); } }
  function handleMouseUp() { isDragging = false; }
  function resetView() { zoom = 1; panX = 0; panY = 0; }

  // ── Node interaction ───────────────────────────────────────────
  function handleNodeClick(vn: VNode) {
    if (mode === 'path') {
      if (vn.isCommunity) {
        const next = new Set(expandedCommunities); next.add(vn.type); expandedCommunities = next;
        return;
      }
      if (!pathStartId || (pathStartId && pathEndId)) { pathStartId = vn.id; pathEndId = null; }
      else if (vn.id !== pathStartId) { pathEndId = vn.id; }
      return;
    }
    if (vn.isCommunity) {
      const next = new Set(expandedCommunities); next.add(vn.type); expandedCommunities = next;
    } else if (vn.origNode) {
      onselect?.(vn.origNode);
    }
  }
  function handleNodeDblClick(vn: VNode) {
    if (vn.isCommunity) { handleNodeClick(vn); return; }
    if (!vn.origNode) return;
    // Фокус-режим по двойному клику
    focusNodeId = vn.id;
    mode = 'focus';
    onselect?.(vn.origNode);
  }
  function collapseCommunity(type: string) {
    const next = new Set(expandedCommunities); next.delete(type); expandedCommunities = next;
    if (mode === 'focus' && focusNodeId) {
      const ft = findCommunityType(focusNodeId);
      if (ft === type) { mode = 'select'; focusNodeId = null; }
    }
  }
  function expandAll() { expandedCommunities = new Set(communities.map((c) => c.type)); }
  function collapseAll() { expandedCommunities = new Set(); mode = 'select'; focusNodeId = null; }
  function exitFocus() { mode = 'select'; focusNodeId = null; }

  function expandNeighbors() {
    if (!selectedId) return;
    const types = new Set<string>();
    for (const e of graph.edges) {
      if (e.source === selectedId) types.add(findCommunityType(e.target));
      if (e.target === selectedId) types.add(findCommunityType(e.source));
    }
    if (types.size > 0) {
      const next = new Set(expandedCommunities);
      for (const t of types) next.add(t);
      expandedCommunities = next;
    }
  }

  function selectSearchResult(node: GraphNode) {
    const ct = findCommunityType(node.id);
    if (!expandedCommunities.has(ct) && mode !== 'focus') {
      const next = new Set(expandedCommunities); next.add(ct); expandedCommunities = next;
    }
    onselect?.(node);
    searchQuery = '';
    searchVisible = false;
    // Центрируем на узле
    const pos = positions.get(node.id);
    if (pos) { panX = cx - pos.x * zoom; panY = cy - pos.y * zoom; }
  }

  function truncate(t: string, max: number): string { return t.length > max ? t.slice(0, max - 1) + '…' : t; }

  // ── Selected node detail ───────────────────────────────────────
  const selectedNode = $derived(graph.nodes.find((n) => n.id === selectedId) ?? null);
  const selectedNodeEdges = $derived.by<{ edge: GraphEdge; other: GraphNode }[]>(() => {
    if (!selectedNode) return [];
    const nm = new Map(graph.nodes.map((n) => [n.id, n]));
    const res: { edge: GraphEdge; other: GraphNode }[] = [];
    for (const e of graph.edges) {
      if (e.source === selectedNode.id) { const o = nm.get(e.target); if (o) res.push({ edge: e, other: o }); }
      else if (e.target === selectedNode.id) { const o = nm.get(e.source); if (o) res.push({ edge: e, other: o }); }
    }
    return res.slice(0, 15);
  });

  // ── Keyboard ───────────────────────────────────────────────────
  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      if (mode === 'focus') exitFocus();
      else if (mode === 'path') { mode = 'select'; pathStartId = null; pathEndId = null; }
      else onselect?.(null);
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<div
  class="graph-canvas"
  onwheel={handleWheel}
  onmousedown={handleMouseDown}
  onmousemove={handleMouseMove}
  onmouseup={handleMouseUp}
  onmouseleave={handleMouseUp}
  role="application"
  aria-label="Граф связей"
>
  <!-- ── Toolbar ────────────────────────────────────────────────── -->
  <div class="gc-toolbar">
    <div class="gc-toolbar__mode">
      <button class="gc-tool" class:active={mode === 'select'} onclick={() => mode = 'select'} title="Выбор" aria-label="Выбор">
        <MousePointer2 size={16} />
      </button>
      <button class="gc-tool" class:active={searchVisible} onclick={() => searchVisible = !searchVisible} title="Поиск" aria-label="Поиск">
        <Search size={16} />
      </button>
      <button class="gc-tool" class:active={mode === 'focus'} onclick={() => { if (selectedId) { focusNodeId = selectedId; mode = 'focus'; } }} title="Фокус" aria-label="Фокус" disabled={!selectedId}>
        <Focus size={16} />
      </button>
      <button class="gc-tool" class:active={mode === 'path'} onclick={() => { mode = 'path'; pathStartId = null; pathEndId = null; }} title="Путь" aria-label="Путь">
        <GitBranch size={16} />
      </button>
    </div>

    {#if searchVisible}
      <div class="gc-search">
        <Search size={15} />
        <input type="text" placeholder="Поиск по графу..." bind:value={searchQuery} aria-label="Поиск узлов" />
        {#if searchQuery}<span class="gc-search-count">Найдено: {searchResults.length}</span>{/if}
        {#if searchQuery}
          <button class="gc-search-clear" onclick={() => searchQuery = ''} aria-label="Очистить"><X size={14} /></button>
        {/if}
        {#if searchQuery && searchResults.length > 0}
          <div class="gc-search-dropdown">
            {#each searchResults as node}
              <button class="gc-search-result" onclick={() => selectSearchResult(node)}>
                <i style={`background:${colors[node.type] ?? '#64748b'}`}></i>
                <span>{truncate(node.label, 38)}</span>
                <small>{typeLabels[node.type] ?? node.type}</small>
              </button>
            {/each}
          </div>
        {/if}
      </div>
    {/if}

    <div class="gc-toolbar__right">
      <span class="gc-stats"><strong>{visibleNodes.filter((n) => !n.isCommunity).length}</strong> узл.</span>
      <button class="gc-tool" onclick={expandAll} title="Развернуть все" aria-label="Развернуть все"><Eye size={16} /></button>
      <button class="gc-tool" onclick={collapseAll} title="Свернуть все" aria-label="Свернуть все"><EyeOff size={16} /></button>
      <button class="gc-tool" onclick={zoomIn} title="+" aria-label="Приблизить"><ZoomIn size={16} /></button>
      <button class="gc-tool" onclick={zoomOut} title="−" aria-label="Отдалить"><ZoomOut size={16} /></button>
      <button class="gc-tool" onclick={resetView} title="Сброс" aria-label="Сбросить вид"><Maximize size={16} /></button>
    </div>
  </div>

  {#if graph.nodes.length === 0}
    <div class="gc-empty">
      <Network size={40} />
      <p>Выполните запрос для отображения графа связей</p>
    </div>
  {:else}
    <!-- ── SVG canvas ─────────────────────────────────────────────── -->
    <svg class="graph-canvas__svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" aria-hidden="true">
      <defs>
        <marker id="gc-arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
          <polygon points="0 0, 8 3, 0 6" fill="#94a3a0" />
        </marker>
        <marker id="gc-arrow-hl" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
          <polygon points="0 0, 8 3, 0 6" fill="#b84a22" />
        </marker>
        <marker id="gc-arrow-path" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
          <polygon points="0 0, 8 3, 0 6" fill="#2563eb" />
        </marker>
      </defs>
      <g transform={`translate(${panX} ${panY}) scale(${zoom})`}>
        {#each visibleEdges as edge}
          {@const from = positions.get(edge.source)}
          {@const to = positions.get(edge.target)}
          {#if from && to}
            {@const mx = (from.x + to.x) / 2}
            {@const my = (from.y + to.y) / 2}
            {@const isHl = hlId && (edge.source === hlId || edge.target === hlId)}
            {@const isPath = pathEdges.has(edge.id)}
            <line
              x1={from.x} y1={from.y} x2={to.x} y2={to.y}
              stroke={isPath ? '#2563eb' : isHl ? '#b84a22' : '#94a3a0'}
              stroke-width={edgeWidth(edge)}
              stroke-dasharray={edge.isInter ? '6 3' : undefined}
              opacity={edgeOpacity(edge)}
              marker-end={isPath ? 'url(#gc-arrow-path)' : isHl ? 'url(#gc-arrow-hl)' : 'url(#gc-arrow)'}
            />
            {#if !edge.isInter && edge.label}
              <text x={mx} y={my} class="graph-edge-label" text-anchor="middle" dy="-4" opacity={edgeOpacity(edge)}>
                {truncate(edge.label, 15)}
              </text>
            {/if}
            {#if edge.isInter && edge.count > 1}
              <text x={mx} y={my} class="gc-edge-count" text-anchor="middle" dy="4" opacity={edgeOpacity(edge)}>{edge.count}</text>
            {/if}
          {/if}
        {/each}
        {#each visibleNodes as vn}
          {@const pos = positions.get(vn.id)}
          {#if pos}
            {@const r = nodeRadius(vn)}
            {@const isSel = selectedId === vn.id}
            {@const isHover = hoveredNodeId === vn.id}
            {@const isPathNode = mode === 'path' && (vn.id === pathStartId || vn.id === pathEndId)}
            <g
              class="graph-node"
              class:selected={isSel}
              class:community={vn.isCommunity}
              class:hovered={isHover}
              class:path-end={isPathNode}
              style={`--nc:${vn.color};opacity:${nodeOpacity(vn.id)}`}
              transform={`translate(${pos.x} ${pos.y})`}
              onclick={() => handleNodeClick(vn)}
              ondblclick={() => handleNodeDblClick(vn)}
              onmouseenter={() => hoveredNodeId = vn.id}
              onmouseleave={() => hoveredNodeId = null}
              role="button" tabindex="0"
              aria-label={vn.isCommunity ? `${vn.label}: ${vn.memberCount} узлов` : vn.label}
              onkeydown={(e) => { if (e.key === 'Enter') handleNodeClick(vn); }}
            >
              {#if vn.isCommunity}
                <circle r={r} fill={vn.color} fill-opacity="0.12" stroke={vn.color} stroke-width="2.5" />
                <text class="gc-comm-label" y="-3" text-anchor="middle">{truncate(vn.label, 14)}</text>
                <text class="gc-comm-count" y="13" text-anchor="middle">{vn.memberCount}</text>
              {:else}
                <circle r={r} fill={vn.color} fill-opacity="0.12" stroke={vn.color} stroke-width="2.5" />
                {#if isSel || isPathNode}
                  <circle r={r + 4} fill="none" stroke={vn.color} stroke-width="2" opacity="0.4" />
                {/if}
                <text class="graph-node-label" y={r + 14} text-anchor="middle">{truncate(vn.label, 20)}</text>
                <text class="graph-node-type" y={-r - 8} text-anchor="middle">{typeLabels[vn.type] ?? vn.type}</text>
              {/if}
              <title>{vn.isCommunity ? `${vn.label} (${vn.memberCount} узлов). Кликните для разворачивания.` : `${typeLabels[vn.type] ?? vn.type}: ${vn.label}`}</title>
            </g>
          {/if}
        {/each}
      </g>
    </svg>

    <!-- ── Legend ───────────────────────────────────────────────── -->
    {#if showLegend && communities.length > 1}
      <div class="graph-canvas__legend">
        {#each communities as comm}
          <button
            class="legend-item"
            class:expanded={expandedCommunities.has(comm.type)}
            onclick={() => expandedCommunities.has(comm.type) ? collapseCommunity(comm.type) : handleNodeClick({ id: `__c_${comm.type}`, label: comm.label, type: comm.type, color: comm.color, isCommunity: true, memberCount: comm.nodes.length })}
          >
            <i style={`background:${comm.color}`}></i>
            <span>{comm.label}</span><small>{comm.nodes.length}</small>
            {#if expandedCommunities.has(comm.type)}<ChevronDown size={11} class="rotated" />{/if}
          </button>
        {/each}
      </div>
    {/if}

    <!-- ── Hints ────────────────────────────────────────────────── -->
    {#if mode === 'path'}
      <div class="gc-hint gc-hint--path">
        {#if !pathStartId}Выберите начальный узел
        {:else if !pathEndId}Выберите конечный узел для построения пути
        {:else if pathNodeIds.size > 1}Путь построен: {pathNodeIds.size} узлов
        {:else}Путь не найден между выбранными узлами{/if}
        <button class="gc-hint__btn" onclick={() => { mode = 'select'; pathStartId = null; pathEndId = null; }}>Выход</button>
      </div>
    {:else if mode === 'focus'}
      <div class="gc-hint gc-hint--focus">
        Фокус на узле · ESC для выхода
        <button class="gc-hint__btn" onclick={exitFocus}>Выход</button>
      </div>
    {:else if !searchQuery && !hlId}
      <div class="gc-hint">Клик — развернуть группу · Двойной клик — фокус на узле</div>
    {/if}

    <!-- ── Inspector panel ──────────────────────────────────────── -->
    {#if selectedNode}
      <div class="gc-detail">
        <button class="gc-detail__close" onclick={() => onselect?.(null)} aria-label="Закрыть"><X size={16} /></button>
        <div class="gc-detail__header">
          <span class="gc-detail__badge" style={`background:${colors[selectedNode.type] ?? '#64748b'}`}></span>
          <div>
            <strong>{selectedNode.label}</strong>
            <span>{typeLabels[selectedNode.type] ?? selectedNode.type}</span>
          </div>
        </div>
        <div class="gc-detail__conf">
          <span>Уверенность</span>
          <div class="gc-detail__bar"><div style={`width:${Math.round(selectedNode.confidence * 100)}%;background:${selectedNode.confidence >= 0.7 ? '#059669' : selectedNode.confidence >= 0.4 ? '#d97706' : '#dc2626'}`}></div></div>
          <strong>{Math.round(selectedNode.confidence * 100)}%</strong>
        </div>
        {#if Object.keys(selectedNode.metadata).length > 0}
          <dl class="gc-detail__meta">
            {#each Object.entries(selectedNode.metadata) as [k, v]}
              <div><dt>{k}</dt><dd>{String(v)}</dd></div>
            {/each}
          </dl>
        {/if}
        {#if selectedNodeEdges.length}
          <div class="gc-detail__edges">
            <small>Связи ({selectedNodeEdges.length})</small>
            {#each selectedNodeEdges as { edge, other }}
              <button class="gc-detail__edge" onclick={() => onselect?.(other)}>
                <span class="gc-detail__rel">{truncate(edge.relation, 15)}</span>
                <span class="gc-detail__target" style={`color:${colors[other.type] ?? '#64748b'}`}>→ {truncate(other.label, 22)}</span>
              </button>
            {/each}
          </div>
        {/if}
        <div class="gc-detail__actions">
          <button onclick={expandNeighbors} disabled={!selectedId}><Network size={14} /> Соседи</button>
          <button onclick={() => { focusNodeId = selectedNode.id; mode = 'focus'; }}><Focus size={14} /> Фокус</button>
          {#if onAskNode}<button onclick={() => onAskNode?.(selectedNode)}><Lightbulb size={14} /> Спросить</button>{/if}
        </div>
      </div>
    {/if}
  {/if}
</div>
