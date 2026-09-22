<script module lang="ts">
  // Словарь карты — единственный на компонент и на страницу-инспектор:
  // маршрут импортирует translate-функции отсюда и не заводит своё зеркало.
  // Переводы связей и классов данных берутся из общих словарей $lib/terms и
  // $lib/types; связи, которых в terms нет, читаются фолбэком «связь» рядом
  // с техническим кодом ребра.
  import { RELATION_LABELS } from './terms';
  import { DATA_CLASS_LABELS, type DataClass, type GraphNode } from './types';

  export const TYPE_ORDER: string[] = [
    'material', 'process', 'equipment', 'condition', 'claim', 'experiment',
    'publication', 'expert', 'location', 'organization', 'chunk',
  ];

  export const TYPE_LABELS: Record<string, string> = {
    material: 'Материал', process: 'Метод', equipment: 'Оборудование',
    condition: 'Условие', claim: 'Вывод', experiment: 'Эксперимент',
    publication: 'Источник', expert: 'Эксперт', location: 'Локация',
    organization: 'Организация', chunk: 'Фрагмент',
  };

  // Класс данных узла приходит из контракта GraphNode (`data_class`): он метит
  // права, а не степень консенсуса источников; русские имена — DATA_CLASS_LABELS
  // из $lib/types.
  // Легенда классов данных читается теми же StatusPill, что и находки:
  // публичный согласуется, внутренний — гипотеза, закрытый — оспаривается.
  export const CLASS_PILL: Record<string, 'consensus' | 'hypothesis' | 'disputed'> = {
    public: 'consensus', internal: 'hypothesis', restricted: 'disputed',
  };

  export function typeLabel(type: string): string {
    return TYPE_LABELS[type] ?? type;
  }

  export function relationLabel(relation: string): string {
    return RELATION_LABELS[relation.toUpperCase()] ?? 'связь';
  }

  export function classOf(node: GraphNode): { code: string; ru: string; pill: 'consensus' | 'hypothesis' | 'disputed' } {
    const code = node.data_class;
    return { code, ru: DATA_CLASS_LABELS[code] ?? code, pill: CLASS_PILL[code] ?? 'consensus' };
  }

  // Тип узла задаёт тон диска; группа тонов — коралл / шалфей / лаванда.
  export function discTone(type: string): 'coral' | 'sage' | 'lav' | 'stone' {
    if (type === 'claim' || type === 'experiment' || type === 'condition') return 'coral';
    if (type === 'material' || type === 'process' || type === 'equipment') return 'sage';
    if (type === 'publication' || type === 'chunk' || type === 'expert') return 'lav';
    return 'stone';
  }
</script>

