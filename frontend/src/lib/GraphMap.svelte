<script module lang="ts">
  // Разбор класса данных узла — один на компонент и на страницу-инспектор:
  // русское имя приходит из контракта (DATA_CLASS_LABELS), пилюля статуса — из
  // словаря карты в $lib/terms. Сами словари (типы узлов, отношения, слои)
  // живут там же: локальных карт имён у карты нет.
  import { MAP_DATA_CLASS_PILL } from './terms';
  import { DATA_CLASS_LABELS, type DataClass, type GraphNode } from './types';

  export interface NodeClass {
    code: DataClass;
    ru: string;
    pill: 'consensus' | 'hypothesis' | 'disputed';
  }

  export function classOf(node: GraphNode): NodeClass {
    return {
      code: node.data_class,
      ru: DATA_CLASS_LABELS[node.data_class],
      pill: MAP_DATA_CLASS_PILL[node.data_class] ?? 'consensus',
    };
  }
</script>

<script lang="ts">
  // Карта связей читается за два хода.
  //
  // 1. Обзор корпуса: сообщество за сообществом (кластеры считает сервер, имена
  //    — по ключевым меткам узлов). Показывать граф целиком нельзя: реальным
  //    корпусом считаются сотни источников, и поле на N тысяч узлов не читается
  //    ни при какой раскладке — «окно в 12 строк» при этом показывает не корпус,
  //    а произвольную полку. Обзор отвечает на вопрос «где искать» и остаётся
  //    одного размера при любом корпусе.
  // 2. Цепочки выбранного сообщества: три колонки по роли узла — источники,
  //    выводы, сущности. Порядок колонок повторяет трассу доверия продукта
  //    («откуда известно → что утверждено → о чём говорят»), и из него следует
  //    главное: почти каждое отношение связывает соседние колонки, то есть
  //    пересекает ровно один промежуток. Длинных диагоналей через всё поле, из-за
  //    которых граф распадался в клубок, при такой раскладке не бывает.
  //
  // Узел — строка с полной меткой, связь — линия с остриём у цели и русским
  // именем на ней. Якоря линий измеряются в живом DOM, а не вычисляются из
  // сетки: прежнее поле привязывало конец штриха к центру кнопки, тогда как
  // видимый диск лежал на 13–23 px выше, и связи уходили в пустоту.
  //
  // Масштаба, перетаскивания и внутреннего скролла нет: поле растёт с
  // содержимым, прокрутку ведёт документ. Плотность регулируется сообществом,
  // отбором и «Показать ещё».
  //
  // Клавиатура: Tab по строкам обычного потока, ← → между колонками, ↑ ↓ по
  // колонке, Home/End — край колонки, Enter — выбрать, Esc — снять. На ≤900 px
  // поле пересобирается в вертикальное чтение: у каждой строки отношения
  // печатаются списком под ней, линии не нужны, данные те же.
  import { tick } from 'svelte';
  // GraphNode уже импортирован в <script module> — то же пространство имён.
  import type { GraphEdge, GraphSnapshot } from './types';
  import { countOf } from './format';
  import {
    MAP_ALL_COMMUNITIES,
    MAP_LAYER_GLOSS,
    MAP_LAYER_LABELS,
    MAP_LAYER_ORDER,
    MAP_NODE_TYPE_ORDER,
    MAP_OVERVIEW_TITLE,
    mapLayerOf,
    mapNodeType,
    mapRelationKnown,
    mapRelationLabel,
    type MapLayer,
  } from './terms';
  import Button from './ui/Button.svelte';
  import Chip from './ui/Chip.svelte';
  import Empty from './ui/Empty.svelte';
  import Icon from './ui/Icon.svelte';
  import StatusPill from './ui/StatusPill.svelte';

  interface Props {
    graph: GraphSnapshot;
    selectedId?: string;
    /** Выбранная связь живёт на экране: поле подсвечивает её, инспектор описывает. */
    edgeId?: string;
    onselect?: (node: GraphNode | null) => void;
    onpickEdge?: (edge: GraphEdge | null) => void;
  }

  let { graph, selectedId, edgeId, onselect, onpickEdge }: Props = $props();

  // ── Правила поля ────────────────────────────────────────────────────────
  /**
   * Выводов в окне. Соседних колонок берётся в полтора раза больше: у вывода
   * обычно несколько источников и сущностей. 8 × ~3,5 связи дают поле, где
   * подписи ещё помещаются на своих линиях.
   */
  const PAGE = 8;
  const CARD_PAGE = 12;
  const STARTERS = 8;
  /** Минимальный вертикальный шаг подписей в промежутке: ниже — строки сливаются. */
  const MARK_GAP = 26;
  /** Смещение параллельных связей между одной парой узлов. */
  const FAN = 9;
  /** Больше этого числа подписей не печатаем: поле начинает читаться с трудом. */
  const MARK_LIMIT = 36;

  const LAYER_INDEX: Record<MapLayer, number> = { source: 0, claim: 1, entity: 2 };

  const nf = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 });
  const fmt = (v: number): string => nf.format(v);

  // ── Состояние ───────────────────────────────────────────────────────────
  let level = $state<'overview' | 'chain'>('overview');
  let levelChosen = false;
  let activeCommunity = $state('');
  let query = $state('');
  let hiddenTypes = $state<Set<string>>(new Set());
  let cap = $state(PAGE);
  let cardsCap = $state(CARD_PAGE);
  let chainOnly = $state(false);
  let narrow = $state(false);
  let announcement = $state('');
  let fieldEl = $state<HTMLDivElement | null>(null);
  let box = $state({ w: 0, h: 0 });

  interface Link {
    edge: GraphEdge;
    d: string;
    gutter: number;
    active: boolean;
    picked: boolean;
    dim: boolean;
  }

  interface Mark {
    edge: GraphEdge;
    x: number;
    y: number;
    text: string;
    picked: boolean;
    dim: boolean;
  }

  let links = $state<Link[]>([]);
  let marks = $state<Mark[]>([]);

  // ── Разбор среза ────────────────────────────────────────────────────────
  function communityOf(node: GraphNode): string {
    const raw = node.metadata['community'];
    return typeof raw === 'string' ? raw : '';
  }

  const nodeById = $derived(new Map(graph.nodes.map((n) => [n.id, n])));
  const selectedNode = $derived(graph.nodes.find((n) => n.id === selectedId) ?? null);

  const degree = $derived.by(() => {
    const map = new Map<string, number>();
    for (const edge of graph.edges) {
      map.set(edge.source, (map.get(edge.source) ?? 0) + 1);
      map.set(edge.target, (map.get(edge.target) ?? 0) + 1);
    }
    return map;
  });

  const neighbours = $derived.by(() => {
    const map = new Map<string, Set<string>>();
    const add = (a: string, b: string): void => {
      const set = map.get(a) ?? new Set<string>();
      set.add(b);
      map.set(a, set);
    };
    for (const edge of graph.edges) {
      add(edge.source, edge.target);
      add(edge.target, edge.source);
    }
    return map;
  });

  /**
   * Сообщества: имена с сервера, числа из этого же среза. Связью сообщества
   * считается та, у которой оба конца в нём — иначе карточка обещала бы
   * отношения, которые внутри колонки не найти.
   */
  interface Community {
    name: string;
    nodes: number;
    claims: number;
    sources: number;
    entities: number;
    links: number;
  }

  const communities = $derived.by<Community[]>(() => {
    const buckets = new Map<string, GraphNode[]>();
    for (const node of graph.nodes) {
      const key = communityOf(node);
      const list = buckets.get(key);
      if (list) list.push(node);
      else buckets.set(key, [node]);
    }
    const linkCount = new Map<string, number>();
    for (const edge of graph.edges) {
      const from = nodeById.get(edge.source);
      const to = nodeById.get(edge.target);
      if (!from || !to) continue;
      const key = communityOf(from);
      if (key !== communityOf(to)) continue;
      linkCount.set(key, (linkCount.get(key) ?? 0) + 1);
    }
    const rows = [...buckets.entries()].map(([name, nodes]) => ({
      name,
      nodes: nodes.length,
      claims: nodes.filter((node) => mapLayerOf(node.type) === 'claim').length,
      sources: nodes.filter((node) => mapLayerOf(node.type) === 'source').length,
      entities: nodes.filter((node) => mapLayerOf(node.type) === 'entity').length,
      links: linkCount.get(name) ?? 0,
    }));
    return rows.sort((a, b) => b.claims - a.claims || b.nodes - a.nodes || a.name.localeCompare(b.name, 'ru'));
  });

  /** Есть ли из чего выбирать: один кластер — не обзор, а лишний клик. */
  const hasChoice = $derived(communities.length > 1);

  $effect(() => {
    // Пока человек сам не выбрал уровень, маленький корпус открывается сразу
    // цепочками: показывать одну карточку «вот ваш единственный кластер» —
    // значит заставлять кликать ради ничего.
    if (!levelChosen) level = hasChoice ? 'overview' : 'chain';
  });

  const overview = $derived<Community>({
    name: MAP_ALL_COMMUNITIES,
    nodes: graph.nodes.length,
    claims: communities.reduce((sum, item) => sum + item.claims, 0),
    sources: communities.reduce((sum, item) => sum + item.sources, 0),
    entities: communities.reduce((sum, item) => sum + item.entities, 0),
    links: graph.edges.length,
  });

  const cards = $derived([overview, ...communities]);
  const shownCards = $derived(cards.slice(0, cardsCap));
  const restCards = $derived(Math.max(0, cards.length - shownCards.length));

  const starters = $derived.by(() =>
    [...graph.nodes]
      .sort(
        (a, b) =>
          (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0) || a.label.localeCompare(b.label, 'ru'),
      )
      .slice(0, STARTERS)
      .map((node) => ({ node, links: degree.get(node.id) ?? 0 })),
  );

  // ── Отбор поля ──────────────────────────────────────────────────────────
  function matches(node: GraphNode, q: string): boolean {
    if (hiddenTypes.has(node.type)) return false;
    if (!q) return true;
    return (
      node.label.toLowerCase().includes(q) ||
      node.id.toLowerCase().includes(q) ||
      node.type.toLowerCase().includes(q) ||
      mapNodeType(node.type).toLowerCase().includes(q)
    );
  }

  const scopedNodes = $derived.by<GraphNode[]>(() => {
    const q = query.trim().toLowerCase();
    return graph.nodes.filter(
      (node) =>
        (!activeCommunity || communityOf(node) === activeCommunity) && matches(node, q),
    );
  });

  const filtered = $derived(scopedNodes.length);

  const incident = $derived.by(() => {
    const ids = new Set<string>();
    if (!selectedNode) return ids;
    for (const edge of graph.edges) {
      if (edge.source === selectedNode.id || edge.target === selectedNode.id) ids.add(edge.id);
    }
    return ids;
  });

  const neighbourIds = $derived(
    selectedNode ? new Set(neighbours.get(selectedNode.id) ?? []) : new Set<string>(),
  );

  const presentTypes = $derived.by<string[]>(() => {
    const seen = new Set(
      graph.nodes
        .filter((node) => !activeCommunity || communityOf(node) === activeCommunity)
        .map((node) => node.type),
    );
    const indexOf = (type: string): number => {
      const at = MAP_NODE_TYPE_ORDER.indexOf(type);
      return at < 0 ? MAP_NODE_TYPE_ORDER.length : at;
    };
    return [...seen].sort((a, b) => indexOf(a) - indexOf(b) || a.localeCompare(b, 'ru'));
  });

  const legend = $derived.by(() => {
    const inField = new Map<string, number>();
    for (const id of visibleIds) {
      const type = nodeById.get(id)?.type;
      if (type) inField.set(type, (inField.get(type) ?? 0) + 1);
    }
    return presentTypes.map((type) => ({
      type,
      label: mapNodeType(type),
      count: inField.get(type) ?? 0,
      on: !hiddenTypes.has(type),
    }));
  });

  // ── Раскладка по слоям ──────────────────────────────────────────────────
  interface Column {
    layer: MapLayer;
    label: string;
    gloss: string;
    rows: GraphNode[];
    rest: number;
  }

  /**
   * Окно поля строится от выводов наружу: сначала берутся `cap` выводов с
   * наибольшим числом связей, а источники и сущности — только те, что реально
   * стоят рядом с этими выводами. Прежняя независимая пагинация по колонкам
   * давала 12 выводов и 12 источников, которые друг друга не касались: на
   * области в 480 связей на поле рисовалось 23 линии, и ряды висели порознь.
   *
   * Порядок внутри колонок — по среднему положению своего вывода (один проход
   * барицентра), поэтому линии идут параллельно и почти не переплетаются.
   * Детерминировано: один и тот же срез всегда рисуется одинаково.
   */
  const columns = $derived.by<Column[]>(() => {
    const buckets: Record<MapLayer, GraphNode[]> = { source: [], claim: [], entity: [] };
    for (const node of scopedNodes) buckets[mapLayerOf(node.type)].push(node);

    const byWeight = (a: GraphNode, b: GraphNode): number =>
      (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0) || a.label.localeCompare(b.label, 'ru');

    const claims = [...buckets.claim].sort(byWeight);
    const claimRows = claims.slice(0, cap);
    const claimSet = new Set(claimRows.map((node) => node.id));
    const rank = new Map(claimRows.map((node, i) => [node.id, i]));

    /** Сколько связей узел имеет с видимыми выводами. */
    const toClaims = (node: GraphNode): number => {
      const near = neighbours.get(node.id);
      if (!near) return 0;
      let hits = 0;
      for (const id of near) if (claimSet.has(id)) hits += 1;
      return hits;
    };

    const barycenter = (node: GraphNode): number => {
      const near = neighbours.get(node.id);
      if (!near) return Number.POSITIVE_INFINITY;
      let sum = 0;
      let hits = 0;
      for (const id of near) {
        const at = rank.get(id);
        if (at != null) {
          sum += at;
          hits += 1;
        }
      }
      return hits > 0 ? sum / hits : Number.POSITIVE_INFINITY;
    };

    const neighbourCap = Math.ceil(cap * 1.5);
    const rowsFor = (layer: MapLayer): { rows: GraphNode[]; rest: number } => {
      const all = buckets[layer];
      if (layer === 'claim') return { rows: claimRows, rest: Math.max(0, all.length - claimRows.length) };
      // Связанные с видимыми выводами идут первыми: они и дают линии на поле.
      const sorted = [...all].sort(
        (a, b) => toClaims(b) - toClaims(a) || barycenter(a) - barycenter(b) || byWeight(a, b),
      );
      const rows = sorted.slice(0, neighbourCap).sort(
        (a, b) => barycenter(a) - barycenter(b) || byWeight(a, b),
      );
      return { rows, rest: Math.max(0, all.length - rows.length) };
    };

    return MAP_LAYER_ORDER.map((layer) => {
      const { rows, rest } = rowsFor(layer);
      return {
        layer,
        label: MAP_LAYER_LABELS[layer],
        gloss: MAP_LAYER_GLOSS[layer],
        rows,
        rest,
      };
    });
  });

  const visibleIds = $derived(
    new Set(columns.flatMap((column) => column.rows.map((node) => node.id))),
  );

  const shownCount = $derived(visibleIds.size);
  const restTotal = $derived(columns.reduce((sum, column) => sum + column.rest, 0));

  /**
   * Связи области, а не всего корпуса: вне этого множества связи считать
   * «не показанными» бессмысленно — человек их не искал.
   */
  const scopeEdges = $derived.by<GraphEdge[]>(() => {
    const layerOfId = new Map<string, number>();
    for (const node of scopedNodes) layerOfId.set(node.id, LAYER_INDEX[mapLayerOf(node.type)]);
    return graph.edges.filter(
      (edge) => layerOfId.has(edge.source) && layerOfId.has(edge.target),
    );
  });

  /**
   * Связи делятся на рисуемые (соседние колонки) и остальные: внутри колонки и
   * через колонку. Остальные не молчат — называются числом и читаются списком
   * под полем, а в инспекторе выбранного узла видны всегда.
   */
  const edgeSplit = $derived.by<{ drawn: GraphEdge[]; other: GraphEdge[] }>(() => {
    const drawn: GraphEdge[] = [];
    const other: GraphEdge[] = [];
    for (const edge of scopeEdges) {
      if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target)) continue;
      const span = Math.abs(
        LAYER_INDEX[mapLayerOf(nodeById.get(edge.source)?.type)] -
          LAYER_INDEX[mapLayerOf(nodeById.get(edge.target)?.type)],
      );
      (span === 1 ? drawn : other).push(edge);
    }
    return { drawn, other };
  });

  const offFieldEdges = $derived(
    scopeEdges.length - edgeSplit.drawn.length - edgeSplit.other.length,
  );

  // ── Измерение поля ──────────────────────────────────────────────────────
  function labelFor(edge: GraphEdge): string {
    return mapRelationLabel(edge.relation);
  }

  /**
   * Якорь = измеренный прямоугольник строки. Линия выходит из правого края
   * левой строки и входит в левый край правой (направление показывает остриё),
   * поэтому конец штриха физически не может разойтись с тем, что человек видит.
   */
  function measure(): void {
    const root = fieldEl;
    if (!root) {
      links = [];
      marks = [];
      return;
    }
    if (narrow) {
      // Вертикальное чтение: отношений на линиях нет, они под каждой строкой.
      links = [];
      marks = [];
      box = { w: root.clientWidth, h: root.clientHeight };
      return;
    }

    const frame = root.getBoundingClientRect();
    const pos = new Map<string, { left: number; right: number; cy: number }>();
    for (const el of root.querySelectorAll<HTMLElement>('[data-chain-id]')) {
      const id = el.dataset['chainId'];
      if (!id) continue;
      const r = el.getBoundingClientRect();
      pos.set(id, {
        left: r.left - frame.left,
        right: r.right - frame.left,
        cy: r.top - frame.top + r.height / 2,
      });
    }

    const fan = new Map<string, number>();
    const rows: Link[] = [];
    const pending: { edge: GraphEdge; gutter: number; x: number; y: number }[] = [];
    const wantAllMarks = edgeSplit.drawn.length <= MARK_LIMIT;

    for (const edge of edgeSplit.drawn) {
      const from = pos.get(edge.source);
      const to = pos.get(edge.target);
      if (!from || !to) continue;
      const fi = LAYER_INDEX[mapLayerOf(nodeById.get(edge.source)?.type)];
      const ti = LAYER_INDEX[mapLayerOf(nodeById.get(edge.target)?.type)];
      const forward = ti > fi;
      const key = `${edge.source}|${edge.target}`;
      const dup = fan.get(key) ?? 0;
      fan.set(key, dup + 1);
      const shift = dup * FAN;

      const x0 = (forward ? from.right : from.left) + 2;
      const x1 = (forward ? to.left : to.right) - 2;
      const y0 = from.cy + shift;
      const y1 = to.cy + shift;
      const dir = forward ? 1 : -1;
      const k = Math.max(28, Math.abs(x1 - x0) * 0.42);
      const gutter = Math.min(fi, ti);

      rows.push({
        edge,
        d: `M ${x0.toFixed(1)} ${y0.toFixed(1)} C ${(x0 + dir * k).toFixed(1)} ${y0.toFixed(1)} ${(x1 - dir * k).toFixed(1)} ${y1.toFixed(1)} ${x1.toFixed(1)} ${y1.toFixed(1)}`,
        gutter,
        active: incident.has(edge.id),
        picked: edge.id === edgeId,
        dim: selectedNode != null && !incident.has(edge.id),
      });

      if (wantAllMarks || incident.has(edge.id) || edge.id === edgeId) {
        pending.push({ edge, gutter, x: (x0 + x1) / 2, y: (y0 + y1) / 2 });
      }
    }

    // Подписи раздвигаются по вертикали внутри своего промежутка: две связи
    // одного узла иначе ложатся друг на друга и на метку строки.
    const placed: Mark[] = [];
    for (const gutter of [0, 1]) {
      let last = Number.NEGATIVE_INFINITY;
      const group = pending
        .filter((row) => row.gutter === gutter)
        .sort((a, b) => a.y - b.y);
      for (const row of group) {
        const y = Math.max(row.y, last + MARK_GAP);
        last = y;
        placed.push({
          edge: row.edge,
          x: row.x,
          y,
          text: labelFor(row.edge),
          picked: row.edge.id === edgeId,
          // Подпись гаснет вместе со своей линией: иначе выбранный узел
          // тонет в ярком тумане чужих отношений.
          dim: selectedNode != null && !incident.has(row.edge.id),
        });
      }
    }

    links = rows;
    marks = placed;
    box = { w: root.clientWidth, h: root.clientHeight };
  }

  const layoutKey = $derived(
    [
      columns.map((column) => column.rows.map((node) => node.id).join(',')).join('|'),
      cap,
      narrow ? 'n' : 'w',
      selectedId ?? '',
      edgeId ?? '',
      edgeSplit.drawn.length,
    ].join('#'),
  );

  $effect(() => {
    const key = layoutKey;
    void key;
    const raf = requestAnimationFrame(() => measure());
    return () => cancelAnimationFrame(raf);
  });

  $effect(() => {
    const root = fieldEl;
    if (!root) return;
    const observer = new ResizeObserver(() => measure());
    observer.observe(root);
    for (const column of root.querySelectorAll<HTMLElement>('.chain-col')) observer.observe(column);
    // Метрика шрифтов приходит после первого кадра: без перимера линии встанут
    // по прежним строкам.
    void document.fonts.ready.then(() => measure());
    return () => observer.disconnect();
  });

  $effect(() => {
    const mq = window.matchMedia('(max-width: 900px)');
    const sync = (): void => {
      narrow = mq.matches;
    };
    sync();
    mq.addEventListener('change', sync);
    return () => mq.removeEventListener('change', sync);
  });

  // ── Действия ────────────────────────────────────────────────────────────
  function select(node: GraphNode | null): void {
    onselect?.(node);
    onpickEdge?.(null);
    if (node) {
      const count = degree.get(node.id) ?? 0;
      announcement = `Выбран узел «${node.label}», ${mapNodeType(node.type)}. Связей: ${countOf(count, 'связь', 'связи', 'связей')}. Доказательства — под картой.`;
    } else {
      announcement = 'Выбор узла снят.';
    }
  }

  function pick(edge: GraphEdge): void {
    if (edgeId === edge.id) {
      onpickEdge?.(null);
      announcement = 'Выбор связи снят.';
      return;
    }
    onpickEdge?.(edge);
    announcement = `Связь «${mapRelationLabel(edge.relation)}»: ${nodeById.get(edge.source)?.label ?? ''} → ${nodeById.get(edge.target)?.label ?? ''}.`;
  }

  /** Вход в сообщество из карточки или из списка крупнейших узлов. */
  function openCommunity(name: string): void {
    activeCommunity = name === MAP_ALL_COMMUNITIES ? '' : name;
    cap = PAGE;
    level = 'chain';
    levelChosen = true;
  }

  function openNode(node: GraphNode): void {
    if (hasChoice) openCommunity(communityOf(node) || MAP_ALL_COMMUNITIES);
    select(node);
  }

  function backToOverview(): void {
    level = 'overview';
    levelChosen = true;
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
    cap = PAGE;
  }

  const hasFilters = $derived(query.trim() !== '' || hiddenTypes.size > 0 || cap !== PAGE);

  function rowOf(id: string): GraphNode | null {
    return nodeById.get(id) ?? null;
  }

  /** ← → переводят между колонками, ↑ ↓ — по колонке. */
  function move(current: GraphNode, dx: number, dy: number): boolean {
    const at = columns.findIndex((column) => column.rows.some((node) => node.id === current.id));
    if (at < 0) return false;
    if (dx !== 0) {
      const next = columns[at + dx];
      if (!next || next.rows.length === 0) return false;
      const index = columns[at].rows.findIndex((node) => node.id === current.id);
      focusRow(next.rows[Math.min(index, next.rows.length - 1)].id);
      return true;
    }
    const column = columns[at];
    const index = column.rows.findIndex((node) => node.id === current.id);
    const target = column.rows[index + dy];
    if (!target) return false;
    focusRow(target.id);
    return true;
  }

  function focusRow(id: string): void {
    fieldEl?.querySelector<HTMLElement>(`[data-chain-id="${CSS.escape(id)}"]`)?.focus();
  }

  function onRowKeydown(event: KeyboardEvent, node: GraphNode): void {
    const keys: Record<string, [number, number]> = {
      ArrowRight: [1, 0],
      ArrowLeft: [-1, 0],
      ArrowUp: [0, -1],
      ArrowDown: [0, 1],
    };
    const step = keys[event.key];
    if (step) {
      if (move(node, step[0], step[1])) event.preventDefault();
      return;
    }
    if (event.key === 'Home' || event.key === 'End') {
      const column = columns.find((item) => item.rows.some((row) => row.id === node.id));
      const target = column
        ? event.key === 'Home'
          ? column.rows[0]
          : column.rows[column.rows.length - 1]
        : undefined;
      if (target) {
        event.preventDefault();
        focusRow(target.id);
      }
    }
  }

  /**
   * Отношения строки для вертикального чтения. Берутся все связи среза, а не
   * только нарисованные: у строки на узком экране линии нет, и прятать связь
   * потому, что её конец не лёг в поле, — значит показывать меньше, чем есть.
   */
  const outgoing = $derived.by(() => {
    const map = new Map<string, GraphEdge[]>();
    for (const edge of graph.edges) {
      const list = map.get(edge.source);
      if (list) list.push(edge);
      else map.set(edge.source, [edge]);
    }
    return map;
  });

  const incoming = $derived.by(() => {
    const map = new Map<string, GraphEdge[]>();
    for (const edge of graph.edges) {
      const list = map.get(edge.target);
      if (list) list.push(edge);
      else map.set(edge.target, [edge]);
    }
    return map;
  });

  async function showMore(): Promise<void> {
    cap += PAGE;
    await tick();
    measure();
  }
