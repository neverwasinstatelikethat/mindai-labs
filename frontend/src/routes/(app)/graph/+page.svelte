<script lang="ts">
  // Карта связей: обзор областей, цепочки выбранной области (GraphMap) и
  // инспектор выбранной записи с доказательствами. Инспектор показывается,
  // только когда выбрана запись или связь: до выбора экран принадлежит карте,
  // и держать под ней пустую панель на весь экран нечего.
  import { api, ApiError } from '$lib/api';
  import GraphMap, { classOf } from '$lib/GraphMap.svelte';
  import { navLabel } from '$lib/nav';
  import { countOf, dateTime, num } from '$lib/format';
  import { session } from '$lib/sessionStore.svelte';
  import {
    ASK_SOURCE,
    MAP_KNOWLEDGE_STATUS_LABELS,
    MAP_META_LABELS,
    MAP_NAME_FALLBACK,
    MAP_NOUN,
    MAP_UNNAMED_AREA,
    PROPERTY_LABELS,
    STATUS_SHORT,
    SUBJECT_LABELS,
    TERM_FALLBACKS,
    describeValue,
    knownTerm,
    mapNodeType,
    mapRelationLabel,
  } from '$lib/terms';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import Sheet from '$lib/ui/Sheet.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';
  import { DATA_CLASS_LABELS } from '$lib/types';
  import type {
    FindingListItem,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    NumericObservation,
  } from '$lib/types';

  // Доступ решает сервер: клиент сверяется с /api/v1/auth/me только чтобы не
  // дёргать пустой запрос, а 403 от /api/v1/graph всё равно равноправный ответ.
  const access = $derived(
    session.state === 'unknown'
      ? 'checking'
      : session.can('knowledge:read')
        ? 'granted'
        : 'denied',
  );

  let status = $state<'idle' | 'loading' | 'ready' | 'error' | 'denied'>('idle');
  let graph = $state<GraphSnapshot>({ nodes: [], edges: [], communities: [] });
  let error = $state('');
  let loadedAt = $state<Date | null>(null);
  let selected = $state<GraphNode | null>(null);
  // Выбранная связь живёт на экране: поле подсвечивает её, инспектор описывает
  // словами и ведёт к обеим записям.
  let pickedEdge = $state<GraphEdge | null>(null);
  // Доказательства берутся из настоящих находок корпуса: /api/v1/findings отдаёт
  // тот же Finding, что и ответ запроса, уже срезанный по классу данных сессии.
  // Список приходит страницей, а полное число подходит заголовком сервера: без
  // полного числа экран не отличает «доказательств нет» от «показана не вся
  // выборка».
  const FINDINGS_PAGE_SIZE = 200;
  let findings = $state<FindingListItem[]>([]);
  let findingsStatus = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
  let findingsError = $state('');
  let findingsRequested = false;
  let findingsTotal = $state<number | null>(null);
  /** Сколько строк корпуса уже показано: по нему считают «показаны не все». */
  let findingsShown = $state(0);
  let findingsReading = $state(false);

  // На узком экране инспектор становится нижней шторкой, а не второй колонкой.
  // Узкий экран измеряет страница и передаёт значение полю: второго замера на
  // карте нет.
  let narrow = $state(false);

  $effect(() => {
    const tablet = window.matchMedia('(max-width: 900px)');
    const sync = (): void => {
      narrow = tablet.matches;
    };
    sync();
    tablet.addEventListener('change', sync);
    return () => tablet.removeEventListener('change', sync);
  });

  /**
   * Имена записей берёт сервер: метка узла этого же среза графа и его
   * алиасы. Затем словарь корпуса, и только потом человекочитаемая заглушка.
   * Сырой ключ онтологии в позицию подписи не попадает: он читается под
   * раскрытием «Служебные данные».
   */
  const serverNames = $derived.by(() => {
    const map = new Map<string, string>();
    for (const node of graph.nodes) {
      map.set(node.id.toLowerCase(), node.label);
      map.set(node.label.toLowerCase(), node.label);
      const aliases = node.metadata['aliases'];
      if (typeof aliases === 'string') {
        for (const alias of aliases.split(',')) {
          const key = alias.trim().toLowerCase();
          if (key !== '') map.set(key, node.label);
        }
      }
    }
    return map;
  });

  /** Имя по ключу: сервер, затем словарь, затем заглушка вместо сырого ключа. */
  function humanName(key: string, dictionary: Record<string, string>): string {
    const raw = key.trim();
    if (raw === '') return MAP_NAME_FALLBACK.record;
    return (
      serverNames.get(raw.toLowerCase()) ??
      knownTerm(dictionary, raw) ??
      knownTerm(dictionary, raw.toLowerCase()) ??
      MAP_NAME_FALLBACK.offDictionary
    );
  }

  function subjectName(finding: FindingListItem): string {
    const raw = (finding.subject ?? '').trim();
    if (raw === '') return TERM_FALLBACKS.subject;
    return humanName(raw, SUBJECT_LABELS);
  }

  /** Показатель числового условия: серверное имя либо имя словаря, но не ключ. */
  function propertyName(obs: NumericObservation): string {
    return humanName(obs.property_name, PROPERTY_LABELS);
  }

  /** Чтение величины так, как её записал источник: без нормализации единиц. */
  function sourceValue(obs: NumericObservation): string {
    return describeValue({
      ...obs,
      normalized_value: undefined,
      normalized_min: undefined,
      normalized_max: undefined,
      normalized_unit: '',
    });
  }

  /**
   * Предикат находки называется тем же словом, что и связь на карте: словарь
   * один (`MAP_RELATION_LABELS` собирает базовый `RELATION_LABELS`), своих
   * переименований экран не задаёт. Отношения вне реестра читаются как связь без
   * имени, а её техническое имя лежит в служебных данных находки.
   */
  function predicateName(finding: FindingListItem): string {
    const raw = (finding.predicate ?? '').trim();
    return raw === '' ? TERM_FALLBACKS.predicate : mapRelationLabel(raw);
  }

  /** Символьные оффсеты фрагмента: они читаются только в служебных данных. */
  function offsetRows(finding: FindingListItem): { key: string; value: string }[] {
    return finding.evidence.flatMap((ev, i) =>
      ev.char_start != null && ev.char_end != null
        ? [{ key: `символы фрагмента ${i + 1}`, value: `${ev.char_start}–${ev.char_end}` }]
        : [],
    );
  }

  /** Область записи: серверная метка либо названная группа без метки. */
  function areaOf(node: GraphNode): string {
    const raw = node.metadata['community'];
    return typeof raw === 'string' && raw.trim() !== '' ? raw : MAP_UNNAMED_AREA;
  }

  /**
   * Ссылка на находку и её источник: `/findings` читает `claim` (открывает
   * шторку первоисточника) и `document` (оставляет в списке этот документ).
   * Контракт тот же, что у перехода из ответа в «Находках»: карта своих
   * параметров не изобретает.
   */
  function findingsHref(finding: FindingListItem): string {
    const params = new URLSearchParams();
    params.set('claim', finding.id);
    const doc = finding.evidence[0]?.document_id;
    if (doc) params.set('document', doc);
    return `/findings?${params.toString()}`;
  }

  /** Ссылка на источник: `/findings?document=` оставляет в списке этот документ. */
  function documentHref(node: GraphNode): string | null {
    if (!node.id.startsWith('document-')) return null;
    return `/findings?document=${encodeURIComponent(node.id.slice('document-'.length))}`;
  }

  /**
   * Код находки для записи карты. У извлечённого утверждения узел `claim-<uuid>`,
   * находка `finding-claim-<uuid>`; у готового тезиса схемы узел `claim-ro`,
   * находка `finding-ro`. Экран берёт тот код, который уже виден в списке
   * находок, и только при пустом списке полагается на соглашение извлечения.
   */
  function findingIdFor(node: GraphNode): string | null {
    const suffix = node.id.startsWith('claim-') ? node.id.slice('claim-'.length) : node.id;
    const candidates = [`finding-${node.id}`, node.id, `finding-${suffix}`, suffix];
    return candidates.find((code) => findings.some((finding) => finding.id === code)) ?? null;
  }

  /**
   * Числа той области, на которую смотрит человек: выбранная запись лежит в
   * своей области, и знаменатель счётчика берётся из неё, а не из всего корпуса.
   */
  const focusArea = $derived.by<{ name: string; records: number; links: number } | null>(() => {
    if (!selected) return null;
    const name = areaOf(selected);
    const ids = new Set<string>();
    for (const node of graph.nodes) {
      if (areaOf(node) === name) ids.add(node.id);
    }
    let links = 0;
    for (const edge of graph.edges) {
      if (ids.has(edge.source) && ids.has(edge.target)) links += 1;
    }
    return { name, records: ids.size, links };
  });

  /**
   * Находки, которым принадлежит запись карты: только совпадение идентификаторов.
   * claim-узел `claim-…` и находка `finding-claim-…`, узел документа
   * `document-<uuid>` и `evidence.document_id`.
   *
   * Прежняя привязка по подстроке метки («метка встречается в формулировке»)
   * превращала «Доказательства узла» хаба в стену из сотен несвязанных тезисов.
   * Пересечение по идентификатору остаётся единственной связкой, которую нельзя
   * выдумать из названия.
   */
  const relatedFindings = $derived.by<FindingListItem[]>(() => {
    if (!selected || findingsStatus !== 'ready') return [];
    const nodeId = selected.id;
    const docId = nodeId.startsWith('document-') ? nodeId.slice('document-'.length) : '';
    const rows = findings.filter((finding) => {
      if (finding.id === nodeId || finding.id === `finding-${nodeId}`) return true;
      return docId !== '' && finding.evidence.some((ev) => ev.document_id === docId);
    });
    return rows.sort((a, b) => b.confidence - a.confidence);
  });

  // Списки под инспектором ограничены: стена на сотню строк не читается, а
  // «показать всё» остаётся явным действием.
  const FINDINGS_PAGE = 3;
  const CONNECTIONS_PAGE = 12;
  let findingsAll = $state(false);
  let connectionsAll = $state(false);

  const shownFindings = $derived(
    findingsAll ? relatedFindings : relatedFindings.slice(0, FINDINGS_PAGE),
  );

  interface Connection {
    edge: GraphEdge;
    outgoing: boolean;
    other: GraphNode | null;
  }

  const nodeIndex = $derived(new Map(graph.nodes.map((node) => [node.id, node])));

  /** Связи выбранной записи: направление и вторая сторона берутся из этого же среза. */
  const connections = $derived.by<Connection[]>(() => {
    if (!selected) return [];
    const rows: Connection[] = [];
    for (const edge of graph.edges) {
      if (edge.source === selected.id) {
        rows.push({ edge, outgoing: true, other: nodeIndex.get(edge.target) ?? null });
      } else if (edge.target === selected.id) {
        rows.push({ edge, outgoing: false, other: nodeIndex.get(edge.source) ?? null });
      }
    }
    return rows;
  });
  const outgoingCount = $derived(connections.filter((c) => c.outgoing).length);
  const shownConnections = $derived(
    connectionsAll ? connections : connections.slice(0, CONNECTIONS_PAGE),
  );

  /** Источник выбранной записи: соседний документ этого же среза карты. */
  const sourceDocument = $derived.by<GraphNode | null>(() => {
    for (const conn of connections) {
      if (conn.other && conn.other.id.startsWith('document-')) return conn.other;
    }
    return null;
  });

  /**
   * Главное действие инспектора: срез «Находок», а не пустой список. Сначала
   * утверждение самой записи, затем её источник: ссылка ведёт в маршрут, который
   * умеет открыть этот срез (`claim` и `document` читаются «Находками», своего
   * контракта карта не задаёт). Когда у выбранной записи нет ни находки, ни
   * источника, ссылки нет: кнопка, ведущая в никуда, на экран не выходит.
   */
  const findingHref = $derived.by<string | null>(() => {
    if (!selected) return null;
    const ownDocument = documentHref(selected);
    if (ownDocument) return ownDocument;
    const own = relatedFindings[0];
    if (own) return findingsHref(own);
    if (selected.type === 'claim') {
      const code = findingIdFor(selected) ?? `finding-${selected.id}`;
      return `/findings?claim=${encodeURIComponent(code)}`;
    }
    return sourceDocument ? documentHref(sourceDocument) : null;
  });

  /** Смена записи обнуляет раскрытия и выбор связи: чужое состояние не держим. */
  function pick(node: GraphNode | null): void {
    findingsAll = false;
    connectionsAll = false;
    pickedEdge = null;
    selected = node;
  }

  function pickEdge(edge: GraphEdge | null): void {
    pickedEdge = edge;
  }

  let requestSeq = 0;
  async function load(): Promise<void> {
    const call = ++requestSeq;
    status = 'loading';
    error = '';
    try {
      const snapshot = await api.graph();
      if (call !== requestSeq) return;
      graph = snapshot;
      pick(null);
      loadedAt = new Date();
      status = 'ready';
    } catch (reason) {
      if (call !== requestSeq) return;
      if (reason instanceof ApiError && reason.status === 403) {
        status = 'denied';
        error = '';
        return;
      }
      error = 'Проверьте соединение и повторите запрос.';
      status = 'error';
    }
  }

  /**
   * Находки приходят страницей с полным числом: `findingsLeft` отличает
   * «доказательств нет» от «показана не вся выборка». Отдельная строка сервиса о
   * неполноте на экран не выходит: то же самое сказано числом.
   */
  async function loadFindings(more = false): Promise<void> {
    if (more) {
      findingsReading = true;
    } else {
      findingsStatus = 'loading';
      findingsError = '';
    }
    try {
      const page = await api.findings(
        undefined,
        undefined,
        FINDINGS_PAGE_SIZE,
        more ? findingsShown : 0,
      );
      findings = more ? mergePages(findings, page.items) : page.items;
      findingsShown = more ? findingsShown + page.items.length : page.items.length;
      findingsTotal = page.total;
      findingsStatus = 'ready';
    } catch (reason) {
      // На экран выходит человеческая строка, а не текст самого отказа.
      if (reason instanceof ApiError && reason.status === 403) {
        findingsError = 'Доказательства записей открыты при доступе к корпусу. Право выдаёт администратор сервиса.';
      } else {
        findingsError = 'Проверьте соединение и повторите запрос.';
      }
      findingsStatus = 'error';
    } finally {
      findingsReading = false;
    }
  }

  /** Следующая страница может перекрываться с показанной: повтор в список не добавляется. */
  function mergePages(
    current: FindingListItem[],
    incoming: FindingListItem[],
  ): FindingListItem[] {
    const seen = new Set(current.map((finding) => finding.id));
    return [...current, ...incoming.filter((finding) => !seen.has(finding.id))];
  }

  /** Сколько находок корпуса ещё не показано. */
  const findingsLeft = $derived(
    findingsTotal === null ? 0 : Math.max(0, findingsTotal - findings.length),
  );

  // Права приходят с сервера: как только они подтверждены, карта читается.
  $effect(() => {
    if (access !== 'granted') return;
    void load();
  });

  $effect(() => {
    if (selected && !findingsRequested) {
      findingsRequested = true;
      void loadFindings();
    }
  });

  const loadedAtText = $derived(loadedAt ? dateTime(loadedAt, true) : '');

  // Метаданные записи: переводим известные ключи словарём карты, JSON-блоб и
  // неизвестные ключи не печатаем. Эти строки служебные: они живут под раскрытием
  // «Служебные данные», а не в фактах записи.
  const META_SKIP = new Set(['observations']);

  function metaEntries(node: GraphNode | null): { key: string; label: string; value: string }[] {
    if (!node) return [];
    const rows: { key: string; label: string; value: string }[] = [];
    for (const [key, raw] of Object.entries(node.metadata)) {
      if (META_SKIP.has(key)) continue;
      const label = MAP_META_LABELS[key];
      // Без словарного перевода строку пропускаем: сырой snake_case-ключ не
      // становится видимой подписью.
      if (!label) continue;
      let text = String(raw);
      if (key === 'knowledge_status') text = MAP_KNOWLEDGE_STATUS_LABELS[text] ?? text;
      // год=0 есть сервисный маркер «года нет», печатать его нельзя
      if (text.trim() === '' || (key === 'year' && text === '0')) continue;
      rows.push({ key, label, value: text });
    }
    return rows;
  }

  /**
   * Числовые условия могут лежать и в самой записи (`metadata.observations`:
   * JSON-список, который сервер пишет при извлечении). Показываем их запасным
   * слоем, когда находка не нашлась: числа настоящие, места в источнике тут нет.
   */
  function nodeObservations(node: GraphNode | null): { rows: NumericObservation[]; broken: boolean } {
    const raw = node?.metadata['observations'];
    if (typeof raw !== 'string' || raw.trim() === '') return { rows: [], broken: false };
    try {
      const parsed: unknown = JSON.parse(raw);
      if (!Array.isArray(parsed)) return { rows: [], broken: true };
      const rows = parsed.filter(
        (item): item is NumericObservation =>
          typeof item === 'object' &&
          item !== null &&
          typeof (item as NumericObservation).property_name === 'string',
      );
      return { rows, broken: rows.length === 0 };
    } catch {
      return { rows: [], broken: true };
    }
  }

  const nodeObs = $derived(nodeObservations(selected));

  function confidenceWidth(value: number): string {
    return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
  }
