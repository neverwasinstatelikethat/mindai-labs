<script module lang="ts">
  // Разбор класса данных узла — один на компонент и на страницу-инспектор:
  // русское имя приходит из контракта (DATA_CLASS_LABELS). Сами словари (типы
  // узлов, отношения, слои) живут в $lib/terms: локальных карт имён у карты нет.
  import { DATA_CLASS_LABELS, type DataClass, type GraphNode } from './types';

  export interface NodeClass {
    code: DataClass;
    ru: string;
  }

  export function classOf(node: GraphNode): NodeClass {
    return {
      code: node.data_class,
      ru: DATA_CLASS_LABELS[node.data_class],
    };
  }
</script>

<script lang="ts">
  // Карта связей читается за два хода.
  //
  // 1. Цепочки выбранной области: три колонки по роли записи — Источники,
  //    Утверждения, Сущности (единственный словарь колонок, см. terms/map.ts).
  //    Из порядка колонок следует главное: почти каждое отношение связывает
  //    соседние колонки, то есть пересекает ровно один промежуток. Длинных
  //    диагоналей через всё поле при такой раскладке не бывает.
  // 2. Обзор областей: названные множества связанных записей (их считает
  //    сервер, числа берутся из этого же среза). Уровня «весь корпус» у экрана
  //    нет ни по клику, ни с клавиатуры, ни по ссылке: на поле всегда лежит
  //    одна названная область.
  //
  // Экран открывается первым ходом: поле связей видно сразу, обзор областей
  // остаётся вторым нажатием в той же полосе переключения.
  //
  // Поиск стоит строкой над полем на обоих уровнях и не является режимом:
  // совпадение фильтрует поле и печатается списком, а клик по нему ведёт в
  // область найденной записи. Отдельного фильтра колонок над полем больше нет:
  // один запрос живёт в одном месте.
  //
  // Запись — строка с полным названием, связь — линия с остриём у цели и
  // русским именем на линии. Чем является знак, подписано в легенде у поля, а
  // не только за «i». Якоря линий измеряются в живом DOM, а не вычисляются из
  // сетки: прежнее поле привязывало конец штриха к центру кнопки, тогда как
  // видимый диск лежал на 13–23 px выше, и связи уходили в пустоту.
  //
  // Выбранный объект подсвечивается на поле, повторный клик по той же строке
  // снимает выбор. Детали открывает не карта: колонка сведений стоит рядом с
  // полем, и поля карты её не перекрывают.
  //
  // Масштаба, перетаскивания и внутреннего скролла нет: поле растёт с
  // содержимым, прокрутку ведёт документ. Плотность регулируется областью,
  // отбором и «Показать ещё».
  //
  // Методика (способ чтения поля, клавиатура, причина списка связей без линии)
  // живёт за «i» (InfoDot). Легенда знаков при этом остаётся у поля: способ
  // чтения не имеет права быть единственной подсказкой. Текстовое чтение
  // обязательно и не зависит от поля: на узком экране всё поле читается одной
  // колонкой, где под записью идут её связи по строке, без карточек и без
  // решётки кнопок.
  // Клавиатура: Tab по строкам, меткам связей и списку под полем, ← → между
  // колонками, ↑ ↓ по колонке, Home/End край колонки, Enter выбрать, Esc снять.
  // У каждой нарисованной линии есть метка-кнопка (слово или точка), поэтому
  // связь, доступная кликом по линии, доступна и с клавиатуры.
  import { tick } from 'svelte';
  // GraphNode уже импортирован в <script module> — то же пространство имён.
  import type { GraphEdge, GraphSnapshot } from './types';
  import { countOf, num, plural } from './format';
  import {
    MAP_FIELD_SIGNS,
    MAP_FIND,
    MAP_INFO_AREAS,
    MAP_INFO_FIELD,
    MAP_INFO_KEYS,
    MAP_INFO_STARTERS,
    MAP_INSPECTOR,
    MAP_LAYER_GLOSS,
    MAP_LAYER_LABELS,
    MAP_LAYER_ORDER,
    MAP_LEVELS,
    MAP_LARGEST_AREA,
    MAP_NODE_TYPE_ORDER,
    MAP_NOUN,
    MAP_OVERVIEW_TITLE,
    MAP_UNNAMED_AREA,
    mapAreaName,
    mapLayerOf,
    mapNodeType,
    mapRelationLabel,
    mapSearchEmptyBody,
    mapSearchShownOf,
    type MapLayer,
  } from './terms';
  import Button from './ui/Button.svelte';
  import Disclosure from './ui/Disclosure.svelte';
  import Empty from './ui/Empty.svelte';
  import Icon from './ui/Icon.svelte';
  import InfoDot from './ui/InfoDot.svelte';
  import Segmented from './ui/Segmented.svelte';

  interface Props {
    graph: GraphSnapshot;
    selectedId?: string;
    /** Выбранная связь живёт на экране: поле подсвечивает её, инспектор описывает. */
    edgeId?: string;
    /** Узкий экран измеряет страница: второго matchMedia на карте нет. */
    narrow?: boolean;
    /**
     * Плотность подписей на линиях: `focus` — имена только у выбранной записи,
     * `all` — на всём поле. Экран задаёт её подписанным control-элементом.
     */
    labelMode?: 'all' | 'focus';
    onselect?: (node: GraphNode | null) => void;
    onpickEdge?: (edge: GraphEdge | null) => void;
  }

  // Совпадение в поиске и в фильтре читается одинаково на поле, в списке и в
  // счётчике. Ищут только человеческие названия: код записи и сырой ключ типа
  // в совпадении не участвуют.
  function searchHit(node: GraphNode, q: string): boolean {
    if (!q) return false;
    return (
      node.label.toLowerCase().includes(q) ||
      mapNodeType(node.type).toLowerCase().includes(q)
    );
  }

  let {
    graph,
    selectedId,
    edgeId,
    narrow = false,
    labelMode = 'focus',
    onselect,
    onpickEdge,
  }: Props = $props();

  /** Подписи на всех линиях или только у выбранной записи: режим задаёт экран. */
  const labelsAll = $derived(labelMode === 'all');

  // ── Правила поля ────────────────────────────────────────────────────────
  /**
   * Утверждений в окне. Соседних колонок берётся в полтора раза больше: у
   * утверждения обычно несколько источников и предметных записей. 8 строк при
   * ~3,5 связи на строку дают поле, где подписи ещё помещаются на своих линиях.
   */
  const PAGE = 8;
  const CARD_PAGE = 12;
  const STARTERS = 8;
  /** Столько связей печатается под строкой на узком экране; остальное по действию. */
  const REL_PAGE = 10;
  /** Столько связей читаются в списке под полем до «Показать все». */
  const LINK_PAGE = 10;
  /** Минимальный вертикальный шаг подписей в промежутке: ниже строки сливаются. */
  const MARK_GAP = 26;
  /** Смещение параллельных связей между одной парой записей. */
  const FAN = 9;
  /** Сколько строк поиска на обзоре показано; остальное называется числом. */
  const FIND_WINDOW = 40;

  const LAYER_INDEX: Record<MapLayer, number> = { source: 0, claim: 1, entity: 2 };

  // ── Состояние ───────────────────────────────────────────────────────────
  /**
   * Экран открывается цепочками: поле связей видно сразу, а обзор областей
   * остаётся вторым нажатием в той же полосе. Прежний вход обзором заставлял
   * сначала выбрать область, чтобы увидеть хоть какие-то связи.
   */
  let level = $state<'overview' | 'chain'>('chain');
  let levelChosen = false;
  /**
   * Область поля, выбранная человеком. Пустая строка означает «ещё не выбрана»,
   * и тогда поле берёт крупнейшую названную область: уровня «весь корпус» у
   * экрана нет ни по клику, ни с клавиатуры, ни по ссылке.
   */
  let chosenArea = $state('');
  /**
   * Один поисковый запрос на весь экран: строка над полем. Набросок уходит в
   * отбор после паузы ввода, а не на каждый символ, и тот же запрос фильтрует
   * колонки поля.
   */
  let findDraft = $state('');
  let findQuery = $state('');
  let hiddenTypes = $state<Set<string>>(new Set());
  let cap = $state(PAGE);
  let cardsCap = $state(CARD_PAGE);
  let chainOnly = $state(false);
  let announcement = $state('');
  let fieldEl = $state<HTMLDivElement | null>(null);
  let box = $state({ w: 0, h: 0 });
  /** Строки узкого экрана, где человек раскрыл полный список связей. */
  let openedRels = $state<Set<string>>(new Set());
  /** Раскрыт ли полный список связей области под полем. */
  let linksAll = $state(false);

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
    /** Печатать ли слово на метке, или оставить только точку связи. */
    word: boolean;
    picked: boolean;
    dim: boolean;
  }

  let links = $state<Link[]>([]);
  let marks = $state<Mark[]>([]);

  // ── Разбор карты ────────────────────────────────────────────────────────
  /** Область записи: человеческое имя с сервера либо «Область без названия». */
  function areaOf(node: GraphNode): string {
    return mapAreaName(node.metadata['community']);
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
   * Области: имена с сервера, числа из этого же среза. Связью области
   * считается та, у которой оба конца в ней: иначе карточка обещала бы
   * отношения, которые внутри области не найти.
   */
  interface Area {
    name: string;
    records: number;
    claims: number;
    sources: number;
    entities: number;
    links: number;
  }

  const areas = $derived.by<Area[]>(() => {
    const buckets = new Map<string, GraphNode[]>();
    for (const node of graph.nodes) {
      const key = areaOf(node);
      const list = buckets.get(key);
      if (list) list.push(node);
      else buckets.set(key, [node]);
    }
    const linkCount = new Map<string, number>();
    for (const edge of graph.edges) {
      const from = nodeById.get(edge.source);
      const to = nodeById.get(edge.target);
      if (!from || !to) continue;
      const key = areaOf(from);
      if (key !== areaOf(to)) continue;
      linkCount.set(key, (linkCount.get(key) ?? 0) + 1);
    }
    const rows = [...buckets.entries()].map(([name, nodes]) => ({
      name,
      records: nodes.length,
      claims: nodes.filter((node) => mapLayerOf(node.type) === 'claim').length,
      sources: nodes.filter((node) => mapLayerOf(node.type) === 'source').length,
      entities: nodes.filter((node) => mapLayerOf(node.type) === 'entity').length,
      links: linkCount.get(name) ?? 0,
    }));
    return rows.sort(
      (a, b) => b.claims - a.claims || b.records - a.records || a.name.localeCompare(b.name, 'ru'),
    );
  });

  /** Есть ли из чего выбирать: одна область — не обзор, а лишний клик. */
  const hasChoice = $derived(areas.length > 1);

  /**
   * Крупнейшая названная область: она же вход по умолчанию, когда человек
   * ничего не выбрал. Поле при этом остаётся ограниченным одной областью.
   */
  const largestArea = $derived(areas[0]?.name ?? MAP_UNNAMED_AREA);
  const activeArea = $derived(chosenArea || largestArea);

  /**
   * Уровень переключает одна полоса: обзор областей остаётся одним нажатием,
   * но экран открывается полем, а не списком карточек. Одна область — обзора
   * нет: показывать единственную карточку и заставлять кликать через неё
   * значит прятать связи за ничего не значащим шагом.
   */
  function setLevel(next: string): void {
    level = next === 'overview' ? 'overview' : 'chain';
    levelChosen = true;
  }

  const cards = $derived<Area[]>(areas);
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

  // ── Поиск над полем ──────────────────────────────────────────────────────
  /** Результат поиска: записи всей загруженной карты по совпадению названия. */
  const findMatches = $derived.by<GraphNode[]>(() => {
    const q = findQuery.trim().toLowerCase();
    if (q === '') return [];
    return graph.nodes.filter((node) => searchHit(node, q));
  });
  const findShown = $derived(findMatches.slice(0, FIND_WINDOW));
  /** Записи той же области, что на поле: переход в них не меняет поле. */
  const findInArea = $derived(
    findMatches.filter((node) => areaOf(node) === activeArea).length,
  );

  function clearFind(): void {
    findDraft = '';
    findQuery = '';
  }

  /** Совпадение: в него входят и переход в его область, и выбор самой записи. */
  function openFound(node: GraphNode): void {
    openNode(node);
  }

  // ── Отбор поля ──────────────────────────────────────────────────────────
  function matches(node: GraphNode, q: string): boolean {
    if (hiddenTypes.has(node.type)) return false;
    return !q || searchHit(node, q);
  }

  /** Цепочка выбранной записи ограничивает видимую область карты. */
  const chainIds = $derived.by<Set<string> | null>(() => {
    if (!chainOnly || !selectedNode) return null;
    const ids = new Set<string>([selectedNode.id]);
    for (const id of neighbours.get(selectedNode.id) ?? []) ids.add(id);
    return ids;
  });

  /** Записи выбранной области до поиска: из них же строятся фильтры видов. */
  const areaNodes = $derived.by<GraphNode[]>(() =>
    graph.nodes.filter(
      (node) => areaOf(node) === activeArea && (!chainIds || chainIds.has(node.id)),
    ),
  );

  const scopedNodes = $derived.by<GraphNode[]>(() => {
    const q = findQuery.trim().toLowerCase();
    return areaNodes.filter((node) => matches(node, q));
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
    const seen = new Set(areaNodes.map((node) => node.type));
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
   * Окно поля строится от утверждений наружу: сначала берутся `cap` утверждений
   * с наибольшим числом связей, а источники и предметные записи — только те,
   * что реально стоят рядом с этими утверждениями. Прежняя независимая
   * пагинация по колонкам давала 12 утверждений и 12 источников, которые друг
   * друга не касались: на области в 480 связей на поле рисовалось 23 линии, и
   * ряды висели порознь.
   *
   * При непустом поиске совпадения идут первыми в своей колонке и не режутся
   * потолком поля: найденную запись человек обязан увидеть сразу, а не после
   * «Показать ещё».
   *
   * Порядок внутри колонок — по среднему положению своего утверждения (один
   * проход барицентра), поэтому линии идут параллельно и почти не переплетаются.
   * Детерминировано: одна и та же область всегда рисуется одинаково.
   */
  const columns = $derived.by<Column[]>(() => {
    const q = findQuery.trim().toLowerCase();
    const buckets: Record<MapLayer, GraphNode[]> = { source: [], claim: [], entity: [] };
    for (const node of scopedNodes) buckets[mapLayerOf(node.type)].push(node);

    const weightOf = (node: GraphNode): number => degree.get(node.id) ?? 0;
    const hitOf = (node: GraphNode): number => (q && searchHit(node, q) ? 1 : 0);
    const hitTotal = (all: GraphNode[]): number => all.reduce((sum, n) => sum + hitOf(n), 0);

    const byWeight = (a: GraphNode, b: GraphNode): number =>
      weightOf(b) - weightOf(a) || a.label.localeCompare(b.label, 'ru');
    /** Совпадения поиска всегда впереди, дальше — по числу связей. */
    const byHit = (a: GraphNode, b: GraphNode): number => hitOf(b) - hitOf(a) || byWeight(a, b);
    /** Потолок колонки: под поиском он не режет совпадения. */
    const roomFor = (all: GraphNode[], page: number): number =>
      q ? Math.max(page, hitTotal(all)) : page;

    const claims = [...buckets.claim].sort(byHit);
    const claimRows = claims.slice(0, roomFor(claims, cap));
    const claimSet = new Set(claimRows.map((node) => node.id));
    const rank = new Map(claimRows.map((node, i) => [node.id, i]));

    /** Сколько связей запись имеет с видимыми утверждениями. */
    const toClaims = (node: GraphNode): number => {
      const near = neighbours.get(node.id);
      if (!near) return 0;
      let linked = 0;
      for (const id of near) if (claimSet.has(id)) linked += 1;
      return linked;
    };

    const barycenter = (node: GraphNode): number => {
      const near = neighbours.get(node.id);
      if (!near) return Number.POSITIVE_INFINITY;
      let sum = 0;
      let anchor = 0;
      for (const id of near) {
        const at = rank.get(id);
        if (at != null) {
          sum += at;
          anchor += 1;
        }
      }
      return anchor > 0 ? sum / anchor : Number.POSITIVE_INFINITY;
    };

    const neighbourCap = Math.ceil(cap * 1.5);
    const rowsFor = (layer: MapLayer): { rows: GraphNode[]; rest: number } => {
      const all = buckets[layer];
      if (layer === 'claim')
        return { rows: claimRows, rest: Math.max(0, all.length - claimRows.length) };
      // Связанные с видимыми утверждениями идут первыми: они и дают линии на поле.
      const sorted = [...all].sort(
        (a, b) =>
          hitOf(b) - hitOf(a) ||
          toClaims(b) - toClaims(a) ||
          barycenter(a) - barycenter(b) ||
          byWeight(a, b),
      );
      const rows = sorted
        .slice(0, roomFor(sorted, neighbourCap))
        .sort((a, b) => hitOf(b) - hitOf(a) || barycenter(a) - barycenter(b) || byWeight(a, b));
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

  const restTotal = $derived(columns.reduce((sum, column) => sum + column.rest, 0));

  /**
   * Узкий экран читает поле одной колонкой: порядок тот же, что и на широком,
   * но утверждения идут первыми, потому что вопрос экрана звучит «что заявлено
   * и откуда», а не «как разложены полосы».
   */
  const flatRows = $derived.by<GraphNode[]>(() => {
    const order: MapLayer[] = ['claim', 'source', 'entity'];
    const rows: GraphNode[] = [];
    for (const layer of order) {
      const column = columns.find((item) => item.layer === layer);
      if (column) rows.push(...column.rows);
    }
    return rows;
  });

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

      // Метка есть у каждой нарисованной линии: это и точка входа в связь для
      // клавиатуры (линия кликабельна, но не фокусируема), и место русского
      // имени. Без слова метка остаётся маленькой точкой.
      pending.push({ edge, gutter, x: (x0 + x1) / 2, y: (y0 + y1) / 2 });
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
          // Слово печатается только про выбранную запись (или на всей карте,
          // если человек сам попросил): подписи на каждой линии плотного поля
          // мешают найти то, ради чего на карту заходили.
          word:
            labelsAll ||
            row.edge.id === edgeId ||
            (selectedNode != null && incident.has(row.edge.id)),
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
      // Подписи печатает measure(): без переключения в ключе поле не перечитает
      // свои же метки, когда человек попросит имена на всех линиях.
      labelMode,
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

  /**
   * Поиск пересобирает список и поле не на каждый символ: без паузы каждое
   * нажатие перекладывало набор строк под курсором.
   */
  $effect(() => {
    const draft = findDraft;
    const timer = setTimeout(() => {
      findQuery = draft;
    }, 250);
    return () => clearTimeout(timer);
  });

  /**
   * Выбор мог прийти с экрана, а не с поля: глубокая ссылка `/graph?claim=`
   * открывает запись до того, как человек вошёл в её область. Поле переходит в
   * эту область, иначе инспектор рассказывал бы о записи, которой на экране нет.
   * Один выбор обслуживается один раз: возврат к обзору областей при выбранной
   * записи остаётся возвратом, а не принудительным прыжком обратно в цепочку.
   */
  let revealedFor = '';
  $effect(() => {
    const id = selectedId;
    if (!id || id === revealedFor) return;
    const node = nodeById.get(id);
    if (!node) return;
    revealedFor = id;
    if (level === 'chain' && areaOf(node) === activeArea) return;
    openArea(areaOf(node));
  });

  // ── Действия ────────────────────────────────────────────────────────────
  /**
   * Выбор записи повторным кликом по той же строке снимается: человек
   * возвращается к полю тем же жестом, которым в него входил, и отдельная
   * кнопка на каждый случай не нужна.
   */
  function select(node: GraphNode | null): void {
    if (node && selectedId === node.id) {
      onselect?.(null);
      if (edgeId) onpickEdge?.(null);
      chainOnly = false;
      announcement = 'Выбор записи снят.';
      return;
    }
    onselect?.(node);
    if (node) {
      const count = degree.get(node.id) ?? 0;
      announcement = `Выбрана запись «${node.label}», ${mapNodeType(node.type)}. Связей: ${countOf(count, MAP_NOUN.link.one, MAP_NOUN.link.few, MAP_NOUN.link.many)}. Сведения открыты в колонке рядом с полем.`;
      return;
    }
    // Выбор снят: чип цепочки гаснет вместе с ним, иначе следующее нажатие
    // молча ограничивало бы поле.
    chainOnly = false;
    announcement = 'Выбор записи снят.';
  }

  /** Только цепочка выбранной записи: на поле остаётся она и её соседи. */
  function toggleChain(): void {
    chainOnly = !chainOnly;
    const left = neighbours.get(selectedNode?.id ?? '')?.size ?? 0;
    announcement = chainOnly
      ? `На поле цепочка выбранной записи: ${countOf(left + 1, MAP_NOUN.record.one, MAP_NOUN.record.few, MAP_NOUN.record.many)}.`
      : `На поле снова вся область «${activeArea}».`;
  }

  function pick(edge: GraphEdge): void {
    if (edgeId === edge.id) {
      onpickEdge?.(null);
      announcement = 'Выбор связи снят.';
      return;
    }
    onpickEdge?.(edge);
    const from = nodeById.get(edge.source)?.label ?? 'запись вне поля';
    const to = nodeById.get(edge.target)?.label ?? 'запись вне поля';
    announcement = `Связь «${mapRelationLabel(edge.relation)}»: «${from}» ведёт к «${to}». Описание связи открыто в колонке сведений.`;
  }

  /** Вход в область: из карточки обзора или из списка крупнейших записей. */
  function openArea(name: string): void {
    chosenArea = name;
    cap = PAGE;
    level = 'chain';
    levelChosen = true;
    announcement = `На поле область «${name}».`;
  }

  function openNode(node: GraphNode): void {
    openArea(areaOf(node));
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
    clearFind();
    hiddenTypes = new Set();
    announcement = 'Отбор снят: на поле снова вся область.';
  }

  /**
   * Отбор это поиск и виды записей. Потолок «Показать ещё» в счётчик не входит:
   * иначе «Сбросить отбор» всплывал сразу после добавления строк на поле.
   */
  const hasFilters = $derived(findDraft.trim() !== '' || hiddenTypes.size > 0);

  /**
   * Текстовое чтение поля: все связи области списком, а не только те, что легли
   * на линии. На линии встаёт отношение между соседними колонками, поэтому
   * «документ содержит фрагмент» и «документ упоминает запись» на поле не
   * рисуются никогда: они читаются здесь.
   */
  const readEdges = $derived.by<GraphEdge[]>(() => {
    const drawn = new Set(edgeSplit.drawn.map((edge) => edge.id));
    return scopeEdges.filter((edge) => !drawn.has(edge.id));
  });
  const shownLinks = $derived(
    linksAll ? readEdges : readEdges.slice(0, LINK_PAGE),
  );

  function rowOf(id: string): GraphNode | null {
    return nodeById.get(id) ?? null;
  }

  /** ← → переводят между колонками, ↑ ↓ — по колонке; Home/End — её край. */
  function move(current: GraphNode, dx: number, dy: number): boolean {
    // На узком экране колонок не видно: поле читается одним списком, и по нему
    // ходят вверх и вниз, а влево-вправо делать нечего.
    if (narrow) {
      const at = flatRows.findIndex((node) => node.id === current.id);
      if (at < 0) return false;
      const target = flatRows[at + (dy !== 0 ? dy : dx)];
      if (!target) return false;
      focusRow(target.id);
      return true;
    }
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
      if (narrow) {
        const target = event.key === 'Home' ? flatRows[0] : flatRows[flatRows.length - 1];
        if (target) {
          event.preventDefault();
          focusRow(target.id);
        }
        return;
      }
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
   * Связи строки для вертикального чтения. Берутся все связи области, а не
   * только нарисованные: у строки на узком экране линии нет, и прятать связь
   * потому, что её конец не лёг в поле, значит показывать меньше, чем есть.
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

  /**
   * Окно связей строки: десять строк читаются с телефона, стена на сотню
   * связей не читается ни с какого экрана. Полное число остаётся по действию.
   */
  function rowRels(node: GraphNode): { out: GraphEdge[]; back: GraphEdge[]; hidden: number } {
    const out = outgoing.get(node.id) ?? [];
    const back = incoming.get(node.id) ?? [];
    if (openedRels.has(node.id)) return { out, back, hidden: 0 };
    const head = out.slice(0, REL_PAGE);
    const tail = back.slice(0, Math.max(0, REL_PAGE - head.length));
    return {
      out: head,
      back: tail,
      hidden: out.length + back.length - head.length - tail.length,
    };
  }

  function openRowRels(id: string): void {
    openedRels = new Set(openedRels).add(id);
  }

  async function showMore(): Promise<void> {
    cap += PAGE;
    await tick();
    measure();
  }

  /** Сколько утверждений добавит следующая страница поля. */
  const moreClaims = $derived(
    Math.min(
      PAGE,
      columns.find((column) => column.layer === 'claim')?.rest ?? 0,
    ),
  );

  // Смена области обнуляет раскрытия прошлого поля: чужие списки не висят.
  $effect(() => {
    const area = activeArea;
    void area;
    linksAll = false;
    openedRels = new Set();
  });
</script>

<svelte:window
  onkeydown={(event) => {
    if (event.key !== 'Escape' || (!selectedNode && !edgeId)) return;
    // Esc снимает выбор целиком: сначала связь, открытую поверх записи, потом
    // и запись. Двух разных «закрыть» на экране нет.
    event.preventDefault();
    if (edgeId) {
      onpickEdge?.(null);
      announcement = 'Выбор связи снят.';
      return;
    }
    select(null);
  }}
/>

<section class="map" aria-label="Карта связей корпуса">
  <!-- Поиск стоит строкой над полем на обоих уровнях: поле связей открыто
       всегда, а совпадение ведёт в область найденной записи. Режимом экрана
       поиск больше не называется. -->
  <div class="map__findrow">
    <div class="map__find map__find--top">
      <Icon name="search" size={17} />
      <input
        type="search"
        class="input"
        bind:value={findDraft}
        placeholder={MAP_FIND.placeholder}
        aria-label={MAP_FIND.label}
      />
      {#if findDraft !== ''}
        <button type="button" class="map__find-clear" onclick={clearFind}>
          {MAP_FIND.clear}
        </button>
      {/if}
    </div>

    {#if findQuery.trim() !== ''}
      <!-- Слой совпадений над полем: число называет только то, что не влезло,
           а строка ведёт в область найденной записи. -->
      <div class="map__findlayer">
        {#if findMatches.length === 0}
          <p class="small map__find-empty">{mapSearchEmptyBody(findQuery.trim())}</p>
        {:else}
          <p class="micro muted">{mapSearchShownOf(findShown.length, findMatches.length, findQuery.trim())}</p>
          <ul class="map__found" aria-label={MAP_FIND.list}>
            {#each findShown as node (node.id)}
              <li>
                <button
                  type="button"
                  class="map__found-row"
                  aria-pressed={selectedId === node.id}
                  onclick={() => openFound(node)}
                >
                  <span class="map__found-type">{mapNodeType(node.type)}</span>
                  <span class="map__found-name">{node.label}</span>
                  {#if level === 'chain' && areaOf(node) !== activeArea}
                    <span class="micro muted map__found-elsewhere">{MAP_FIND.elsewhere}</span>
                  {/if}
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    {/if}
  </div>

  <!-- Уровень поля переключается той же полосой, что и фасеты находок: обзор
       областей и цепочки — два вида одного поля, а не два действия. -->
  {#if hasChoice}
    <Segmented
      class="map__levels"
      label={MAP_LEVELS.group}
      items={[
        { value: 'overview', label: MAP_LEVELS.overview },
        { value: 'chain', label: MAP_LEVELS.chain },
      ]}
      value={level}
      onchange={setLevel}
    />
  {/if}

  {#if level === 'overview'}
    <!-- Уровень 1: где искать. Экран остаётся одного размера и на 6 записях,
         и на 6 тысячах: дальше идут области, а не весь граф. -->
    <div class="map__head">
      <!-- Методика деления на области живёт за «i»: заголовок остаётся
           заголовком, а не подзаголовком о себе. -->
      <h2 class="h4 row">{MAP_OVERVIEW_TITLE}
        <InfoDot title={MAP_INFO_AREAS.title} body={MAP_INFO_AREAS.body} />
      </h2>
    </div>

    <ul class="cards">
      {#each shownCards as card, i (card.name)}
        <li>
          <button type="button" class="card" onclick={() => openArea(card.name)}>
            <span class="card__name">{card.name}</span>
            {#if i === 0 && hasChoice}
              <span class="card__tag">{MAP_LARGEST_AREA}</span>
            {/if}
            <span class="card__counts">
              <span>
                <span class="num">{card.claims}</span>
                {plural(card.claims, MAP_NOUN.claim.one, MAP_NOUN.claim.few, MAP_NOUN.claim.many)}
              </span>
              <span>
                {countOf(card.sources, MAP_NOUN.source.one, MAP_NOUN.source.few, MAP_NOUN.source.many)}
              </span>
              <span>
                <span class="num">{card.entities}</span>
                {plural(card.entities, 'предметная запись', 'предметные записи', 'предметных записей')}
              </span>
              <span>
                <span class="num">{card.links}</span>
                {plural(card.links, MAP_NOUN.link.one, MAP_NOUN.link.few, MAP_NOUN.link.many)}{' '}
                внутри
              </span>
            </span>
            <span class="card__go">Открыть цепочки <Icon name="chevronRight" size={15} /></span>
          </button>
        </li>
      {/each}
    </ul>

    {#if restCards > 0}
      <Button variant="quiet" size="sm" onclick={() => (cardsCap += CARD_PAGE)}>
        Показать ещё
        <span class="num">
          {countOf(
            Math.min(CARD_PAGE, restCards),
            MAP_NOUN.area.one,
            MAP_NOUN.area.few,
            MAP_NOUN.area.many,
          )}
        </span>
      </Button>
    {/if}

    <div class="map__head map__head--sub">
      <h2 class="h4 row">Крупнейшие записи карты
        <InfoDot title={MAP_INFO_STARTERS.title} body={MAP_INFO_STARTERS.body} />
      </h2>
    </div>
    <ul class="starters">
      {#each starters as row (row.node.id)}
        <li>
          <button type="button" class="starter" onclick={() => openNode(row.node)}>
            <span class="grow">{row.node.label}</span>
            <span class="micro muted">{mapNodeType(row.node.type)}</span>
            <span class="num">
              {countOf(row.links, MAP_NOUN.link.one, MAP_NOUN.link.few, MAP_NOUN.link.many)}
            </span>
          </button>
        </li>
      {/each}
    </ul>
  {:else}
    <!-- Уровень 2: цепочки выбранной области. Полоса отбора стоит над полем:
         человек настраивает список до того, как начинает его читать. -->
    <div class="map__bar" role="group" aria-label="Отбор записей на поле">
      <nav class="map__path" aria-label="Уровень карты">
        <h2 class="h4 map__path-name" title={activeArea}>{activeArea}</h2>
        <p class="micro muted map__path-count">
          в отборе <span class="num">
            {countOf(filtered, MAP_NOUN.record.one, MAP_NOUN.record.few, MAP_NOUN.record.many)}
          </span>
          {#if filtered !== areaNodes.length}
            из <span class="num">{num(areaNodes.length)}</span>
          {/if}
        </p>
      </nav>

      {#if legend.length > 1}
        <!-- Отбор по видам: строки с тоном-фоном, а не обведённые плашки. Все
             виды включены по умолчанию, поэтому гаснет только выключенное, и он
             назван словом, а не одним цветом. -->
        <ul class="map__types" role="group" aria-label="Виды записей на поле">
          {#each legend as item (item.type)}
            <li>
              <button
                type="button"
                class="map__type"
                class:map__type--off={!item.on}
                aria-pressed={item.on}
                onclick={() => toggleType(item.type)}
              >
                <span class="map__type-name">{item.label}</span>
                <span class="num">{item.count}</span>
                {#if !item.on}<span class="map__type-off">скрыт</span>{/if}
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </div>

    {#if filtered === 0}
      <Empty
        icon="filter"
        title="Под этот отбор записей не нашлось"
        body="Все записи области скрыты поиском или выключенными видами."
      >
        {#snippet action()}
          <Button variant="quiet" size="sm" onclick={resetFilters}>Сбросить отбор</Button>
        {/snippet}
      </Empty>
    {:else}
      {#if selectedNode}
        <div class="map__focus">
          <p class="small">
            Цепочка записи <b>{selectedNode.label}</b>:
            <span class="num">
              {countOf(
                degree.get(selectedNode.id) ?? 0,
                MAP_NOUN.link.one,
                MAP_NOUN.link.few,
                MAP_NOUN.link.many,
              )}
            </span>
            в этой области
          </p>
          <div class="row">
            <Button variant={chainOnly ? 'ink' : 'quiet'} current={chainOnly} onclick={toggleChain}>
              {chainOnly ? 'Показать всю область' : 'Показать только цепочку'}
            </Button>
            <!-- Повторный клик по выбранной строке снимает выбор, а «Снять
                 выбор» на экране один: он живёт в колонке сведений. Сюда до
                 колонки надо дотянуться, когда она не справа, а под полем. -->
            <span class="map__to-insp">
              <Button href="#inspector" variant="quiet" size="sm">{MAP_INSPECTOR.details}</Button>
            </span>
          </div>
        </div>
      {:else}
        <p class="map__hint">
          Выберите строку: её связи подсветятся, остальные отойдут на второй план.
        </p>
      {/if}

      <!-- Легенда знаков стоит у поля и всегда: чем является знак, человек
           видит до того, как начал искать ответ за «i». -->
      <ul class="map__signs" aria-label="Знаки поля">
        {#each MAP_FIELD_SIGNS as item (item.sign)}
          <li class="map__sign">
            <span class="map__sign-mark map__sign-mark--{item.sign}" aria-hidden="true"></span>
            {item.label}
          </li>
        {/each}
      </ul>

      <div class="chain-field" bind:this={fieldEl}>
        {#if narrow}
          <!-- Узкий экран: одна колонка. Запись, под ней её связи по строке,
               без карточек вокруг каждой строки и без решётки кнопок. Отношений
               на линиях здесь нет, поэтому связи печатаются словами. -->
          <ul class="chain-list">
            {#each flatRows as node (node.id)}
              {@const rels = rowRels(node)}
              {@const layer = MAP_LAYER_LABELS[mapLayerOf(node.type)]}
              <li
                class="chain-item"
                class:chain-item--on={selectedId === node.id}
                class:chain-item--near={selectedNode != null && neighbourIds.has(node.id)}
              >
                <button
                  type="button"
                  class="chain-row"
                  data-chain-id={node.id}
                  aria-pressed={selectedId === node.id}
                  aria-label="{layer}, {mapNodeType(node.type)}: {node.label}. Связей: {degree.get(node.id) ?? 0}."
                  onclick={() => select(node)}
                  onkeydown={(event) => onRowKeydown(event, node)}
                >
                  <span class="chain-row__kind">{mapNodeType(node.type)}</span>
                  <span class="chain-row__label">{node.label}</span>
                  <span class="micro muted">
                    {countOf(
                      degree.get(node.id) ?? 0,
                      MAP_NOUN.link.one,
                      MAP_NOUN.link.few,
                      MAP_NOUN.link.many,
                    )}
                  </span>
                </button>

                {#if rels.out.length > 0 || rels.back.length > 0}
                  <ul class="chain-rels">
                    {#each rels.out as edge (edge.id)}
                      <li>
                        <button
                          type="button"
                          class="chain-rel"
                          aria-pressed={edgeId === edge.id}
                          onclick={() => pick(edge)}
                        >
                          <span class="chain-rel__word">{labelFor(edge)}</span>
                          <span class="chain-rel__to">
                            ведёт к: {rowOf(edge.target)?.label ?? 'запись вне поля'}
                          </span>
                        </button>
                      </li>
                    {/each}
                    {#each rels.back as edge (edge.id)}
                      <li>
                        <button
                          type="button"
                          class="chain-rel"
                          aria-pressed={edgeId === edge.id}
                          onclick={() => pick(edge)}
                        >
                          <span class="chain-rel__word">{labelFor(edge)}</span>
                          <span class="chain-rel__to">
                            исходит от: {rowOf(edge.source)?.label ?? 'запись вне поля'}
                          </span>
                        </button>
                      </li>
                    {/each}
                  </ul>
                  {#if rels.hidden > 0}
                    <Button variant="ghost" size="sm" onclick={() => openRowRels(node.id)}>
                      Показать остальные связи
                      <span class="num">
                        {countOf(
                          rels.hidden,
                          MAP_NOUN.link.one,
                          MAP_NOUN.link.few,
                          MAP_NOUN.link.many,
                        )}
                      </span>
                    </Button>
                  {/if}
                {/if}
              </li>
            {/each}
          </ul>
        {:else}
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

          <div class="chain-grid">
            {#each columns as column (column.layer)}
              <section class="chain-col" aria-label={column.label}>
                <header class="chain-col__head">
                  <h3 class="h4">{column.label}</h3>
                  <p class="chain-col__count micro muted">
                    показано <span class="num">{column.rows.length}</span>
                    {#if column.rest > 0}
                      из <span class="num">{column.rows.length + column.rest}</span>
                    {/if}
                  </p>
                  <p class="micro muted">{column.gloss}</p>
                </header>

                {#if column.rows.length === 0}
                  <p class="micro muted chain-col__none">В отборе записей этого слоя нет.</p>
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
                            {countOf(
                              degree.get(node.id) ?? 0,
                              MAP_NOUN.link.one,
                              MAP_NOUN.link.few,
                              MAP_NOUN.link.many,
                            )}
                          </span>
                          <!-- Дробную уверенность модели на строке поля не печатают:
                               у неё нет калиброванного смысла, она читается за «i»
                               в колонке сведений вместе с оговоркой о происхождении. -->
                          {#if node.data_class !== 'public'}
                            <!-- Класс данных — доступ, а не состояние проверки:
                                 тон статуса врал бы, что материал оспорен. -->
                            <span class="chain-row__class">{classOf(node).ru}</span>
                          {/if}
                        </span>
                      </button>
                    </div>
                  {/each}
                {/if}
              </section>
            {/each}
          </div>

          <div class="chain-marks">
            {#each marks as mark (mark.edge.id)}
              <button
                type="button"
                class="chain-mark"
                class:chain-mark--on={mark.picked}
                class:chain-mark--dim={mark.dim}
                class:chain-mark--dot={!mark.word}
                aria-pressed={mark.picked}
                aria-label="Связь «{mark.text}»: «{nodeById.get(mark.edge.source)?.label ?? 'запись вне поля'}» ведёт к «{nodeById.get(mark.edge.target)?.label ?? 'запись вне поля'}»"
                style="left: {mark.x.toFixed(1)}px; top: {mark.y.toFixed(1)}px"
                onclick={() => pick(mark.edge)}
              >{mark.word ? mark.text : ''}</button>
            {/each}
          </div>
        {/if}
      </div>

      <p class="sr-only" aria-live="polite" role="status">{announcement}</p>

      <!-- Слой под полем живёт только ради своих действий: когда потолок
           отрисовки не упёрся и отбора нет, пустая полоса не раздвигает
           вертикаль поля. -->
      {#if restTotal > 0 || hasFilters}
        <div class="map__under">
          {#if restTotal > 0}
            <Button variant="quiet" size="sm" onclick={showMore}>
              Показать ещё
              <span class="num">
                {countOf(moreClaims, MAP_NOUN.claim.one, MAP_NOUN.claim.few, MAP_NOUN.claim.many)}
              </span>
              и их соседей
            </Button>
          {/if}
          {#if hasFilters}
            <Button variant="ghost" size="sm" onclick={resetFilters}>Сбросить отбор</Button>
          {/if}
        </div>
      {/if}

      {#if readEdges.length > 0 && !narrow}
        <!-- Раскрытие ведёт примитив: нативный маркер рисует браузер, и он не
             наследует ни кегль, ни ритм системы. На узком экране список не
             нужен: там связи каждой строки уже напечатаны под ней. -->
        <Disclosure
          id="map-other-links"
          title="Связи области без линии на поле"
          summary={countOf(
            readEdges.length,
            MAP_NOUN.link.one,
            MAP_NOUN.link.few,
            MAP_NOUN.link.many,
          )}
          bodyClass="map__other"
        >
          <p class="micro muted">
            На линию встаёт только отношение между соседними колонками: здесь
            остальные связи области.
          </p>
          <ul class="map__other-list">
            {#each shownLinks as edge (edge.id)}
              <li>
                <button type="button" class="map__jump" onclick={() => select(rowOf(edge.source))}>
                  {rowOf(edge.source)?.label ?? 'запись вне поля'}
                </button>
                <button
                  type="button"
                  class="map__word"
                  aria-pressed={edgeId === edge.id}
                  onclick={() => pick(edge)}
                >
                  <span>{labelFor(edge)}, ведёт к</span>
                </button>
                <button type="button" class="map__jump" onclick={() => select(rowOf(edge.target))}>
                  {rowOf(edge.target)?.label ?? 'запись вне поля'}
                </button>
              </li>
            {/each}
          </ul>
          {#if !linksAll && readEdges.length > shownLinks.length}
            <Button variant="quiet" size="sm" onclick={() => (linksAll = true)}>
              Показать все
              <span class="num">
                {countOf(
                  readEdges.length,
                  MAP_NOUN.link.one,
                  MAP_NOUN.link.few,
                  MAP_NOUN.link.many,
                )}
              </span>
            </Button>
          {/if}
        </Disclosure>
      {/if}

      <div class="map__legend">
        <!-- Способ чтения поля и клавиатура стоят за «i»: инлайн-методика не
             спорит с полем за внимание. -->
        <InfoDot title={MAP_INFO_FIELD.title} body={MAP_INFO_FIELD.body}>
          {#snippet children()}
            <dl class="kv map__keys">
              {#each MAP_INFO_KEYS as key (key.key)}
                <dt>{key.key}</dt>
                <dd>{key.action}</dd>
              {/each}
            </dl>
          {/snippet}
        </InfoDot>
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

  /* Заголовок обзорной шапки стоит без внешних полей: интервал между ним и
     подписью даёт только gap области. */
  .map__head h2 {
    margin: 0;
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

  /* Карточка области — объект: её держат тон, подъём и радиус, а не обводка.
     Обведённая плашка на поле из шести плашек не читалась как выбор. */
  .card {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    width: 100%;
    padding: var(--s5);
    border: 0;
    border-radius: var(--r-lg);
    background: var(--surface-raised);
    box-shadow: var(--shadow-soft);
    color: var(--ink);
    text-align: start;
    cursor: pointer;
    transition: box-shadow var(--dur-fast) var(--ease-soft),
      transform var(--dur-fast) var(--ease-soft);
  }

  .card:hover {
    transform: translateY(-2px);
    box-shadow: var(--shadow-lift);
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

  /* Метка крупнейшей области: она объясняет, почему вход по умолчанию ведёт
     именно сюда. Плашки не нужно: одно слово подписью под названием. */
  .card__tag {
    align-self: flex-start;
    color: var(--ink-3);
    font-size: var(--t-micro);
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

  /* Список крупнейших записей: строки держит расстояние, а не линия между
     ними. Разделитель живёт в плотной таблице, здесь таблицы нет. */
  .starters {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--s1);
  }

  .starter {
    display: flex;
    align-items: center;
    gap: var(--s3);
    width: 100%;
    min-height: 44px;
    padding: var(--s2) var(--s3);
    border: 0;
    border-radius: var(--r-sm);
    background: none;
    color: var(--ink);
    font: inherit;
    font-size: var(--t-small);
    text-align: start;
    cursor: pointer;
    overflow-wrap: anywhere;
    transition: background var(--dur-fast) var(--ease-soft);
  }

  .starter:hover {
    background: var(--surface-sunk);
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
    align-items: baseline;
    gap: var(--s2) var(--s3);
    flex-wrap: wrap;
    min-width: 0;
  }

  /* Название области — заголовок уровня, а не абзац: под ним читается счётчик.
     Сервер подписывает область и целой формулировкой утверждения: заголовок
     держит две строки, полное имя остаётся в подсказке и в карточке обзора. */
  .map__path h2 {
    margin: 0;
    min-width: 0;
    max-width: min(100%, 46ch);
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    overflow: hidden;
  }

  .map__path p {
    margin: 0;
    display: flex;
    align-items: baseline;
    gap: var(--s1);
    font-weight: 400;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  /* Возврат к обзору — действие внутри полосы поля: его держит тон-фон, а не
     обводка. Двойная обводка вокруг кнопки и вокруг поля не читалась бы. */
  .map__path-back {
    display: inline-flex;
    align-items: center;
    gap: var(--s1);
    min-height: 44px;
    padding: 0 var(--s4);
    border: 0;
    border-radius: var(--r-pill);
    background: var(--surface-sunk);
    color: var(--ink-2);
    font: inherit;
    font-size: var(--t-small);
    cursor: pointer;
    align-self: center;
    transition: background var(--dur-fast) var(--ease-soft),
      color var(--dur-fast) var(--ease-soft);
  }

  .map__path-back:hover {
    color: var(--ink);
    background: var(--surface);
  }

  /* Строка поиска над полем: шире всего поля, потому что поиск относится ко
     всему экрану, а не к одной его колонке. */
  .map__findrow {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  .map__find {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex: 1 1 240px;
    min-width: 0;
    padding-inline: var(--s4);
    border-radius: var(--r-pill);
    background: var(--surface-sunk);
    color: var(--ink-3);
  }

  .map__find--top {
    flex: none;
    width: 100%;
    padding-block: var(--s1);
  }

  .map__find .input {
    border: 0;
    background: none;
    box-shadow: none;
    min-height: 44px;
    padding-inline-start: 0;
  }

  /* Сброс поиска — текстовое действие, а не вторая кнопка рядом с полем ввода. */
  .map__find-clear {
    flex: none;
    border: 0;
    background: none;
    padding: var(--s2) 0;
    color: var(--action-ink);
    font: inherit;
    font-size: var(--t-small);
    cursor: pointer;
  }

  .map__find-clear:hover {
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  /* Слой совпадений: поднятый объект поверх поля, а не ещё одна секция. */
  .map__findlayer {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    padding: var(--s4);
    border-radius: var(--r-lg);
    background: var(--surface-raised);
    box-shadow: var(--shadow-soft);
  }

  .map__findlayer p {
    margin: 0;
  }

  .map__find-empty {
    color: var(--ink-2);
  }

  .map__found {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }

  .map__found-row {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    width: 100%;
    min-height: 44px;
    padding: var(--s2) var(--s3);
    border: 0;
    border-radius: var(--r-sm);
    background: none;
    color: var(--ink);
    font: inherit;
    font-size: var(--t-small);
    text-align: start;
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-soft);
  }

  .map__found-row:hover {
    background: var(--surface-sunk);
  }

  .map__found-row[aria-pressed='true'] {
    background: var(--sage);
  }

  .map__found-type {
    flex: none;
    color: var(--ink-3);
    font-size: var(--t-micro);
  }

  .map__found-name {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .map__found-elsewhere {
    flex: none;
    margin-inline-start: auto;
    color: var(--ink-3);
  }

  /* Отбор по видам записей: строки с тоном-фоном. Обведённые плашки фильтров
     запрещены: они удваивали границу там, где нужно одно слово с числом. */
  .map__types {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s1);
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .map__types li {
    display: flex;
  }

  /* Ряд фильтров читается списком слов, а не рядом плашек: закрашено только
     исключение, поэтому глаз сразу цепляет то, что спрятано. */
  .map__type {
    display: inline-flex;
    align-items: center;
    gap: var(--s2);
    min-height: 36px;
    padding: 0 var(--s3);
    border: 0;
    border-radius: var(--r-sm);
    background: none;
    color: var(--ink-2);
    font: inherit;
    font-size: var(--t-small);
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-soft),
      color var(--dur-fast) var(--ease-soft);
  }

  .map__type:hover {
    background: var(--surface-sunk);
    color: var(--ink);
  }

  .map__type .num {
    font-family: var(--font-data);
    font-weight: 600;
    color: inherit;
  }

  /* Выключенный вид гаснет и называется словом: один только цвет не решение,
     а обводка вокруг плашки вернула бы двойную границу на поле. */
  .map__type--off {
    background: var(--surface-sunk);
    color: var(--ink-4);
  }

  .map__type-off {
    font-size: var(--t-micro);
    font-weight: 500;
    color: var(--ink-4);
  }

  /* Полоса выбранной записи держится тоном и подъёмом: тень-обводка
     `--shadow-inset` рисовала рамку вокруг строки действий. */
  .map__focus {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s4);
    flex-wrap: wrap;
    padding: var(--s3) var(--s4);
    border-radius: var(--r-md);
    background: var(--surface-raised);
    box-shadow: var(--shadow-soft);
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

  /* Прыжок к колонке сведений нужен только там, где колонка стоит под полем:
     на широком экране она справа и всегда перед глазами. */
  .map__to-insp {
    display: none;
  }

  @media (min-width: 900px) and (max-width: 1119px) {
    .map__to-insp {
      display: inline-flex;
    }
  }

  /* Легенда знаков у поля: каждый знак нарисован тем же, чем он стоит на поле,
     поэтому подпись не просит верить ей на слово. */
  .map__signs {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--s2) var(--s5);
    margin: 0;
    padding: 0;
    list-style: none;
    font-size: var(--t-micro);
    color: var(--ink-3);
  }

  .map__sign {
    display: inline-flex;
    align-items: center;
    gap: var(--s2);
  }

  .map__sign-mark {
    flex: none;
    display: inline-block;
  }

  /* Строка означает запись: короткий отрезок поверхности с тенью. */
  .map__sign-mark--row {
    width: 22px;
    height: 8px;
    border-radius: var(--r-xs);
    background: var(--surface-raised);
    box-shadow: var(--shadow-soft);
  }

  /* Линия означает связь. */
  .map__sign-mark--line {
    width: 24px;
    height: 2px;
    border-radius: var(--r-pill);
    background: var(--edge);
  }

  /* Остриё показывает направление к цели. Знак вырезан формой, а не собран из
     рамок: рамка здесь была бы декором, а не границей взаимодействия. */
  .map__sign-mark--arrow {
    width: 10px;
    height: 10px;
    background: var(--edge-active);
    clip-path: polygon(0 0, 100% 50%, 0 100%);
  }

  /* Точка означает связь без подписи. */
  .map__sign-mark--dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--edge);
  }

  /* ── Поле: колонки и линии ────────────────────────────────────────────── */
  /* Поле растёт вместе с содержимым: внутреннего скролла нет, прокрутку ведёт
     документ. Границу поля держит тон, а не обводка. */
  .chain-field {
    position: relative;
    padding: var(--s5);
    border-radius: var(--r-xl);
    background: var(--surface-sunk);
    /* Дорожка между колонками и подпись связи на линии — одно и то же число.
       Пока метка могла быть шире промежутка, она наезжала на карточку и
       закрывала первые буквы утверждения. Низ в 112 px держит самое длинное
       русское слово отношения целиком: меньше — и подпись начнёт рваться
       по буквам. */
    --chain-gap: clamp(112px, 9vw, 128px);
  }

  .chain-links {
    position: absolute;
    inset: 0;
    pointer-events: none;
    overflow: visible;
  }

  /* Три колонки, промежутки — это column-gap: линии рисуются поверх них.
     Раньше пустые дорожки чередовались с колонками, и автопосстановление
     ставило вторую колонку в дорожку промежутка. Дорожка держит только подпись
     связи на линии, а не половину поля: при 144 px на колонку утверждений
     оставалось 240 px, и карточка вырастала в шесть строк. */
  .chain-grid {
    position: relative;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1.9fr) minmax(0, 1fr);
    column-gap: var(--chain-gap);
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
    margin: 0;
    font-size: var(--t-small);
    font-weight: 600;
    letter-spacing: var(--tr-head);
    color: var(--ink-2);
  }

  /* Число стоит отдельной строкой под заголовком: вклеенное в заголовок оно
     читалось как часть названия колонки. */
  .chain-col__count {
    display: flex;
    align-items: baseline;
    gap: var(--s1);
  }

  .chain-col__head .num {
    font-family: var(--font-data);
    font-weight: 400;
    color: var(--ink-2);
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

  /* Строка поля — объект, который можно выбрать: её держат тон и подъём.
     Обводка вокруг каждой из двадцати строк превращала поле в решётку рамок. */
  .chain-row {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    width: 100%;
    min-width: 0;
    padding: var(--s3) var(--s4);
    border: 0;
    border-radius: var(--r-md);
    background: var(--surface-raised);
    color: var(--ink);
    text-align: start;
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-soft),
      box-shadow var(--dur-fast) var(--ease-soft), transform var(--dur-fast) var(--ease-soft);
  }

  .chain-row:hover {
    transform: translateY(-1px);
    box-shadow: var(--shadow-lift);
  }

  .chain-row:focus-visible {
    outline: none;
    box-shadow: var(--shadow-focus);
  }

  .chain-row__kind {
    font-size: var(--t-micro);
    color: var(--ink-3);
  }

  /* Класс данных — слабее типа записи: он меняет доступ, а не смысл строки. */
  .chain-row__class {
    font-size: var(--t-micro);
    color: var(--ink-4);
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

  /* Выбор читается сценой, а не вторым контуром вокруг рамки: строка залита
     цветом поля и поднята. */
  .chain-item--on .chain-row {
    background: var(--sage-deep);
    box-shadow: var(--shadow-lift);
  }

  .chain-item--near .chain-row {
    background: var(--surface);
    box-shadow: var(--shadow-soft);
  }

  /* Второй план не гасят прозрачностью: вместе с ней текст строки падал до
     2.7:1, а подпись связи до 1.5:1. Гаснет токеном цвета, и текст остаётся
     не ниже нормы. */
  .chain-item--dim .chain-row {
    background: var(--surface-sunk);
  }

  .chain-item--dim .chain-row__label {
    color: var(--ink-2);
  }

  .chain-link {
    fill: none;
    stroke: var(--edge);
    stroke-width: 1.6px;
    stroke-linecap: round;
    transition: stroke var(--dur-fast) var(--ease-soft),
      stroke-width var(--dur-fast) var(--ease-soft);
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

  /* Затухание линии — токеном, а не прозрачностью: поверх пастельного поля
     полупрозрачный штрих уходил к 1.1:1 и переставал быть видимым. */
  .chain-link--dim {
    stroke: var(--edge-soft);
    stroke-width: 1.2px;
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

  /* Подпись связи на линии держится тоном и подъёмом: обводка вокруг слова
     стояла поверх линии и удваивала границу там, где знак один. Высота —
     32 px: метка стоит на линии и в неё надо попасть, не целясь в текст. */
  .chain-mark {
    position: absolute;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transform: translate(-50%, -50%);
    max-width: calc(var(--chain-gap) - var(--s2));
    min-height: 32px;
    padding: 0 var(--s2);
    border: 0;
    border-radius: var(--r-pill);
    background: var(--surface-raised);
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
    box-shadow: var(--shadow-lift);
  }

  /* Метка без слова: точка на линии. Доступ к связи не прячется у зрения.
     Цель — 32x32, видимый кружок 8 px рисует псевдоэлемент: на линии в
     точку меньше попасть труднее, чем в слово. */
  .chain-mark--dot {
    display: grid;
    place-items: center;
    width: 32px;
    height: 32px;
    max-width: 32px;
    padding: 0;
    background: transparent;
    box-shadow: none;
    border-radius: 50%;
  }

  .chain-mark--dot::after {
    content: '';
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--edge);
  }

  .chain-mark--dot:hover,
  .chain-mark--dot:focus-visible {
    background: var(--surface-raised);
    box-shadow: var(--shadow-soft);
  }

  .chain-mark--on {
    color: var(--ink);
    background: var(--lavender);
    box-shadow: var(--shadow-lift);
  }

  /* Подпись на линии гаснет токеном: с opacity 0.3 она читалась за 1.5:1 и
     исчезала целиком. */
  .chain-mark--dim {
    color: var(--ink-2);
    background: var(--surface-raised);
    box-shadow: none;
  }

  .map__under {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
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

  /* Слово связи в списке — действие: с клавиатуры оно выбирает связь, а не
     только перескакивает на соседнюю запись. Русское имя сверху, техническое
     подписью под ним. */
  .map__word {
    display: inline-flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0;
    padding: 0 var(--s2);
    border: 0;
    border-radius: var(--r-xs);
    background: none;
    color: var(--ink-2);
    font: inherit;
    font-size: var(--t-small);
    text-align: start;
    cursor: pointer;
  }

  .map__word:hover {
    color: var(--ink);
    background: var(--surface-sunk);
  }

  .map__word[aria-pressed='true'] {
    color: var(--ink);
    background: var(--sage);
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

  /* Раскладка клавиатуры читается парами «клавиша, действие»: список, а не
     строка с разделителями. */
  .map__keys {
    margin: var(--s2) 0 0;
    gap: var(--s1) var(--s5);
  }

  .map__keys dt {
    color: var(--ink-2);
    font-family: var(--font-data);
    font-size: var(--t-micro);
  }

  .map__keys dd {
    margin: 0;
    text-align: start;
    color: var(--ink-3);
  }

  /* Отношения под строкой — вертикальное чтение на узком экране. Отступ
     говорит о вложенности, линия-стержень здесь не нужна: связей у строки и
     так по одной строке. */
  .chain-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    position: relative;
  }

  .chain-rels {
    list-style: none;
    margin: 0;
    padding: 0 0 0 var(--s4);
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  /* Одна связь — одна строка текста: отношение и вторая запись читаются в
     той же строке, а не двумя плашками под кнопкой. */
  .chain-rel {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
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

  .chain-rel[aria-pressed='true'] {
    background: var(--sage);
  }

  .chain-rel__word {
    flex: none;
    color: var(--ink-3);
  }

  .chain-rel__to {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  /* Узкий экран: поле теряет внутренние отступы широкой раскладки, потому что
     колонок на нём нет. */
  @media (max-width: 900px) {
    .chain-field {
      padding: var(--s4);
    }
  }

  /* Цель нажатия на карте: переход по сущности и слова-фильтры были 23 px по
     высоте, и на узком экране в них не попасть пальцем. Пло́тность на
     десктопе не трогается — воздух добавляется только там, где строка и так
     перестроена в одну колонку. */
  @media (max-width: 640px) {
    .map__jump,
    .map__word {
      min-height: 44px;
      align-items: center;
      justify-content: center;
    }
  }

</style>