<script lang="ts">
  // Карта связей в мире Softly: узлы — мягкие диски на пастельном поле, связи —
  // каменные штрихи 1.5px со скруглёнными концами. Раскладка детерминирована:
  // типы идут полосами по онтологии, внутри полосы — по алфавиту метки, число
  // колонок задано шириной поля, поэтому порядок обхода Tab совпадает с порядком
  // чтения текстового списка.
  //
  // Поле скроллится как обычный блок (pan), масштаб — кнопки и +/−/0. Колесо
  // мыши не перехватывается: страница скроллится обычно.
  //
  // Клавиатура: Tab — по узлам, стрелки — пространственный переход к ближайшему
  // диску в направлении, Enter/Space — выбрать, Esc — снять выбор.
  // Картография не единственный способ прочтения: под полем лежат текстовые
  // списки узлов и связей, а выбор объявляется через aria-live.
  import type { GraphEdge, GraphSnapshot } from './types';
  import { countOf } from './format';
  import Button from './ui/Button.svelte';
  import Chip from './ui/Chip.svelte';
  import Empty from './ui/Empty.svelte';
  import Icon from './ui/Icon.svelte';
  import Panel from './ui/Panel.svelte';
  import StatusPill from './ui/StatusPill.svelte';

  interface Props {
    graph: GraphSnapshot;
    selectedId?: string;
    onselect?: (node: GraphNode | null) => void;
  }

  let { graph, selectedId, onselect }: Props = $props();

  // ── Геометрия раскладки (world-координаты, px до масштабирования) ───────
  const CELL_W = 176;
  const CELL_H = 100;
  const CAP_H = 36;
  const NODE_BOX = 148;
  const GROUP_GAP = 28;
  const PAD = 40;
  const MAP_PAGE = 64;
  const LIST_PAGE = 20;
  const ZOOMS = [0.45, 0.6, 0.72, 0.85, 1, 1.2, 1.45, 1.75];

  const nf = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 });
  const fmt = (v: number): string => nf.format(v);

  // ── Состояние поля ──────────────────────────────────────────────────────
  let query = $state('');
  let hiddenTypes = $state<Set<string>>(new Set());
  let mapCap = $state(MAP_PAGE);
  let listCap = $state(LIST_PAGE);
  let edgeCap = $state(LIST_PAGE);
  let zoom = $state(1);
  let focusedId = $state<string | null>(null);
  let announcement = $state('');
  let fieldEl = $state<HTMLDivElement | null>(null);
  let fieldW = $state(0);

  // ── Разбор среза ────────────────────────────────────────────────────────
  function communityOf(node: GraphNode): string {
    const raw = node.metadata['community'];
    return typeof raw === 'string' ? raw : '';
  }

  function matches(node: GraphNode, q: string): boolean {
    if (!q) return true;
    return (
      node.label.toLowerCase().includes(q) ||
      node.id.toLowerCase().includes(q) ||
      node.type.toLowerCase().includes(q) ||
      typeLabel(node.type).toLowerCase().includes(q) ||
      communityOf(node).toLowerCase().includes(q)
    );
  }

  const nodeById = $derived(new Map(graph.nodes.map((n) => [n.id, n])));

  const degree = $derived.by<Map<string, number>>(() => {
    const map = new Map<string, number>();
    for (const edge of graph.edges) {
      map.set(edge.source, (map.get(edge.source) ?? 0) + 1);
      map.set(edge.target, (map.get(edge.target) ?? 0) + 1);
    }
    return map;
  });

  const orderedTypes = $derived.by<string[]>(() => {
    const seen = new Set(graph.nodes.map((n) => n.type));
    return [...seen].sort((a, b) => {
      const ia = TYPE_ORDER.indexOf(a);
      const ib = TYPE_ORDER.indexOf(b);
      if (ia !== ib) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
      return a.localeCompare(b, 'ru');
    });
  });

  /** Кластеры: тип → отобранные узлы, отсортированные по метке. */
  const groups = $derived.by<{ type: string; nodes: GraphNode[] }[]>(() => {
    const q = query.trim().toLowerCase();
    const buckets = new Map<string, GraphNode[]>();
    for (const node of graph.nodes) {
      if (hiddenTypes.has(node.type) || !matches(node, q)) continue;
      const bucket = buckets.get(node.type);
      if (bucket) bucket.push(node);
      else buckets.set(node.type, [node]);
    }
    for (const list of buckets.values()) list.sort((a, b) => a.label.localeCompare(b.label, 'ru'));
    return orderedTypes
      .filter((type) => buckets.has(type))
      .map((type) => ({ type, nodes: buckets.get(type) ?? [] }));
  });

  const filtered = $derived(groups.reduce((sum, group) => sum + group.nodes.length, 0));

  const legend = $derived.by<{ type: string; label: string; tone: string; count: number; on: boolean }[]>(() =>
    orderedTypes
      .filter((type) => graph.nodes.some((n) => n.type === type))
      .map((type) => ({
        type,
        label: typeLabel(type),
        tone: discTone(type),
        count: graph.nodes.filter((n) => n.type === type && matches(n, query.trim().toLowerCase())).length,
        on: !hiddenTypes.has(type),
      })),
  );

  const classKey = $derived.by<{ code: string; ru: string; pill: 'consensus' | 'hypothesis' | 'disputed'; count: number }[]>(() => {
    const counts = new Map<string, number>();
    for (const node of graph.nodes) counts.set(node.data_class, (counts.get(node.data_class) ?? 0) + 1);
    const order = ['public', 'internal', 'restricted'];
    const known = order.filter((code) => counts.has(code));
    const extra = [...counts.keys()].filter((code) => !order.includes(code)).sort();
    return [...known, ...extra].map((code) => ({
      code,
      ru: DATA_CLASS_LABELS[code as DataClass] ?? code,
      pill: CLASS_PILL[code] ?? 'consensus',
      count: counts.get(code) ?? 0,
    }));
  });

  interface Placed {
    node: GraphNode;
    cx: number;
    cy: number;
    r: number;
    links: number;
  }

  interface Block {
    type: string;
    label: string;
    total: number;
    shown: number;
    x: number;
    y: number;
    w: number;
    h: number;
    nodes: Placed[];
  }

  function radiusOf(node: GraphNode, links: number): number {
    const base = 12 + 10 * Math.max(0, Math.min(1, node.confidence));
    return Math.min(30, base + Math.min(9, Math.sqrt(links) * 2.6));
  }

  /**
   * Лента кластеров: тип → полоса с подписью, внутри — строки по `perRow`
   * дисков. Ширина поля диктует число колонок, поэтому при 100 % карта всегда
   * по ширине, а вертикаль читается скроллом поля.
   */
  const perRow = $derived(fieldW > 0 ? Math.max(3, Math.min(7, Math.floor((fieldW - PAD * 2) / CELL_W))) : 6);

  const blocks = $derived.by<Block[]>(() => {
    const out: Block[] = [];
    let y = PAD;
    let used = 0;

    for (const group of groups) {
      if (used >= mapCap) break;
      const take = group.nodes.slice(0, mapCap - used);
      used += take.length;
      const cols = Math.min(perRow, take.length);
      const rows = Math.ceil(take.length / cols);
      const w = cols * CELL_W;
      const h = CAP_H + rows * CELL_H;
      const nodes: Placed[] = take.map((node, index) => {
        const col = index % cols;
        const row = Math.floor(index / cols);
        const links = degree.get(node.id) ?? 0;
        return {
          node,
          cx: PAD + (col + 0.5) * CELL_W,
          cy: y + CAP_H + (row + 0.5) * CELL_H,
          r: radiusOf(node, links),
          links,
        };
      });
      out.push({ type: group.type, label: typeLabel(group.type), total: group.nodes.length, shown: take.length, x: PAD, y, w, h, nodes });
      y += h + GROUP_GAP;
    }
    return out;
  });

  const extent = $derived.by<{ w: number; h: number }>(() => {
    const last = blocks[blocks.length - 1];
    return {
      w: PAD * 2 + perRow * CELL_W,
      h: last ? last.y + last.h + PAD : 340,
    };
  });

  const placed = $derived<Map<string, Placed>>(
    new Map(blocks.flatMap((block) => block.nodes.map((item) => [item.node.id, item]))),
  );
  const placedCount = $derived(placed.size);

  const drawnEdges = $derived.by<GraphEdge[]>(() =>
    graph.edges.filter((edge) => placed.has(edge.source) && placed.has(edge.target)),
  );
  const offFieldEdges = $derived(graph.edges.length - drawnEdges.length);

  const selectedNode = $derived(graph.nodes.find((n) => n.id === selectedId) ?? null);

  const incident = $derived.by<Set<string>>(() => {
    const ids = new Set<string>();
    if (!selectedNode) return ids;
    for (const edge of graph.edges) {
      if (edge.source === selectedNode.id || edge.target === selectedNode.id) ids.add(edge.id);
    }
    return ids;
  });

  const neighbourIds = $derived.by<Set<string>>(() => {
    const ids = new Set<string>();
    if (!selectedNode) return ids;
    for (const edge of graph.edges) {
      if (edge.source === selectedNode.id) ids.add(edge.target);
      else if (edge.target === selectedNode.id) ids.add(edge.source);
    }
    return ids;
  });

  // ── Текстовое чтение карты: узлы и связи списком ────────────────────────
  const nodeList = $derived(groups.flatMap((group) => group.nodes).slice(0, listCap));
  const listFilteredIds = $derived(new Set(groups.flatMap((group) => group.nodes.map((n) => n.id))));

  const listedEdges = $derived.by<GraphEdge[]>(() =>
    graph.edges.filter((edge) => listFilteredIds.has(edge.source) && listFilteredIds.has(edge.target)),
  );

  function labelOf(id: string): string {
    return nodeById.get(id)?.label ?? '';
  }

  // ── Геометрия штрихов ───────────────────────────────────────────────────
  interface Point { x: number; y: number }

  function anchors(a: Placed, b: Placed): { p0: Point; c: Point; p1: Point } {
    const dx = b.cx - a.cx;
    const dy = b.cy - a.cy;
    const len = Math.hypot(dx, dy) || 1;
    const ux = dx / len;
    const uy = dy / len;
    const p0 = { x: a.cx + ux * (a.r + 4), y: a.cy + uy * (a.r + 4) };
    const p1 = { x: b.cx - ux * (b.r + 6), y: b.cy - uy * (b.r + 6) };
    const bend = Math.min(46, Math.hypot(p1.x - p0.x, p1.y - p0.y) * 0.15);
    return { p0, p1, c: { x: (p0.x + p1.x) / 2 - uy * bend, y: (p0.y + p1.y) / 2 + ux * bend } };
  }

  function edgePath(a: Placed, b: Placed): string {
    const { p0, c, p1 } = anchors(a, b);
    return `M ${p0.x.toFixed(1)} ${p0.y.toFixed(1)} Q ${c.x.toFixed(1)} ${c.y.toFixed(1)} ${p1.x.toFixed(1)} ${p1.y.toFixed(1)}`;
  }

  // Точка на штрихе ближе к цели: направление связи читается без подписи ребра.
  function edgeMark(a: Placed, b: Placed): Point {
    const { p0, c, p1 } = anchors(a, b);
    const t = 0.7;
    const m = 1 - t;
    return {
      x: m * m * p0.x + 2 * m * t * c.x + t * t * p1.x,
      y: m * m * p0.y + 2 * m * t * c.y + t * t * p1.y,
    };
  }

  // ── Выбор, фильтрация, масштаб ──────────────────────────────────────────
  // fromField — выбор начат на самом поле (клик или клавиатура по диску):
  // только он переносит фокус в узел; выбор из текстового списка и инспектора
  // фокус не отнимает — узел им показывает эффект скроллом поля.
  function select(node: GraphNode | null, fromField = false): void {
    onselect?.(node);
    if (node) {
      if (fromField) focusNode(node.id);
      const links = degree.get(node.id) ?? 0;
      announcement =
        `Выбран узел «${node.label}», ${typeLabel(node.type)}, уверенность ${fmt(node.confidence)}. ` +
        `Связей в текущем срезе: ${countOf(links, 'связь', 'связи', 'связей')}. ` +
        'Доказательства узла открыты под полем карты.';
    } else {
      announcement = 'Выбор узла снят.';
    }
  }

  function nodeElement(id: string): HTMLElement | null {
    const escaped = id.replace(/["\\]/g, '\\$&');
    return fieldEl?.querySelector<HTMLElement>(`[data-node-id="${escaped}"]`) ?? null;
  }

  function focusNode(id: string): void {
    nodeElement(id)?.focus();
  }

  /** Стрелки уводят к ближайшему узлу в направлении: проекция + штраф за снос. */
  function moveFocus(from: Placed, dx: number, dy: number): boolean {
    let best: Placed | null = null;
    let bestScore = Number.POSITIVE_INFINITY;
    for (const item of placed.values()) {
      if (item.node.id === from.node.id) continue;
      const vx = item.cx - from.cx;
      const vy = item.cy - from.cy;
      const along = vx * dx + vy * dy;
      if (along <= CELL_W * 0.18) continue;
      const across = Math.abs(vx * dy - vy * dx);
      const score = along + across * 2.2;
      if (score < bestScore) {
        bestScore = score;
        best = item;
      }
    }
    if (!best) return false;
    focusNode(best.node.id);
    return true;
  }

  function onNodeKeydown(event: KeyboardEvent, current: Placed): void {
    if (event.key === '+' || event.key === '=') {
      event.preventDefault();
      stepZoom(1);
      return;
    }
    if (event.key === '-') {
      event.preventDefault();
      stepZoom(-1);
      return;
    }
    if (event.key === '0') {
      event.preventDefault();
      zoom = 1;
      return;
    }

    if (event.key === 'Home' || event.key === 'End') {
      const list = blocks.flatMap((block) => block.nodes);
      const target = event.key === 'Home' ? list[0] : list[list.length - 1];
      if (target) {
        event.preventDefault();
        focusNode(target.node.id);
      }
      return;
    }

    const moves: Record<string, [number, number]> = {
      ArrowRight: [1, 0],
      ArrowLeft: [-1, 0],
      ArrowDown: [0, 1],
      ArrowUp: [0, -1],
    };
    const move = moves[event.key];
    if (!move) return;
    // Стрелки ведут к ближайшему диску в направлении: проекция на ось плюс штраф
    // за снос вбок. Tab при этом остаётся штатным обходом по порядку раскладки.
    if (moveFocus(current, move[0], move[1])) event.preventDefault();
  }

  function stepZoom(direction: 1 | -1): void {
    const next = direction > 0
      ? ZOOMS.find((value) => value > zoom + 0.001)
      : [...ZOOMS].reverse().find((value) => value < zoom - 0.001);
    if (next != null) zoom = next;
  }

  /** Сброс вида: 100 % и возврат к выбранному узлу (или к началу поля). */
  function resetView(): void {
    zoom = 1;
    if (!fieldEl) return;
    if (selectedId) focusNode(selectedId);
    else fieldEl.scrollTo({ left: 0, top: 0 });
  }

  function toggleType(type: string): void {
    const next = new Set(hiddenTypes);
    if (next.has(type)) next.delete(type);
    else next.add(type);
    hiddenTypes = next;
  }

  function resetFilters(): void {
    query = '';
    hiddenTypes = new Set();
    mapCap = MAP_PAGE;
  }

  function showMoreOnMap(): void {
    mapCap += MAP_PAGE;
  }

  function onWindowKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape' && selectedNode) {
      event.preventDefault();
      select(null);
    }
  }

  // Перетаскивание поля — необязательный pan: клавиатура, скролл и кнопки
  // масштаба работают и без него.
  let dragging = $state(false);
  let dragOrigin: { x: number; y: number; scrollX: number; scrollY: number } | null = null;

  function onFieldPointerDown(event: PointerEvent): void {
    if (event.pointerType === 'touch' || !fieldEl) return;
    if (event.target instanceof HTMLElement && event.target.closest('button,a')) return;
    dragOrigin = { x: event.clientX, y: event.clientY, scrollX: fieldEl.scrollLeft, scrollY: fieldEl.scrollTop };
    dragging = true;
    fieldEl.setPointerCapture(event.pointerId);
  }

  function onFieldPointerMove(event: PointerEvent): void {
    if (!dragOrigin || !fieldEl) return;
    fieldEl.scrollLeft = dragOrigin.scrollX - (event.clientX - dragOrigin.x);
    fieldEl.scrollTop = dragOrigin.scrollY - (event.clientY - dragOrigin.y);
  }

  function onFieldPointerEnd(): void {
    dragOrigin = null;
    dragging = false;
  }

  const labelVisible = $derived(zoom >= 0.72);
  const hasFilters = $derived(query.trim() !== '' || hiddenTypes.size > 0 || mapCap !== MAP_PAGE);

  // Приёмы поля и легенда — вторичный слой: свёрнуты, чтобы поле корреляции
  // читалось в первом вьюпорте, а не после подсказок.
  let helpOpen = $state(false);

  $effect(() => {
    // Выбранный узел может лежать вне видимой части поля — доводим его в кадр
    // скроллом. Фокус переносит только выбор с самого поля (select с fromField):
    // из списка и инспектора узел показывается без захвата фокуса.
    if (selectedId) nodeElement(selectedId)?.scrollIntoView({ block: 'nearest' });
  });