</script>

<svelte:window
  onkeydown={(event) => {
    if (event.key === 'Escape' && (selectedNode || edgeId)) {
      event.preventDefault();
      select(null);
    }
  }}
/>

<section class="map" aria-label="Карта связей корпуса">
  {#if graph.nodes.length === 0}
    <Empty
      icon="graph"
      title="Узлов нет"
      body="В срезе нет ни одного узла корпуса: задайте вопрос в рабочем пространстве или загрузите документ."
    />
  {:else if level === 'overview'}
    <!-- Уровень 1: где искать. Экран остаётся одного размера и на 6 узлах,
         и на 6 тысячах — дальше идут сообщества, а не весь граф. -->
    <div class="map__head">
      <h3 class="h4">{MAP_OVERVIEW_TITLE}</h3>
      <p class="micro muted">
        Корпус показан сообществами: поле целиком на сотни источников не читается,
        поэтому сначала выбирается область, потом её цепочки.
      </p>
    </div>

    <ul class="cards">
      {#each shownCards as card (card.name || '—')}
        <li>
          <button type="button" class="card" onclick={() => openCommunity(card.name || MAP_ALL_COMMUNITIES)}>
            <span class="card__name">{card.name || 'Без названия'}</span>
            <span class="card__counts">
              <span><span class="num">{card.claims}</span> выводов</span>
              <span><span class="num">{card.sources}</span> источников</span>
              <span><span class="num">{card.entities}</span> сущностей</span>
              <span><span class="num">{card.links}</span> связей внутри</span>
            </span>
            {#if card.name === MAP_ALL_COMMUNITIES}
              <span class="micro muted">все {graph.nodes.length} узлов среза</span>
            {/if}
            <span class="card__go">Открыть цепочки <Icon name="chevronRight" size={15} /></span>
          </button>
        </li>
      {/each}
    </ul>

    {#if restCards > 0}
      <Button variant="quiet" size="sm" onclick={() => (cardsCap += CARD_PAGE)}>
        Показать ещё сообщества
        <span class="num">{countOf(Math.min(CARD_PAGE, restCards), 'кластер', 'кластера', 'кластеров')}</span>
      </Button>
    {/if}

    <div class="map__head map__head--sub">
      <h3 class="h4">Крупнейшие узлы среза</h3>
      <p class="micro muted">Узлы с наибольшим числом связей: у них самая длинная цепочка.</p>
    </div>
    <ul class="starters">
      {#each starters as row (row.node.id)}
        <li>
          <button type="button" class="starter" onclick={() => openNode(row.node)}>
            <span class="grow">{row.node.label}</span>
            <span class="micro muted">{mapNodeType(row.node.type)}</span>
            <span class="num">{countOf(row.links, 'связь', 'связи', 'связей')}</span>
          </button>
        </li>
      {/each}
    </ul>
  {:else}
    <!-- Уровень 2: цепочки выбранной области. -->
    <div class="map__bar">
      <nav class="map__path" aria-label="Обзор карты">
        {#if hasChoice}
          <button type="button" class="map__path-back" onclick={backToOverview}>
            <Icon name="chevronLeft" size={15} />
            {MAP_OVERVIEW_TITLE}
          </button>
          <span aria-hidden="true">·</span>
        {/if}
        <p class="small">
          {activeCommunity || MAP_ALL_COMMUNITIES}
          <span class="micro muted">
            <span class="num">{countOf(filtered, 'узел', 'узла', 'узлов')}</span>
            в отборе
          </span>
        </p>
      </nav>

      <div class="map__find">
        <Icon name="search" size={17} />
        <input
          type="search"
          class="input"
          bind:value={query}
          placeholder="метка или тип узла"
          aria-label="Найти узел в этом сообществе по метке, типу или коду"
        />
      </div>

      {#if legend.length > 0}
        <div class="map__types" role="group" aria-label="Типы узлов: показать или скрыть">
          {#each legend as item (item.type)}
            <Chip pressed={item.on} onclick={() => toggleType(item.type)}>
              {item.label}
              <span class="num">{item.count}</span>
            </Chip>
          {/each}
        </div>
      {/if}
    </div>

    {#if filtered === 0}
      <Empty
        icon="filter"
        title="Ничего не отобрано"
        body="Все узлы этой области скрыты фильтром или выключенными типами."
      >
        {#snippet action()}
          <Button variant="quiet" size="sm" onclick={resetFilters}>Сбросить фильтры</Button>
        {/snippet}
      </Empty>
    {:else}
      {#if selectedNode}
        <div class="map__focus">
          <p class="small">
            Цепочка узла <b>{selectedNode.label}</b> ·
            <span class="num">{countOf(degree.get(selectedNode.id) ?? 0, 'связь', 'связи', 'связей')}</span>
            в срезе
          </p>
          <div class="row">
            <Chip pressed={chainOnly} onclick={() => (chainOnly = !chainOnly)}>
              {chainOnly ? 'Показать всю область' : 'Показать только цепочку'}
            </Chip>
            <Button variant="ghost" size="sm" onclick={() => select(null)}>Снять выбор</Button>
          </div>
        </div>
      {:else}
        <p class="map__hint">
          Выберите строку — её связи подсветятся, остальные отойдут на второй план.
        </p>
      {/if}

      <div class="chain-field" bind:this={fieldEl}>
        {#if !narrow}
          <svg
            class="chain-links"
            width={box.w}
            height={box.h}
            viewBox="0 0 {box.w} {box.h}"
            aria-hidden="true"
            focusable="false"
          >
            <defs>
              <marker
                id="nk-chain-arrow"
                markerWidth="10"
                markerHeight="10"
                refX="8.4"
                refY="5"
                orient="auto"
                markerUnits="userSpaceOnUse"
              >
                <path class="chain-arrow" d="M0.6 1 L9 5 L0.6 9 Z" />
              </marker>
              <marker
                id="nk-chain-arrow-on"
                markerWidth="12"
                markerHeight="12"
                refX="10.2"
                refY="6"
                orient="auto"
                markerUnits="userSpaceOnUse"
              >
                <path class="chain-arrow chain-arrow--on" d="M0.8 1.4 L11 6 L0.8 10.6 Z" />
              </marker>
            </defs>

            {#each links as row (row.edge.id)}
              <path
                class="chain-link"
                class:chain-link--on={row.active}
                class:chain-link--pick={row.picked}
                class:chain-link--dim={row.dim}
                d={row.d}
                marker-end={row.active || row.picked ? 'url(#nk-chain-arrow-on)' : 'url(#nk-chain-arrow)'}
              />
            {/each}
            {#each links as row (row.edge.id)}
              <path class="chain-hit" d={row.d} onclick={() => pick(row.edge)} aria-hidden="true" />
            {/each}
          </svg>
        {/if}

        <div class="chain-grid">
          {#each columns as column (column.layer)}
            <section class="chain-col" aria-label={column.label}>
              <header class="chain-col__head">
                <h3 class="h4">{column.label}<span class="num">{column.rows.length}</span></h3>
                <p class="micro muted">{column.gloss}</p>
              </header>

              {#if column.rows.length === 0}
                <p class="micro muted chain-col__none">В отборе узлов этого слоя нет.</p>
              {:else}
                {#each column.rows as node (node.id)}
                  <div
                    class="chain-item"
                    class:chain-item--on={selectedId === node.id}
                    class:chain-item--near={selectedNode != null && neighbourIds.has(node.id)}
                    class:chain-item--dim={selectedNode != null &&
                      selectedId !== node.id &&
                      !neighbourIds.has(node.id)}
                  >
                    <button
                      type="button"
                      class="chain-row"
                      data-chain-id={node.id}
                      aria-pressed={selectedId === node.id}
                      aria-label="{column.label}, {mapNodeType(node.type)}: {node.label}. Связей: {degree.get(node.id) ?? 0}."
                      onclick={() => select(node)}
                      onkeydown={(event) => onRowKeydown(event, node)}
                    >
                      <span class="chain-row__kind">{mapNodeType(node.type)}</span>
                      <span class="chain-row__label">{node.label}</span>
                      <span class="chain-row__meta">
                        <span class="micro muted">
                          {countOf(degree.get(node.id) ?? 0, 'связь', 'связи', 'связей')}
                        </span>
                        <span class="micro muted">уверенность <span class="num">{fmt(node.confidence)}</span></span>
                        {#if node.data_class !== 'public'}
                          <StatusPill status={classOf(node).pill} label={classOf(node).ru} />
                        {/if}
                      </span>
                    </button>

                    {#if narrow}
                      {@const rels = outgoing.get(node.id) ?? []}
                      {@const backs = incoming.get(node.id) ?? []}
                      {#if rels.length > 0 || backs.length > 0}
                        <ul class="chain-rels">
                          {#each rels as edge (edge.id)}
                            <li>
                              <button type="button" class="chain-rel" onclick={() => select(rowOf(edge.target))}>
                                <span class="chain-rel__word">{labelFor(edge)}</span>
                                <span aria-hidden="true">→</span>
                                <span class="chain-rel__to">
                                  {rowOf(edge.target)?.label ?? 'узел недоступен в этом срезе'}
                                </span>
                              </button>
                            </li>
                          {/each}
                          {#each backs as edge (edge.id)}
                            <li>
                              <button type="button" class="chain-rel" onclick={() => select(rowOf(edge.source))}>
                                <span aria-hidden="true">←</span>
                                <span class="chain-rel__word">{labelFor(edge)}</span>
                                <span class="chain-rel__to">
                                  {rowOf(edge.source)?.label ?? 'узел недоступен в этом срезе'}
                                </span>
                              </button>
                            </li>
                          {/each}
                        </ul>
                      {/if}
                    {/if}
                  </div>
                {/each}
              {/if}
            </section>
          {/each}
        </div>

        {#if !narrow}
          <div class="chain-marks">
            {#each marks as mark (mark.edge.id)}
              <button
                type="button"
                class="chain-mark"
                class:chain-mark--on={mark.picked}
                class:chain-mark--dim={mark.dim}
                aria-pressed={mark.picked}
                aria-label="Связь «{mark.text}»: {nodeById.get(mark.edge.source)?.label ?? ''} → {nodeById.get(mark.edge.target)?.label ?? ''}"
                style="left: {mark.x.toFixed(1)}px; top: {mark.y.toFixed(1)}px"
                onclick={() => pick(mark.edge)}
              >{mark.text}</button>
            {/each}
          </div>
        {/if}
      </div>

      <p class="sr-only" aria-live="polite" role="status">{announcement}</p>

      <div class="map__under">
        <p class="micro muted">
          в поле <span class="num">{shownCount}</span> из <span class="num">{filtered}</span>
          отобранных узлов · связей в области{' '}
          <span class="num">{scopeEdges.length}</span>, линий на поле{' '}
          <span class="num">{edgeSplit.drawn.length}</span>
          {#if edgeSplit.other.length > 0}
            · внутри колонки и через колонку — <span class="num">{edgeSplit.other.length}</span>
          {/if}
          {#if offFieldEdges > 0}
            · с узлом вне поля — <span class="num">{offFieldEdges}</span>
          {/if}
        </p>
        {#if restTotal > 0}
          <Button variant="quiet" size="sm" onclick={showMore}>
            Показать ещё
            <span class="num">{countOf(Math.min(PAGE, restTotal), 'узел', 'узла', 'узлов')}</span>
          </Button>
        {/if}
        {#if hasFilters}
          <Button variant="ghost" size="sm" onclick={resetFilters}>Сбросить фильтры</Button>
        {/if}
      </div>

      {#if edgeSplit.other.length > 0}
        <details class="acc map__other">
          <summary class="acc__head">
            <span>Связи внутри колонки и через колонку</span>
            <span class="num">{edgeSplit.other.length}</span>
          </summary>
          <div class="acc__body">
            <p class="micro muted">
              Такие связи не пересекают соседние колонки, поэтому на поле их не видно:
              они перечислены здесь и всегда читаются в списке связей выбранного узла.
            </p>
            <ul class="map__other-list">
              {#each edgeSplit.other as edge (edge.id)}
                <li>
                  <button type="button" class="map__jump" onclick={() => select(rowOf(edge.source))}>
                    {rowOf(edge.source)?.label ?? edge.source}
                  </button>
                  <span class="micro muted map__word">
                    {labelFor(edge)}
                    {#if !mapRelationKnown(edge.relation)}
                      <code class="tech">{edge.relation}</code>
                    {/if}
                    →
                  </span>
                  <button type="button" class="map__jump" onclick={() => select(rowOf(edge.target))}>
                    {rowOf(edge.target)?.label ?? edge.target}
                  </button>
                </li>
              {/each}
            </ul>
          </div>
        </details>
      {/if}

      <div class="map__legend">
        <p class="micro muted">
          Колонка — роль узла, строка — сам узел, линия — отношение: остриё смотрит в
          цель, имя отношения стоит на линии.
        </p>
        <details class="map__tech">
          <summary class="micro">Как читать поле и клавиатура</summary>
          <p class="map__keys micro">
            <span class="num">Tab</span> — по строкам ·
            <span class="num">← →</span> — между колонками ·
            <span class="num">↑ ↓</span> — по колонке ·
            <span class="num">Home</span> / <span class="num">End</span> — край колонки ·
            <span class="num">Enter</span> — выбрать ·
            <span class="num">Esc</span> — снять
          </p>
        </details>
      </div>
    {/if}
  {/if}
</section>

<style>
  .map {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  .map__head {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
  }

  .map__head h3 {
    margin: 0;
  }

  .map__head p {
    margin: 0;
    max-width: 68ch;
  }

  .map__head--sub {
    margin-top: var(--s4);
  }

  /* ── Уровень 1: обзор ─────────────────────────────────────────────────── */
  .cards {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--s3);
    grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr));
  }

  .cards li {
    display: flex;
  }

  .card {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    width: 100%;
    padding: var(--s5);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    background: var(--surface);
    color: var(--ink);
    text-align: start;
    cursor: pointer;
    transition: border-color var(--dur-fast) var(--ease-soft),
      box-shadow var(--dur-fast) var(--ease-soft), transform var(--dur-fast) var(--ease-soft);
  }

  .card:hover {
    border-color: var(--line-strong);
    transform: translateY(-2px);
    box-shadow: var(--shadow-soft);
  }

  .card:focus-visible {
    outline: none;
    box-shadow: var(--shadow-focus);
  }

  .card__name {
    font-size: var(--t-h4);
    font-weight: 600;
    line-height: var(--lh-head);
    overflow-wrap: anywhere;
  }

  .card__counts {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s1) var(--s4);
    font-size: var(--t-micro);
    color: var(--ink-3);
  }

  .card__counts .num {
    font-family: var(--font-data);
    color: var(--ink-2);
  }

  .card__go {
    display: inline-flex;
    align-items: center;
    gap: var(--s1);
    margin-top: var(--s1);
    color: var(--action-ink);
    font-size: var(--t-small);
  }

  .starters {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }

  .starter {
    display: flex;
    align-items: center;
    gap: var(--s3);
    width: 100%;
    min-height: 44px;
    padding: var(--s2) 0;
    border: 0;
    border-top: 1px solid var(--line);
    background: none;
    color: var(--ink);
    font: inherit;
    font-size: var(--t-small);
    text-align: start;
    cursor: pointer;
    overflow-wrap: anywhere;
  }

  .starters li:first-child .starter {
    border-top: 0;
  }

  .starter:hover {
    color: var(--action-ink);
  }

  .starter .num {
    color: var(--ink-3);
    white-space: nowrap;
  }

  /* ── Уровень 2: полоса управления ─────────────────────────────────────── */
  .map__bar {
    display: flex;
    align-items: center;
    gap: var(--s4);
    flex-wrap: wrap;
  }

  .map__path {
    display: flex;
    align-items: center;
    gap: var(--s2);
    min-width: 0;
  }

  .map__path p {
    margin: 0;
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    font-weight: 600;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .map__path p span {
    font-weight: 400;
  }

  .map__path-back {
    display: inline-flex;
    align-items: center;
    gap: var(--s1);
    min-height: 44px;
    padding: 0 var(--s3);
    border: 1px solid var(--line);
    border-radius: var(--r-pill);
    background: var(--surface);
    color: var(--ink-2);
    font: inherit;
    font-size: var(--t-small);
    cursor: pointer;
  }

  .map__path-back:hover {
    color: var(--ink);
    border-color: var(--line-strong);
  }

  .map__find {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex: 1 1 240px;
    min-width: 200px;
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

  .map__types {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
  }

  .map__focus {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s4);
    flex-wrap: wrap;
    padding: var(--s3) var(--s4);
    border-radius: var(--r-md);
    background: var(--surface);
    box-shadow: var(--shadow-inset);
  }

  .map__focus p {
    margin: 0;
    min-width: 0;
  }

  .map__hint {
    margin: 0;
    font-size: var(--t-small);
    color: var(--ink-3);
  }

  /* ── Поле: колонки и линии ────────────────────────────────────────────── */
  /* Поле растёт вместе с содержимым: внутреннего скролла нет, прокрутку ведёт
     документ. */
  .chain-field {
    position: relative;
    padding: var(--s5);
    border-radius: var(--r-xl);
    border: 1px solid var(--line-soft);
    background: var(--surface-sunk);
  }

  .chain-links {
    position: absolute;
    inset: 0;
    pointer-events: none;
    overflow: visible;
  }

  /* Три колонки, промежутки — это column-gap: линии рисуются поверх них.
     Раньше пустые дорожки чередовались с колонками, и автопосстановление
     ставило вторую колонку в дорожку промежутка. */
  .chain-grid {
    position: relative;
    display: grid;
    grid-template-columns: minmax(0, 0.92fr) minmax(0, 1.3fr) minmax(0, 0.92fr);
    column-gap: clamp(88px, 10vw, 168px);
    align-items: start;
  }

  .chain-col {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    min-width: 0;
  }

  .chain-col__head {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding-block-end: var(--s2);
  }

  .chain-col__head h3 {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    margin: 0;
    font-size: var(--t-small);
    font-weight: 600;
    letter-spacing: var(--tr-head);
    color: var(--ink-2);
  }

  .chain-col__head .num {
    font-family: var(--font-data);
    font-size: var(--t-micro);
    font-weight: 400;
    color: var(--ink-3);
  }

  .chain-col__head p {
    margin: 0;
  }

  .chain-col__none {
    margin: 0;
    padding: var(--s3);
    border-radius: var(--r-md);
    background: var(--surface);
  }

  .chain-item {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
    min-width: 0;
  }

  .chain-row {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    width: 100%;
    min-width: 0;
    padding: var(--s3) var(--s4);
    border: 1px solid var(--line);
    border-radius: var(--r-md);
    background: var(--surface);
    color: var(--ink);
    text-align: start;
    cursor: pointer;
    transition: border-color var(--dur-fast) var(--ease-soft),
      box-shadow var(--dur-fast) var(--ease-soft), transform var(--dur-fast) var(--ease-soft);
  }

  .chain-row:hover {
    border-color: var(--line-strong);
    transform: translateY(-1px);
    box-shadow: var(--shadow-soft);
  }

  .chain-row:focus-visible {
    outline: none;
    box-shadow: var(--shadow-focus);
  }

  .chain-row__kind {
    font-size: var(--t-micro);
    color: var(--ink-3);
  }

  .chain-row__label {
    font-size: var(--t-body);
    line-height: var(--lh-dense);
    font-weight: 500;
    overflow-wrap: anywhere;
  }

  .chain-row__meta {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .chain-row__meta .num {
    font-family: var(--font-data);
  }

  .chain-item--on .chain-row {
    border-color: var(--action-ink);
    box-shadow: 0 0 0 1px var(--action-ink), var(--shadow-lift);
  }

  .chain-item--near .chain-row {
    border-color: var(--edge-active);
  }

  .chain-item--dim {
    opacity: 0.45;
  }

  .chain-link {
    fill: none;
    stroke: var(--edge);
    stroke-width: 1.6px;
    stroke-linecap: round;
    transition: stroke var(--dur-fast) var(--ease-soft),
      stroke-width var(--dur-fast) var(--ease-soft), opacity var(--dur-fast) var(--ease-soft);
  }

  .chain-link--on,
  .chain-link--pick {
    stroke: var(--edge-active);
  }

  .chain-link--on {
    stroke-width: 2.6px;
  }

  .chain-link--pick {
    stroke-width: 3.4px;
  }

  .chain-link--dim {
    stroke: var(--edge-soft);
    opacity: 0.55;
  }

  .chain-arrow {
    fill: var(--edge);
  }

  .chain-arrow--on {
    fill: var(--edge-active);
  }

  .chain-hit {
    fill: none;
    stroke: transparent;
    stroke-width: 22px;
    pointer-events: stroke;
    cursor: pointer;
  }

  .chain-marks {
    position: absolute;
    inset: 0;
    pointer-events: none;
  }

  .chain-mark {
    position: absolute;
    transform: translate(-50%, -50%);
    max-width: clamp(76px, 9vw, 150px);
    padding: 2px var(--s3);
    border: 1px solid var(--line);
    border-radius: var(--r-pill);
    background: var(--surface);
    color: var(--ink-2);
    font-family: var(--font-ui);
    font-size: var(--t-micro);
    line-height: 1.3;
    text-align: center;
    overflow-wrap: anywhere;
    cursor: pointer;
    pointer-events: auto;
    box-shadow: var(--shadow-soft);
  }

  .chain-mark:hover {
    color: var(--ink);
    border-color: var(--line-strong);
  }

  .chain-mark--on {
    color: var(--ink);
    border-color: var(--action-ink);
    box-shadow: var(--shadow-lift);
  }

  .chain-mark--dim {
    opacity: 0.3;
  }

  .map__under {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .map__under p {
    margin: 0;
  }

  .map__other-list {
    list-style: none;
    margin: var(--s2) 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--s1);
  }

  .map__other-list li {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
    font-size: var(--t-small);
  }

  .map__word {
    color: var(--ink-3);
  }

  .map__jump {
    border: 0;
    border-radius: var(--r-xs);
    padding: 0;
    background: none;
    color: var(--ink);
    font: inherit;
    text-align: start;
    cursor: pointer;
    overflow-wrap: anywhere;
  }

  .map__jump:hover {
    color: var(--action-ink);
  }

  .map__legend {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  .map__legend p {
    margin: 0;
    max-width: 68ch;
  }

  .map__tech {
    border-top: 1px solid var(--line-soft);
    padding-top: var(--s3);
  }

  .map__tech summary {
    cursor: pointer;
    color: var(--ink-3);
  }

  .map__keys {
    margin: var(--s2) 0 0;
    color: var(--ink-3);
    line-height: 1.9;
  }

  .map__keys .num {
    padding: 2px var(--s2);
    border-radius: var(--r-xs);
    background: var(--surface-sunk);
    color: var(--ink-2);
  }

  /* Отношения под строкой — вертикальное чтение на узком экране. */
  .chain-rels {
    list-style: none;
    margin: 0;
    padding: 0 0 0 var(--s4);
    display: flex;
    flex-direction: column;
    gap: 2px;
    border-inline-start: 1px solid var(--line);
  }

  .chain-rel {
    display: block;
    width: 100%;
    min-height: 44px;
    padding: var(--s2);
    border: 0;
    border-radius: var(--r-xs);
    background: none;
    color: var(--ink);
    font-size: var(--t-small);
    line-height: var(--lh-dense);
    text-align: start;
    cursor: pointer;
  }

  .chain-rel:hover {
    background: var(--surface);
  }

  /* Отношение и второй узел — две строки: на ширине телефона имя отношения
  	  вместе с меткой не помещается, и метка разваливалась по буквам. */
  .chain-rel__word {
    color: var(--ink-3);
  }

  .chain-rel__to {
    display: block;
    min-width: 0;
    overflow-wrap: break-word;
  }

  @media (max-width: 900px) {
    .chain-grid {
      grid-template-columns: minmax(0, 1fr);
      row-gap: var(--s5);
    }

    .chain-field {
      padding: var(--s4);
    }
  }
</style>