</script>

<svelte:head>
  <title>Карта связей: Научный Клубок</title>
</svelte:head>

{#snippet nodeBody()}
  {#if selected}
    {@const cls = classOf(selected)}
    <div class="stack map__node-facts">
      <dl class="kv">
        <dt>вид записи</dt>
        <dd>{mapNodeType(selected.type)}</dd>
        <dt>область на карте</dt>
        <dd>{areaOf(selected)}</dd>
        {#if focusArea}
          <dt>записей в этой области</dt>
          <dd class="num">{num(focusArea.records)}</dd>
          <dt>связей в этой области</dt>
          <dd class="num">{num(focusArea.links)}</dd>
        {/if}
        <dt>класс данных</dt>
        <dd><StatusPill status={cls.pill} label={cls.ru} /></dd>
        <dt>уверенность</dt>
        <dd class="num">{num(selected.confidence)}</dd>
        <dt>связей всего</dt>
        <dd class="num">{connections.length}</dd>
        <dt>из них исходящих</dt>
        <dd class="num">{outgoingCount}</dd>
        <dt>входящих</dt>
        <dd class="num">{connections.length - outgoingCount}</dd>
      </dl>

      <div
        class="bar"
        role="img"
        aria-label="Уверенность записи {num(selected.confidence)} из 1"
      >
        <span class="bar__fill" style="width: {confidenceWidth(selected.confidence)}"></span>
      </div>

      <div class="map__conn">
        <div class="row row--between">
          <h3 class="h4">Связи этой записи</h3>
          <p class="micro muted map__count">
            {#if connections.length > shownConnections.length}
              <span>
                показано
                <span class="num">{shownConnections.length}</span>
                из
                <span class="num">{connections.length}</span>
              </span>
            {:else}
              <span class="num">
                {countOf(
                  connections.length,
                  MAP_NOUN.link.one,
                  MAP_NOUN.link.few,
                  MAP_NOUN.link.many,
                )}
              </span>
            {/if}
          </p>
        </div>
        {#if connections.length === 0}
          <p class="micro muted">В этой карте у записи нет связей.</p>
        {:else}
          <p class="micro muted map__list-head">
            направление, отношение, вторая запись, уверенность
          </p>
          <ul class="map__conn-list">
            {#each shownConnections as conn, i (`${conn.edge.id}-${i}`)}
              <li>
                <span class="micro muted map__dir">{conn.outgoing ? 'исходит' : 'входит'}</span>
                <button
                  type="button"
                  class="map__rel"
                  aria-pressed={pickedEdge?.id === conn.edge.id}
                  onclick={() => pickEdge(pickedEdge?.id === conn.edge.id ? null : conn.edge)}
                >
                  <span>{mapRelationLabel(conn.edge.relation)}</span>
                  <code class="tech">{conn.edge.relation}</code>
                </button>
                {#if conn.other}
                  {@const target = conn.other}
                  <button type="button" class="map__jump" onclick={() => pick(target)}>
                    {target.label}
                    <span class="micro muted">{mapNodeType(target.type)}</span>
                  </button>
                {:else}
                  <span class="micro muted">запись вне этой карты</span>
                {/if}
                <span class="num micro map__conf">{num(conn.edge.confidence)}</span>
              </li>
            {/each}
          </ul>
          {#if connections.length > shownConnections.length}
            <Button variant="quiet" size="sm" onclick={() => (connectionsAll = true)}>
              Показать все связи
              <span class="num">
                {countOf(
                  connections.length - shownConnections.length,
                  MAP_NOUN.link.one,
                  MAP_NOUN.link.few,
                  MAP_NOUN.link.many,
                )}
              </span>
            </Button>
          {/if}
        {/if}
      </div>

      <details class="map__tech">
        <summary class="micro">Служебные данные записи</summary>
        <dl class="kv">
          <dt>код записи</dt>
          <dd><code class="tech">{selected.id}</code></dd>
          <dt>вид в онтологии</dt>
          <dd><code class="tech">{selected.type}</code></dd>
          <dt>класс данных</dt>
          <dd><code class="tech">{cls.code}</code></dd>
          {#each metaEntries(selected) as row (row.key)}
            <dt>{row.label}</dt>
            <dd><code class="tech">{row.value}</code></dd>
          {/each}
        </dl>
      </details>
    </div>
  {/if}
{/snippet}

{#snippet obsTable(rows: NumericObservation[], caption: string)}
  <div class="table-wrap">
    <table class="table">
      <caption class="micro muted map__caption">{caption}</caption>
      <thead>
        <tr>
          <th>Показатель</th>
          <th>Условие в единых единицах</th>
          <th>Как в источнике</th>
        </tr>
      </thead>
      <tbody>
        {#each rows as obs, i (`${caption}-${i}`)}
          <tr>
            <td>{propertyName(obs)}</td>
            <td class="num">{describeValue(obs)}</td>
            <td class="num">{sourceValue(obs)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
  <details class="map__tech">
    <summary class="micro">Служебные данные условий</summary>
    <dl class="kv">
      {#each rows as obs, i (`code-${i}`)}
        <dt>код показателя</dt>
        <dd><code class="tech">{obs.property_name}</code></dd>
        <dt>условие как записано в источнике</dt>
        <dd><code class="tech">{obs.raw_text || 'нет данных'}</code></dd>
      {/each}
    </dl>
  </details>
{/snippet}

{#snippet evidenceBlock()}
  {#if selected}
    <div class="map__evidence">
      <div class="row row--between">
        <h3 class="h4">Доказательства этой записи</h3>
        {#if findingsStatus === 'ready'}
          <p class="micro muted map__count">
            <span class="num">
              {countOf(relatedFindings.length, 'находка', 'находки', 'находок')}
            </span>
          </p>
        {/if}
      </div>

      {#if findingsStatus === 'loading' || findingsStatus === 'idle'}
        <div class="stack" style="--gap: var(--s3)">
          <p class="micro muted">
            Загружаем находки корпуса: доказательства этой записи появятся здесь.
          </p>
          <span class="skeleton map__skeleton-line"></span>
          <span class="skeleton map__skeleton-line map__skeleton-line--short"></span>
        </div>
      {:else if findingsStatus === 'error'}
        <Notice tone="error" title="Находки не загрузились">{findingsError}</Notice>
        <Button
          variant="quiet"
          size="sm"
          onclick={() => {
            findingsRequested = false;
            void loadFindings();
          }}
        >
          Повторить запрос находок
        </Button>
      {:else if relatedFindings.length === 0}
        {#if findingsLeft > 0}
          <p class="small muted">
            Показано <span class="num">{findingsShown}</span> находок корпуса
            из <span class="num">{findingsTotal ?? 0}</span>. Доказательств этой записи
            среди показанных нет.
          </p>
          <div class="row">
            <Button
              variant="quiet"
              size="sm"
              busy={findingsReading}
              onclick={() => void loadFindings(true)}
            >
              Показать ещё
            </Button>
            {#if findingHref}
              <Button href={findingHref} variant="ghost" size="sm">{ASK_SOURCE.findings}</Button>
            {/if}
          </div>
        {:else}
          <p class="small muted">Для этой записи доказательств не найдено.</p>
          {#if findingHref}
            <Button href={findingHref} variant="quiet" size="sm">{ASK_SOURCE.findings}</Button>
          {:else}
            <Button href="/research" variant="quiet" size="sm">
              Спросить в разделе «{navLabel('/research')}»
            </Button>
          {/if}
        {/if}

        {#if nodeObs.broken}
          <Notice tone="warn" title="Числовые условия записи не читаются">
            Условия записаны так, что числа из них не извлекаются.
          </Notice>
        {:else if nodeObs.rows.length > 0}
          {@render obsTable(nodeObs.rows, 'Числовые условия самой записи')}
          <p class="micro muted">
            Места в источнике у этих условий нет: они пришли вместе с записью.
          </p>
        {/if}
      {:else}
        {#if findingsLeft > 0}
          <p class="micro muted">
            Показаны не все находки корпуса: <span class="num">{findingsShown}</span>
            из <span class="num">{findingsTotal ?? 0}</span>.
          </p>
        {/if}
        {#each shownFindings as finding (finding.id)}
          <article class="map__claim">
            <div class="row row--between">
              <p class="eyebrow map__claim-tags">
                <span>{subjectName(finding)}</span>
                <span>{predicateName(finding)}</span>
                <span>версия <span class="num">{finding.version}</span></span>
              </p>
              <StatusPill status={finding.status} label={STATUS_SHORT[finding.status]} />
            </div>

            <h4 class="h4">{finding.statement}</h4>

            <dl class="kv">
              <dt>уверенность</dt>
              <dd class="num">{num(finding.confidence)}</dd>
              <dt>класс данных</dt>
              <dd>{DATA_CLASS_LABELS[finding.data_class]}</dd>
              {#if finding.superseded_by}
                <dt>заменена</dt>
                <dd>более новой версией, её код лежит в служебных данных</dd>
              {/if}
            </dl>

            <div
              class="bar"
              role="img"
              aria-label="Уверенность находки {num(finding.confidence)} из 1"
            >
              <span
                class="bar__fill bar__fill--{finding.status === 'disputed' ? 'disputed' : 'consensus'}"
                style="width: {confidenceWidth(finding.confidence)}"
              ></span>
            </div>

            {#if finding.observations.length > 0}
              {@render obsTable(finding.observations, 'Числовые условия утверждения')}
            {:else}
              <p class="micro muted">
                Утверждение без числовых условий: сравнение идёт по формулировке и по месту
                в источнике.
              </p>
            {/if}

            <div class="map__ev">
              <div class="row row--between">
                <p class="small">Места в источнике</p>
                <p class="micro muted">
                  <span class="num">
                    {countOf(
                      finding.evidence.length,
                      'доказательство',
                      'доказательства',
                      'доказательств',
                    )}
                  </span>
                </p>
              </div>
              {#if finding.evidence.length === 0}
                <p class="micro muted">
                  У утверждения нет ни одного места в источнике: проверить первоисточник
                  по этой находке нельзя.
                </p>
              {:else}
                {#each finding.evidence as ev, i (`${finding.id}-ev-${i}`)}
                  <figure class="map__evidence-item">
                    <blockquote class="quote">{ev.quote}</blockquote>
                    <figcaption class="locator">
                      <Icon name="doc" size={14} />
                      <span>{ev.source_title || 'источник без названия'}</span>
                      {#if ev.page != null}<span>стр. <span class="num">{ev.page}</span></span>{/if}
                      {#if ev.sheet}<span>лист {ev.sheet}</span>{/if}
                      {#if ev.cell_range}
                        <span>ячейки {ev.cell_range}</span>
                      {/if}
                    </figcaption>
                  </figure>
                {/each}
              {/if}
            </div>

            <details class="map__tech">
              <summary class="micro">Служебные данные находки</summary>
              <dl class="kv">
                <dt>код находки</dt>
                <dd><code class="tech">{finding.id}</code></dd>
                <dt>субъект</dt>
                <dd><code class="tech">{finding.subject || '—'}</code></dd>
                <dt>предикат</dt>
                <dd><code class="tech">{finding.predicate || '—'}</code></dd>
                {#if finding.superseded_by}
                  <dt>заменена на</dt>
                  <dd><code class="tech">{finding.superseded_by}</code></dd>
                {/if}
                {#each offsetRows(finding) as row (row.key)}
                  <dt>{row.key}</dt>
                  <dd><code class="tech">{row.value}</code></dd>
                {/each}
              </dl>
            </details>

            <div class="row">
              <Button href={findingsHref(finding)} variant="quiet" size="sm">
                {ASK_SOURCE.findings}
              </Button>
              {#if finding.status === 'disputed'}
                <Button href="/conflicts" variant="quiet" size="sm">Открыть расхождение</Button>
              {/if}
            </div>
          </article>
        {/each}

        {#if relatedFindings.length > shownFindings.length}
          <Button variant="quiet" size="sm" onclick={() => (findingsAll = true)}>
            Показать все находки этой записи
            <span class="num">
              {countOf(
                relatedFindings.length - shownFindings.length,
                'находка',
                'находки',
                'находок',
              )}
            </span>
          </Button>
        {/if}
      {/if}
    </div>
  {/if}
{/snippet}

{#snippet edgeCard()}
  {#if pickedEdge}
    {@const fromNode = nodeIndex.get(pickedEdge.source) ?? null}
    {@const toNode = nodeIndex.get(pickedEdge.target) ?? null}
    <div class="map__edge-card">
      <div class="row row--between">
        <p class="eyebrow">
          <Icon name="link" size={16} />
          выбранная связь
        </p>
        <Button variant="ghost" size="sm" onclick={() => pickEdge(null)}>Снять связь</Button>
      </div>
      <p class="small map__edge-line">
        <span>{mapRelationLabel(pickedEdge.relation)}</span>
        <code class="tech">{pickedEdge.relation}</code>
      </p>
      <p class="small map__edge-line">
        <button type="button" class="map__jump" onclick={() => fromNode && pick(fromNode)}>
          {fromNode?.label ?? 'запись вне этой карты'}
        </button>
        <span class="micro muted">ведёт к</span>
        <button type="button" class="map__jump" onclick={() => toNode && pick(toNode)}>
          {toNode?.label ?? 'запись вне этой карты'}
        </button>
      </p>
      <dl class="kv">
        <dt>уверенность связи</dt>
        <dd class="num">{num(pickedEdge.confidence)}</dd>
        <dt>класс данных связи</dt>
        <dd>{DATA_CLASS_LABELS[pickedEdge.data_class]}</dd>
      </dl>
    </div>
  {/if}
{/snippet}

<div class="page map-page">
  <div class="wrap">
    <!-- Узкий экран отдаёт вертикаль полю карты: подпись раздела на нём заменяет
         заголовок. -->
    <SectionHead
      level="1"
      eyebrow={narrow ? '' : 'Граф доказательств'}
      title="Карта связей корпуса"
    >
      {#if status === 'ready'}
        <div class="map__head-tools">
          <p class="micro muted map__meta">
            <span>
              записей <span class="num">{graph.nodes.length}</span>
            </span>
            <span>
              связей <span class="num">{graph.edges.length}</span>
            </span>
            {#if loadedAt}
              <span>
                карта получена <time datetime={loadedAt.toISOString()}>{loadedAtText}</time>
              </span>
            {/if}
          </p>
          <Button variant="quiet" size="sm" icon="refresh" onclick={() => void load()}>
            Перечитать карту
          </Button>
        </div>
      {/if}
    </SectionHead>

    {#if access === 'checking'}
      <Panel tone="sunk">
        <div class="map__skeleton">
          <span class="skeleton map__skeleton-line"></span>
          <span class="skeleton map__skeleton-field"></span>
          <p class="micro muted">Уточняем доступ к корпусу: карту заранее не читаем.</p>
        </div>
      </Panel>
    {:else if access === 'denied' || status === 'denied'}
      <Panel tone="lav">
        <div class="panel__head">
          <h2 class="h3">Карта связей корпуса закрыта</h2>
          <StatusPill status="off" label="нет доступа" />
        </div>
        <p class="lead small">
          Карта связей и находки открываются при доступе к корпусу. Право выдаёт
          администратор сервиса, на этом экране его не включить.
        </p>
        {#if error}<p class="micro muted">{error}</p>{/if}
        <div class="row">
          <Button href="/research" variant="quiet">Перейти в рабочее пространство</Button>
          <Button href="/dashboard" variant="ghost">{navLabel('/dashboard')}</Button>
        </div>
      </Panel>
    {:else if status === 'loading' || status === 'idle'}
      <Panel tone="sunk">
        <div class="map__skeleton">
          <p class="micro muted">
            Собираем карту: записи, связи и области корпуса.
          </p>
          <span class="skeleton map__skeleton-line"></span>
          <span class="skeleton map__skeleton-field"></span>
        </div>
      </Panel>
    {:else if status === 'error'}
      <Panel tone="coral">
        <div class="panel__head">
          <h2 class="h3">Карта связей не загрузилась</h2>
        </div>
        <Notice tone="error">{error}</Notice>
        <div class="row">
          <Button variant="action" onclick={() => void load()}>Повторить запрос</Button>
          <Button href="/research" variant="quiet">Задать вопрос по корпусу</Button>
        </div>
      </Panel>
    {:else if graph.nodes.length === 0}
      <Empty
        icon="graph"
        title="Карта связей пуста"
        body="Сервер вернул карту без записей: на ней появляются только те документы, из которых уже извлечены утверждения."
      >
        {#snippet action()}
          <div class="row">
            <Button href="/findings" variant="quiet">Загрузить документ в «Находках»</Button>
            <Button href="/research" variant="ghost">Задать вопрос по корпусу</Button>
          </div>
        {/snippet}
      </Empty>
    {:else}
      <GraphMap
        {graph}
        {narrow}
        selectedId={selected ? selected.id : undefined}
        edgeId={pickedEdge ? pickedEdge.id : undefined}
        onselect={pick}
        onpickEdge={pickEdge}
      />

      {#if pickedEdge && !selected}
        <section class="map__edge-note" aria-label="Выбранная связь">
          {@render edgeCard()}
        </section>
      {/if}

      {#if selected}
        <section id="evidence" class="map__inspector">
          {#if narrow}
            <Sheet
              title="Запись карты: {selected.label}"
              onclose={() => pick(null)}
              width="760px"
            >
              {@render edgeCard()}
              {@render nodeBody()}
              {@render evidenceBlock()}
            </Sheet>
          {:else}
            <Panel tone="default" raised>
              <div class="panel__head">
                <div>
                  <p class="eyebrow">
                    <Icon name="pin" size={16} />
                    выбранная запись
                    <span>{mapNodeType(selected.type)}</span>
                  </p>
                  <h2 class="h3">{selected.label}</h2>
                </div>
                <div class="row">
                  {#if findingHref}
                    <Button href={findingHref} variant="quiet" size="sm">
                      {ASK_SOURCE.findings}
                    </Button>
                  {/if}
                  <Button variant="ghost" size="sm" onclick={() => pick(null)}>Снять выбор</Button>
                </div>
              </div>

              {@render edgeCard()}

              <div class="split map__split">
                <div class="stack" style="--gap: var(--s5)">
                  {@render nodeBody()}
                </div>
                <div class="stack" style="--gap: var(--s4)">
                  {@render evidenceBlock()}
                </div>
              </div>
            </Panel>
          {/if}
        </section>
      {/if}
    {/if}
  </div>
</div>

<style>
  .map-page .wrap {
    display: flex;
    flex-direction: column;
    gap: var(--s5);
  }

  .map__head-tools {
    display: flex;
    align-items: center;
    gap: var(--s4);
    flex-wrap: wrap;
  }

  .map__meta {
    margin: 0;
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: var(--s1) var(--s4);
    white-space: normal;
  }

  /* Карточка выбранной связи живёт и без выбранной записи: связь остаётся
     прочитанной, когда человек снял выбор с поля. */
  .map__edge-note {
    display: flex;
    flex-direction: column;
  }

  .map__edge-note .map__edge-card {
    margin-block: 0;
  }

  /* Карточка выбранной связи: та же трасса, что у строки на поле, но словами. */
  .map__edge-card {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    margin-block: var(--s4) 0;
    padding: var(--s4);
    border-radius: var(--r-md);
    background: var(--surface-sunk);
    align-items: flex-start;
  }

  .map__edge-card p {
    margin: 0;
  }

  .map__edge-line {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
  }

  .map__edge-card .kv {
    width: 100%;
  }

  /* Субъект, отношение и версия находки — три подписанных элемента, а не одна
     склеенная строка: длинное имя переносится, не сминая соседей. */
  .map__claim-tags {
    flex-wrap: wrap;
    row-gap: var(--s1);
    min-width: 0;
  }

  .map__claim-tags span {
    overflow-wrap: anywhere;
  }

  .map__skeleton {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  .map__skeleton p {
    margin: 0;
  }

  /* Загрузка без скачка вёрстки: очертания поля занимают место сразу. */
  .map__skeleton-line {
    height: 14px;
    width: 100%;
  }

  .map__skeleton-line--short {
    width: 46%;
  }

  /* Поле больше не имеет жёсткой высоты: оно растёт с содержимым, поэтому на
     время чтения достаточно очертания в треть экрана. */
  .map__skeleton-field {
    height: clamp(240px, 34dvh, 420px);
    border-radius: var(--r-xl);
  }

  .map__inspector {
    min-height: 260px;
  }

  .map__split {
    --gap: var(--s6);
  }

  .map__node-facts {
    --gap: var(--s4);
  }

  .map__node-facts .kv dd {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: var(--s2);
    flex-wrap: wrap;
  }

  .map__list-head {
    margin: var(--s2) 0 0;
    letter-spacing: var(--tr-body);
  }

  .map__conn-list {
    list-style: none;
    margin: var(--s2) 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }

  .map__conn-list li {
    display: grid;
    grid-template-columns: 66px minmax(96px, 0.9fr) minmax(0, 1.4fr) 52px;
    gap: var(--s3);
    align-items: baseline;
    padding: var(--s2) 0;
    border-top: 1px solid var(--line-soft);
  }

  .map__dir {
    color: var(--ink-3);
  }

  /* Отношение связи — действие: с клавиатуры оно выбирает связь и раскрывает её
     карточку, а не только перескакивает на вторую запись. Русское имя сверху,
     техническое подписью под ним. */
  .map__rel {
    display: inline-flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0;
    min-width: 0;
    padding: 0 var(--s2);
    border: 0;
    border-radius: var(--r-xs);
    background: none;
    color: var(--ink-2);
    font: inherit;
    font-size: var(--t-small);
    line-height: var(--lh-dense);
    text-align: start;
    cursor: pointer;
  }

  .map__rel:hover {
    color: var(--ink);
    background: var(--surface-sunk);
  }

  .map__rel[aria-pressed='true'] {
    color: var(--ink);
    box-shadow: inset 0 0 0 1px var(--action-ink);
  }

  .map__rel .tech {
    color: var(--ink-3);
  }

  .map__jump {
    display: inline-flex;
    align-items: baseline;
    gap: var(--s2);
    border: 0;
    border-radius: var(--r-xs);
    background: none;
    padding: 0;
    color: var(--ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
    min-width: 0;
    /* Метки узлов — часто одно длинное слово (имя файла, «руда_медногорского_…»):
       без переноса оно распирает колонку и уходит за край шторки. */
    overflow-wrap: anywhere;
  }

  .map__jump:hover {
    color: var(--action-ink);
  }

  .map__conf {
    text-align: right;
    color: var(--ink-2);
  }

  .map__evidence {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  .map__claim {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    padding: var(--s5);
    border-radius: var(--r-lg);
    background: var(--surface-sunk);
  }

  .map__ev {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  .map__evidence-item {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  .map__caption {
    caption-side: top;
    text-align: left;
    padding: var(--s3) var(--s4) 0;
  }

  /* Технические имена — только под раскрытием «Служебные данные»: в фактах узла
     и находки они не заголовки. */
  .map__tech {
    border-top: 1px solid var(--line-soft);
    padding-top: var(--s3);
  }

  .map__tech summary {
    cursor: pointer;
    color: var(--ink-3);
  }

  .map__tech .kv {
    margin-top: var(--s2);
  }

  .map__tech dd {
    overflow-wrap: anywhere;
  }

  @media (max-width: 900px) {
    /* Шапка в один плотный ряд: подпись среза переносится, кнопка остаётся
       на своей ширине, и поле карты поднимается к первому вьюпорту. */
    .map__head-tools {
      align-items: flex-start;
      flex-wrap: nowrap;
    }

    .map__meta {
      flex: 1 1 auto;
      min-width: 0;
    }

    .map__head-tools :global(.btn) {
      flex: none;
    }

    /* Строка связи перестраивается в текучую: трёхколоночная сетка на ширине
       шторки оставляла метке ~30 px, и слово разваливалось по буквам. */
    .map__conn-list li {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: var(--s1) var(--s2);
    }
  }

  @media (max-width: 640px) {
    /* В узкой колонке читаются направление, отношение и второй узел;
       уверенность остаётся в полном составе списка. */
    .map__conf {
      display: none;
    }
  }
</style>