</script>

<svelte:window onkeydown={onWindowKeydown} />

<section class="map" aria-label="Карта связей корпуса">
  <div class="map__bar">
    <div class="map__tools">
      <p class="map__summary small">
        узлов <b class="num">{graph.nodes.length}</b> · связей <b class="num">{graph.edges.length}</b>
        {#if graph.communities.length > 0}
          · сообществ <b class="num">{graph.communities.length}</b>
        {/if}
        {#if hasFilters || filtered !== graph.nodes.length}
          <span class="map__matched">в отборе <b class="num">{filtered}</b> из <b class="num">{graph.nodes.length}</b></span>
        {/if}
      </p>

      <div class="map__find">
        <Icon name="search" size={17} />
        <input
          type="search"
          class="input"
          bind:value={query}
          placeholder="метка, тип или сообщество"
          aria-label="Фильтр узлов карты по метке, типу, коду или сообществу"
        />
      </div>

      <div class="map__zoom" role="group" aria-label="Масштаб карты">
        <button class="icon-btn" type="button" onclick={() => stepZoom(-1)} disabled={zoom <= ZOOMS[0]} aria-label="Отдалить">
          <Icon name="minus" size={17} />
        </button>
        <button
          type="button"
          class="map__zoom-value num"
          title="Вернуть 100 %"
          aria-label="Масштаб {Math.round(zoom * 100)} процентов: вернуть 100"
          onclick={() => (zoom = 1)}
        >{Math.round(zoom * 100)} %</button>
        <button class="icon-btn" type="button" onclick={() => stepZoom(1)} disabled={zoom >= ZOOMS[ZOOMS.length - 1]} aria-label="Приблизить">
          <Icon name="plus" size={17} />
        </button>
        <Button size="sm" variant="quiet" onclick={resetView}>Сбросить вид</Button>
      </div>
    </div>

    {#if legend.length > 0}
      <div class="map__types" role="group" aria-label="Типы узлов: показать или скрыть кластер">
        {#each legend as item (item.type)}
          <Chip pressed={item.on} onclick={() => toggleType(item.type)}>
            <span class="map__swatch map__swatch--{item.tone}" aria-hidden="true"></span>
            {item.label}
            <span class="num">{item.count}</span>
          </Chip>
        {/each}
      </div>
    {/if}
  </div>

  {#if graph.nodes.length === 0}
    <Empty icon="graph" title="Узлов нет" body="В ответе нет ни одного узла корпуса: задайте вопрос в рабочем пространстве или загрузите документ." />
  {:else if filtered === 0}
    <Empty icon="filter" title="Ничего не отобрано" body="Все {graph.nodes.length} узлов скрыты фильтром или выключенными типами.">
      {#snippet action()}
        <Button variant="quiet" size="sm" onclick={resetFilters}>Сбросить фильтры</Button>
      {/snippet}
    </Empty>
  {:else}
    <div
      class="map__field"
      class:map__field--drag={dragging}
      bind:this={fieldEl}
      bind:clientWidth={fieldW}
      role="group"
      aria-label="Поле карты: {placedCount} из {filtered} отобранных узлов"
      aria-roledescription="мягкая карта: узлы — диски, связи — штрихи; содержимое поля продублировано текстовыми списками ниже"
      onpointerdown={onFieldPointerDown}
      onpointermove={onFieldPointerMove}
      onpointerup={onFieldPointerEnd}
      onpointercancel={onFieldPointerEnd}
    >
      <div class="map__canvas" style="width: {extent.w * zoom}px; height: {extent.h * zoom}px">
        <svg
          class="map__edges"
          width={extent.w * zoom}
          height={extent.h * zoom}
          viewBox="0 0 {extent.w} {extent.h}"
          aria-hidden="true"
          focusable="false"
        >
          {#each drawnEdges as edge (edge.id)}
            {@const from = placed.get(edge.source)}
            {@const to = placed.get(edge.target)}
            {#if from && to}
              {@const activeEdge = incident.has(edge.id)}
              {@const mark = edgeMark(from, to)}
              <path
                class="map__edge"
                class:map__edge--on={activeEdge}
                class:map__edge--dim={selectedNode != null && !activeEdge}
                d={edgePath(from, to)}
              />
              <circle
                class="map__mark"
                class:map__mark--on={activeEdge}
                class:map__mark--dim={selectedNode != null && !activeEdge}
                cx={mark.x}
                cy={mark.y}
                r={activeEdge ? 3.4 : 2.6}
              />
            {/if}
          {/each}
        </svg>

        <div
          class="map__layer"
          style="--map-node-box: {NODE_BOX}px; width: {extent.w}px; height: {extent.h}px; transform: scale({zoom})"
        >
          {#each blocks as block (block.type)}
            <p class="map__block-label" style="left: {block.x}px; top: {block.y}px; width: {block.w}px">
              {block.label}
              <span class="num">{block.shown}</span>
              {#if block.total > block.shown}
                <span class="num map__block-part">из {block.total}</span>
              {/if}
              <code>{block.type}</code>
            </p>

            {#each block.nodes as item (item.node.id)}
              <button
                type="button"
                class="map-node"
                class:map-node--on={selectedId === item.node.id}
                class:map-node--near={selectedNode != null && neighbourIds.has(item.node.id)}
                class:map-node--dim={selectedNode != null &&
                  selectedId !== item.node.id &&
                  !neighbourIds.has(item.node.id)}
                data-node-id={item.node.id}
                aria-pressed={selectedId === item.node.id}
                aria-label="{typeLabel(item.node.type)}: {item.node.label}. Уверенность {fmt(item.node.confidence)}, связей {item.links}, класс данных {classOf(item.node).ru} ({item.node.data_class})."
                style="left: {item.cx}px; top: {item.cy}px; --map-disc: {item.r * 2}px"
                onclick={() => select(item.node, true)}
                onkeydown={(event: KeyboardEvent) => onNodeKeydown(event, item)}
                onfocus={() => (focusedId = item.node.id)}
                onblur={() => (focusedId = null)}
              >
                <span class="map-node__disc map-node__disc--{discTone(item.node.type)}" aria-hidden="true">
                  <span class="num map-node__conf">{fmt(item.node.confidence)}</span>
                </span>
                <span
                  class="map-node__label"
                  class:map-node__label--fade={!labelVisible && focusedId !== item.node.id && selectedId !== item.node.id}
                  aria-hidden="true"
                >{item.node.label}</span>
                {#if item.node.data_class !== 'public'}
                  <span class="map-node__class micro" aria-hidden="true">{DATA_CLASS_LABELS[item.node.data_class] ?? ''}</span>
                {/if}
              </button>
            {/each}
          {/each}
        </div>
      </div>
    </div>

    <p class="map__live" aria-live="polite" role="status">{announcement}</p>

    <div class="map__under">
      <p class="micro muted map__placed">
        в поле <span class="num">{placedCount}</span> из <span class="num">{filtered}</span> отобранных
        узлов{#if placedCount < filtered} · связи с узлами вне поля не рисуются:
          <span class="num">{offFieldEdges}</span> штрихов вне поля{/if}
      </p>
      {#if placedCount < filtered}
        <Button variant="quiet" size="sm" onclick={showMoreOnMap}>
          Показать ещё
          <span class="num">{countOf(Math.min(MAP_PAGE, filtered - placedCount), 'узел', 'узла', 'узлов')}</span>
        </Button>
      {/if}
      {#if hasFilters}
        <Button variant="ghost" size="sm" onclick={resetFilters}>Сбросить фильтры</Button>
      {/if}
    </div>

    <div class="acc map__help">
      <button
        type="button"
        class="acc__head"
        aria-expanded={helpOpen}
        aria-controls="map-help-body"
        onclick={() => (helpOpen = !helpOpen)}
      >
        <span>Как читать поле</span>
        <Icon name="plus" size={16} class="acc__icon" />
      </button>
      {#if helpOpen}
        <div class="acc__body" id="map-help-body">
          <p class="map__keys micro">
            <span class="num">Tab</span> — по узлам поля ·
            <span class="num">← ↑ → ↓</span> — к ближайшему диску в направлении ·
            <span class="num">Home</span> / <span class="num">End</span> — первый и последний узел ·
            <span class="num">Enter</span> / <span class="num">Space</span> — выбрать ·
            <span class="num">Esc</span> — снять выбор ·
            <span class="num">+ − 0</span> — масштаб и 100 % · поле тянется мышью
          </p>
          <div class="map__legend">
            <p class="micro muted">Тон диска — тип узла, размер — уверенность и число связей.</p>
            <div class="map__classes" role="group" aria-label="Классы данных в срезе">
              {#each classKey as item (item.code)}
                <span class="map__class-item">
                  <StatusPill status={item.pill} label={item.ru} />
                  <code class="tech">{item.code}</code>
                  <span class="num">{item.count}</span>
                </span>
              {/each}
            </div>
          </div>
        </div>
      {/if}
    </div>

    <div class="map__text" id="map-text">
      <Panel tone="sunk" tag="section" class="map__panel">
        <div class="panel__head">
          <h3 class="h4">Узлы текстом</h3>
          <p class="micro muted">полное содержимое поля для чтения без графики</p>
        </div>
        <p class="micro muted map__list-head">
          метка узла · тип · уверенность · связей
        </p>
        <ul class="map__list">
          {#each nodeList as node (node.id)}
            <li class="map__list-row">
              <button type="button" class="map__list-target" aria-current={selectedId === node.id ? 'true' : undefined} onclick={() => select(node)}>
                <span class="map__swatch map__swatch--{discTone(node.type)}" aria-hidden="true"></span>
                <span class="grow">{node.label}</span>
              </button>
              <span class="micro muted map__list-kind">{typeLabel(node.type)}</span>
              <span class="num micro">{fmt(node.confidence)}</span>
              <span class="num micro">{degree.get(node.id) ?? 0}</span>
            </li>
          {/each}
        </ul>
        {#if filtered > nodeList.length}
          <Button variant="quiet" size="sm" onclick={() => (listCap += LIST_PAGE)}>
            Показать ещё
            <span class="num">
              {countOf(Math.min(LIST_PAGE, filtered - nodeList.length), 'узел', 'узла', 'узлов')}
            </span>
          </Button>
        {/if}
      </Panel>

      <Panel tone="sunk" tag="section" class="map__panel">
        <div class="panel__head">
          <h3 class="h4">Связи текстом</h3>
          <p class="micro muted">
            <span class="num">{listedEdges.length}</span>
            из <span class="num">{graph.edges.length}</span> связей среза
            {#if listedEdges.length < graph.edges.length}
              · остальные ведут на узлы вне отбора
            {/if}
          </p>
        </div>
        {#if listedEdges.length === 0}
          <p class="micro muted">Для текущего отбора связей нет — узлы стоят отдельно.</p>
        {:else}
          <p class="micro muted map__list-head">исток · отношение · цель · уверенность</p>
          <ul class="map__list">
            {#each listedEdges.slice(0, edgeCap) as edge (edge.id)}
              <li class="map__list-row">
                <span class="grow map__edge-pair">
                  <button type="button" class="map__list-target" onclick={() => select(nodeById.get(edge.source) ?? null)}>
                    {labelOf(edge.source)}
                  </button>
                  <span class="micro muted map__edge-rel">
                    {relationLabel(edge.relation)} <code class="tech">{edge.relation}</code>
                  </span>
                  <button type="button" class="map__list-target" onclick={() => select(nodeById.get(edge.target) ?? null)}>
                    {labelOf(edge.target)}
                  </button>
                  {#if !(placed.has(edge.source) && placed.has(edge.target))}
                    <span class="micro muted">· вне поля</span>
                  {/if}
                </span>
                <span class="num micro">{fmt(edge.confidence)}</span>
              </li>
            {/each}
          </ul>
          {#if listedEdges.length > edgeCap}
            <Button variant="quiet" size="sm" onclick={() => (edgeCap += LIST_PAGE)}>
              Показать ещё
              <span class="num">
                {countOf(Math.min(LIST_PAGE, listedEdges.length - edgeCap), 'связь', 'связи', 'связей')}
              </span>
            </Button>
          {/if}
        {/if}
      </Panel>
    </div>
  {/if}
</section>

<style>
  .map {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  .map__bar {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  .map__summary {
    color: var(--ink-3);
    margin: 0;
    min-width: 0;
  }

  .map__summary b {
    color: var(--ink);
    font-weight: 600;
  }

  .map__matched {
    margin-inline-start: var(--s3);
  }

  /* Строка управления полем: счётчики, поиск и масштаб в один плотный ряд. */
  .map__tools {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .map__find {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex: 1 1 260px;
    min-width: 220px;
    padding-inline: var(--s4);
    border-radius: var(--r-pill);
    background: var(--surface-sunk);
    color: var(--ink-3);
  }

  .map__find .input {
    border: 0;
    background: none;
    box-shadow: none;
    min-height: 44px;
    padding-inline-start: 0;
  }

  .map__zoom {
    display: flex;
    align-items: center;
    gap: var(--s2);
    padding: var(--s1) var(--s2) var(--s1) var(--s1);
    border-radius: var(--r-pill);
    background: var(--surface);
    border: 1px solid var(--line);
  }

  .map__zoom-value {
    min-width: 6ch;
    padding: var(--s1) var(--s2);
    border: 0;
    border-radius: var(--r-pill);
    background: none;
    text-align: center;
    font-family: var(--font-data);
    font-size: var(--t-small);
    color: var(--ink-2);
    cursor: pointer;
  }

  .map__zoom-value:hover {
    background: var(--surface-sunk);
    color: var(--ink);
  }

  .map__types {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
  }

  .map__swatch {
    width: 12px;
    height: 12px;
    border-radius: var(--r-pill);
    flex: none;
    box-shadow: inset 0 0 0 1px var(--line-strong);
  }

  .map__swatch--coral {
    background: var(--coral-mist);
  }

  .map__swatch--sage {
    background: var(--sage-deep);
  }

  .map__swatch--lav {
    background: var(--lavender-deep);
  }

  .map__swatch--stone {
    background: var(--superseded-wash);
  }

  .map__keys {
    margin: 0;
    color: var(--ink-3);
    line-height: 1.9;
  }

  .map__keys .num {
    padding: 2px var(--s2);
    border-radius: var(--r-xs);
    background: var(--surface-sunk);
    color: var(--ink-2);
  }

  /* Пастельное ground вместо прежней разметки-блюпринта.
     Высота поля следует за вьюпортом: поле корреляции — главный объект экрана
     и читается в первом вьюпорте, а не после подсказок. */
  .map__field {
    position: relative;
    overflow: auto;
    overscroll-behavior: contain;
    height: clamp(320px, 48dvh, 500px);
    padding: 0;
    border-radius: var(--r-xl);
    border: 1px solid var(--line-soft);
    background:
      radial-gradient(120% 90% at 8% 0%, var(--sage) 0%, transparent 58%),
      radial-gradient(110% 80% at 92% 6%, var(--lavender) 0%, transparent 56%),
      radial-gradient(120% 95% at 50% 100%, var(--peach-wash) 0%, transparent 62%),
      var(--paper);
    box-shadow: var(--shadow-soft);
    cursor: grab;
    scrollbar-width: thin;
  }

  .map__field--drag {
    cursor: grabbing;
  }

  .map__canvas {
    position: relative;
  }

  .map__edges {
    position: absolute;
    inset: 0;
    overflow: visible;
  }

  .map__layer {
    position: absolute;
    inset: 0 auto auto 0;
    transform-origin: 0 0;
  }

  .map__edge {
    fill: none;
    stroke: var(--line-strong);
    stroke-width: 1.5px;
    stroke-linecap: round;
    opacity: 0.75;
    transition: opacity var(--dur-fast) var(--ease-soft), stroke var(--dur-fast) var(--ease-soft);
  }

  /* Подсветка выбранных связей — нейтральные чернила: это выделение чтения,
     а не статус действия, поэтому коралл здесь не участвует. */
  .map__edge--on {
    stroke: var(--ink-3);
    stroke-width: 2px;
    opacity: 1;
  }

  .map__edge--dim {
    opacity: 0.3;
  }

  .map__mark {
    fill: var(--line-strong);
    opacity: 0.7;
    transition: opacity var(--dur-fast) var(--ease-soft), fill var(--dur-fast) var(--ease-soft);
  }

  .map__mark--on {
    fill: var(--ink-3);
    opacity: 1;
  }

  .map__mark--dim {
    opacity: 0.25;
  }

  .map__block-label {
    position: absolute;
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    max-width: 40ch;
    font-size: var(--t-small);
    font-weight: 600;
    letter-spacing: var(--tr-head);
    color: var(--ink-2);
  }

  .map__block-label code,
  .map__block-part {
    font-family: var(--font-data);
    font-size: var(--t-micro);
    font-weight: 400;
    color: var(--ink-3);
  }

  .map__block-label .num {
    font-family: var(--font-data);
    color: var(--ink-3);
    font-size: var(--t-micro);
  }

  .map-node {
    position: absolute;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--s1);
    width: var(--map-node-box);
    transform: translate(-50%, -50%);
    padding: var(--s1);
    border: 0;
    border-radius: var(--r-lg);
    background: none;
    color: inherit;
    cursor: pointer;
    text-align: center;
  }

  .map-node__disc {
    display: grid;
    place-items: center;
    width: var(--map-disc);
    height: var(--map-disc);
    border-radius: var(--r-pill);
    box-shadow: var(--shadow-soft);
    transition: box-shadow var(--dur-fast) var(--ease-soft), transform var(--dur-fast) var(--ease-enter);
  }

  .map-node__disc--coral {
    background: var(--coral-mist);
  }

  .map-node__disc--sage {
    background: var(--sage-deep);
  }

  .map-node__disc--lav {
    background: var(--lavender-deep);
  }

  .map-node__disc--stone {
    background: var(--superseded-wash);
  }

  .map-node__conf {
    font-size: var(--t-micro);
    line-height: 1;
    color: var(--ink-2);
    opacity: 0;
    transition: opacity var(--dur-fast) var(--ease-soft);
  }

  .map-node:hover .map-node__conf,
  .map-node:focus-visible .map-node__conf,
  .map-node--on .map-node__conf {
    opacity: 1;
  }

  .map-node:hover .map-node__disc {
    box-shadow: var(--shadow-lift);
    transform: translateY(-1px);
  }

  /* Видимый focus и отдельное кольцо выбранного узла. */
  .map-node:focus-visible {
    outline: none;
  }

  .map-node:focus-visible .map-node__disc {
    box-shadow: var(--shadow-focus), 0 0 0 2px var(--paper), var(--shadow-lift);
  }

  .map-node--on .map-node__disc {
    box-shadow: 0 0 0 2px var(--paper), 0 0 0 4px var(--action-ink), var(--shadow-lift);
  }

  .map-node--on.map-node:focus-visible .map-node__disc {
    box-shadow: var(--shadow-focus), 0 0 0 2px var(--paper), 0 0 0 5px var(--action-ink);
  }

  .map-node__label {
    font-family: var(--font-ui);
    font-size: var(--t-micro);
    line-height: 1.28;
    color: var(--ink);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    overflow-wrap: anywhere;
  }

  .map-node__label--fade {
    /* На мелком масштабе подписи превращались бы в кашу: узел без фокуса
       остаётся диском, метку читает aria-label и текстовый список. */
    opacity: 0;
  }

  .map-node__class {
    font-size: var(--t-micro);
    color: var(--ink-3);
  }

  /* Соседи выбранного узла остаются в кадре, остальное поле отступает. */
  .map-node--near .map-node__disc {
    box-shadow: var(--shadow-lift);
  }

  .map-node--dim .map-node__disc,
  .map-node--dim .map-node__label {
    opacity: 0.45;
  }

  .map__live {
    margin: 0;
    font-size: var(--t-small);
    color: var(--ink-2);
    min-height: 1.4em;
  }

  .map__live:empty {
    min-height: 0;
  }

  .map__under {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .map__placed {
    margin: 0;
  }

  .map__help .acc__head {
    padding: var(--s4) var(--s5);
    font-size: var(--t-small);
  }

  .map__help .acc__body {
    padding: 0 var(--s5) var(--s5);
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  .map__legend {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s4);
    flex-wrap: wrap;
  }

  .map__legend p {
    margin: 0;
    max-width: 42ch;
  }

  .map__classes {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .map__class-item {
    display: inline-flex;
    align-items: center;
    gap: var(--s2);
    font-size: var(--t-micro);
    color: var(--ink-3);
  }

  .map__text {
    display: grid;
    gap: var(--s4);
    grid-template-columns: repeat(auto-fit, minmax(min(340px, 100%), 1fr));
    scroll-margin-top: calc(var(--topbar-h) + var(--s5));
  }

  /* Класс уходит внутрь Panel: объявляем глобально, имя принадлежит карте. */
  :global(.panel.map__panel) {
    padding: var(--s5);
  }

  .map__list-head {
    margin: 0 0 var(--s2);
    letter-spacing: var(--tr-body);
  }

  .map__list {
    list-style: none;
    margin: 0 0 var(--s3);
    padding: 0;
    display: flex;
    flex-direction: column;
  }

  .map__list-row {
    display: flex;
    align-items: center;
    gap: var(--s3);
    padding: var(--s2) 0;
    border-top: 1px solid var(--line);
    font-size: var(--t-small);
    min-height: 40px;
  }

  .map__list-row:first-child {
    border-top: 0;
  }

  .map__list-target {
    display: inline-flex;
    align-items: center;
    gap: var(--s2);
    border: 0;
    padding: 0;
    background: none;
    color: var(--ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
    min-width: 0;
  }

  .map__list-target:hover {
    color: var(--action-ink);
  }

  .map__list-target[aria-current='true'] {
    font-weight: 600;
  }

  .map__list-kind {
    white-space: nowrap;
  }

  .map__edge-pair {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
  }

  /* Мобильная композиция поля: строка фильтров идёт текучей полосой, а не
     лесенкой из строк — поле корреляции поднимается в первый вьюпорт. */
  @media (max-width: 900px) {
    .map__types {
      flex-wrap: nowrap;
      overflow-x: auto;
      padding-block-end: var(--s2);
      scrollbar-width: thin;
    }

    .map__types > * {
      flex: none;
    }

    .map__summary {
      flex: 1 1 100%;
    }
  }

  @media (max-width: 640px) {
    .map__field {
      height: clamp(300px, 46dvh, 420px);
    }
  }
</style>
