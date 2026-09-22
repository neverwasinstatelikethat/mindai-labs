<script lang="ts">
  // Карта связей: поле корреляции (GraphCanvas) + инспектор выбранного узла с
  // доказательствами. Шапку, skip-link и <main id="main"> рендерит +layout.svelte.
  import { api, ApiError } from '$lib/api';
  import GraphCanvas, { classOf, relationLabel, typeLabel } from '$lib/GraphCanvas.svelte';
  import { plural } from '$lib/format';
  import { session } from '$lib/sessionStore.svelte';
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

  // Доказательства берутся из настоящих находок корпуса: /api/v1/findings отдаёт
  // тот же Finding, что и ответ запроса, уже срезанный по классу данных сессии.
  let findings = $state<FindingListItem[]>([]);
  let findingsStatus = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
  let findingsError = $state('');
  let findingsRequested = false;

  // Мобильная композиция: инспектор уходит в шторку, поле карты остаётся один.
  let narrow = $state(false);
  // Показание среза и пояснение чтения карты свёрнуты в рельсе: поле карты —
  // главный объект экрана, оно открывается в первом вьюпорте.
  let helpOpen = $state(false);
  $effect(() => {
    const media = window.matchMedia('(max-width: 900px)');
    narrow = media.matches;
    const onChange = (event: MediaQueryListEvent): void => {
      narrow = event.matches;
    };
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  });

  const nf = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 });
  function fmt(v: number): string {
    return nf.format(v);
  }

  const STATUS_RU: Record<string, string> = {
    consensus: 'согласуется',
    hypothesis: 'гипотеза',
    disputed: 'оспаривается',
  };
  const OPERATOR_RU: Record<string, string> = {
    eq: 'равно',
    lt: 'меньше',
    lte: 'не выше',
    gt: 'больше',
    gte: 'не ниже',
    between: 'в диапазоне',
  };

  const typeCount = $derived(new Set(graph.nodes.map((n) => n.type)).size);
  const classCount = $derived(new Set(graph.nodes.map((n) => n.data_class)).size);
  // Классы данных в срезе читаются человеческими словами, а не кодом контракта.
  const classLabels = $derived(
    [...new Set(graph.nodes.map((node) => classOf(node).ru))].sort((a, b) => a.localeCompare(b, 'ru')).join(' · '),
  );

  function obsValue(obs: NumericObservation): string {
    if (obs.min_value != null && obs.max_value != null) {
      return `${fmt(obs.min_value)}–${fmt(obs.max_value)}`;
    }
    if (obs.value != null) return fmt(obs.value);
    if (obs.min_value != null) return `>= ${fmt(obs.min_value)}`;
    if (obs.max_value != null) return `<= ${fmt(obs.max_value)}`;
    return '—';
  }

  function obsNormalized(obs: NumericObservation): string {
    if (obs.normalized_min != null && obs.normalized_max != null) {
      return `${fmt(obs.normalized_min)}–${fmt(obs.normalized_max)} ${obs.normalized_unit}`;
    }
    if (obs.normalized_value != null) {
      return `${fmt(obs.normalized_value)} ${obs.normalized_unit}`;
    }
    return '—';
  }

  /**
   * Узел → находки, которым он принадлежит. Связки настоящие: узел-утверждение
   * имеет id `claim-…`, а соответствующая находка — `finding-claim-…`; узел
   * документа `document-<uuid>` совпадает с `evidence.document_id`; остальное —
   * совпадение субъекта или вхождение метки узла в формулировку.
   */
  const relatedFindings = $derived.by<FindingListItem[]>(() => {
    if (!selected || findingsStatus !== 'ready') return [];
    const nodeId = selected.id.toLowerCase();
    const label = selected.label.trim().toLowerCase();
    const docId = nodeId.startsWith('document-') ? nodeId.slice('document-'.length) : '';
    const scored: { finding: FindingListItem; score: number }[] = [];
    for (const finding of findings) {
      const subject = (finding.subject ?? '').toLowerCase().replace(/[_\s]+/g, '-');
      let score = 0;
      if (finding.id === `finding-${selected.id}` || finding.id === selected.id) score = 4;
      else if (docId && finding.evidence.some((e) => e.document_id === docId)) score = 3;
      else if (subject && (subject === nodeId || subject === label.replace(/\s+/g, '-'))) score = 3;
      else if (label.length > 3 && finding.statement.toLowerCase().includes(label)) score = 2;
      else if (label.length > 3 && finding.predicate?.toLowerCase() === label) score = 1;
      if (score > 0) scored.push({ finding, score });
    }
    return scored
      .sort((a, b) => b.score - a.score || b.finding.confidence - a.finding.confidence)
      .map((row) => row.finding);
  });

  interface Connection {
    edge: GraphEdge;
    outgoing: boolean;
    other: GraphNode | null;
    otherId: string;
  }

  /** Связи выбранного узла: направление и вторая сторона — из того же среза. */
  const connections = $derived.by<Connection[]>(() => {
    if (!selected) return [];
    const index = new Map(graph.nodes.map((n) => [n.id, n]));
    const rows: Connection[] = [];
    for (const edge of graph.edges) {
      if (edge.source === selected.id) {
        rows.push({ edge, outgoing: true, other: index.get(edge.target) ?? null, otherId: edge.target });
      } else if (edge.target === selected.id) {
        rows.push({ edge, outgoing: false, other: index.get(edge.source) ?? null, otherId: edge.source });
      }
    }
    return rows;
  });
  const outgoingCount = $derived(connections.filter((c) => c.outgoing).length);

  let requestSeq = 0;
  async function load(): Promise<void> {
    const call = ++requestSeq;
    status = 'loading';
    error = '';
    try {
      const snapshot = await api.graph();
      if (call !== requestSeq) return;
      graph = snapshot;
      selected = null;
      loadedAt = new Date();
      status = 'ready';
    } catch (reason) {
      if (call !== requestSeq) return;
      if (reason instanceof ApiError && reason.status === 403) {
        status = 'denied';
        error = '';
        return;
      }
      error = 'Карта связей не собралась. Проверьте соединение и повторите запрос.';
      status = 'error';
    }
  }

  async function loadFindings(): Promise<void> {
    findingsStatus = 'loading';
    findingsError = '';
    try {
      findings = await api.findings();
      findingsStatus = 'ready';
    } catch (reason) {
      // На экран выходит человеческая строка, а не текст самого отказа.
      if (reason instanceof ApiError && reason.status === 403) {
        findingsError =
          'Доказательства узла открыты при доступе к корпусу — запросите его у администратора сервиса.';
      } else {
        findingsError = 'Находки корпуса не загрузились: проверьте соединение и повторите запрос.';
      }
      findingsStatus = 'error';
    }
  }

  // Срез зависит от прав сессии: как только права подтверждены — читаем карту.
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

  const loadedAtText = $derived(
    loadedAt ? loadedAt.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'medium' }) : '',
  );

  // Метаданные узла: переводим известные ключи, JSON-блоб не печатаем.
  const META_LABELS: Record<string, string> = {
    community: 'сообщество',
    year: 'год',
    geography: 'география',
    aliases: 'альтернативные названия',
    knowledge_status: 'статус знания',
  };
  const META_SKIP = new Set(['observations']);

  function metaEntries(node: GraphNode | null): [string, string][] {
    if (!node) return [];
    const rows: [string, string][] = [];
    for (const [key, value] of Object.entries(node.metadata)) {
      if (META_SKIP.has(key)) continue;
      const text = String(value);
      // год=0 — сервисный маркер «года нет», печатать его нельзя
      if (text.trim() === '' || (key === 'year' && text === '0')) continue;
      rows.push([META_LABELS[key] ?? key, text]);
    }
    return rows;
  }

  /**
   * Числовые условия могут лежать и в самом узле (`metadata.observations` —
   * JSON-список, который сервер пишет при извлечении). Показываем их как
   * запасной слой, когда находка не нашлась: числа настоящие, локатора тут нет.
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
  <title>Карта связей — Научный Клубок</title>
</svelte:head>

{#snippet nodeBody()}
  {#if selected}
    {@const cls = classOf(selected)}
    <div class="stack map__node-facts">
      <dl class="kv">
        <dt>тип узла</dt>
        <dd>{typeLabel(selected.type)} <code>{selected.type}</code></dd>
        <dt>класс данных</dt>
        <dd><StatusPill status={cls.pill} label={cls.ru} /></dd>
        <dt>уверенность</dt>
        <dd class="num">{fmt(selected.confidence)}</dd>
        <dt>связей</dt>
        <dd class="num">
          {connections.length}
          <span class="micro muted">· исходящих {outgoingCount}, входящих {connections.length - outgoingCount}</span>
        </dd>
        <dt>код узла</dt>
        <dd><code>{selected.id}</code></dd>
        {#each metaEntries(selected) as [key, value] (key)}
          <dt>{key}</dt>
          <dd><code>{value}</code></dd>
        {/each}
      </dl>

      <div class="bar" role="img" aria-label="Уверенность узла {fmt(selected.confidence)} из 1">
        <span class="bar__fill" style="width: {confidenceWidth(selected.confidence)}"></span>
      </div>

      <div class="map__conn">
        <p class="small">
          Связи узла · <span class="num">{connections.length}</span>
        </p>
        {#if connections.length === 0}
          <p class="micro muted">
            В этом срезе у узла нет связей — по корпусу он стоит особняком.
          </p>
        {:else}
          <ul class="map__conn-list">
            {#each connections as conn, i (`${conn.edge.id}-${i}`)}
              <li>
                <span class="micro muted map__dir">{conn.outgoing ? '→' : '←'}</span>
                <span class="small map__rel">
                  {relationLabel(conn.edge.relation)} <code>{conn.edge.relation}</code>
                </span>
                {#if conn.other}
                  {@const target = conn.other}
                  <button type="button" class="map__jump" onclick={() => (selected = target)}>
                    {target.label}
                    <span class="micro muted">{typeLabel(target.type)}</span>
                  </button>
                {:else}
                  <span class="micro muted map__hidden">узел вне среза · <code>{conn.otherId}</code></span>
                {/if}
                <span class="num micro map__conf">{fmt(conn.edge.confidence)}</span>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    </div>
  {/if}
{/snippet}

{#snippet evidenceBlock()}
  {#if selected}
    <div class="map__evidence">
      <div class="row row--between">
        <h3 class="h4">Доказательства узла</h3>
        {#if findingsStatus === 'ready'}
          <p class="micro muted">находок: <span class="num">{relatedFindings.length}</span></p>
        {/if}
      </div>

      {#if findingsStatus === 'loading' || findingsStatus === 'idle'}
        <div class="stack" style="--gap: var(--s3)">
          <p class="micro muted">
            Читаем находки корпуса: доказательства узла появятся здесь.
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
        <p class="small muted">
          Узел <code>{selected.id}</code> стоит в срезе без связанного утверждения: он мог
          попасть на карту как сущность или документ, из которого ещё не извлечён тезис с
          доказательствами.
        </p>
        <div class="row">
          <Button href="/findings" variant="quiet" size="sm">Все находки корпуса</Button>
          <Button href="/research" variant="ghost" size="sm">Спросить по этому узлу</Button>
        </div>

        {#if nodeObs.broken}
          <Notice tone="warn" title="Числовые условия узла не читаются">
            Условия лежат в узле в виде, который не разбирается как список наблюдений, —
            печатать их как величины нельзя.
          </Notice>
        {:else if nodeObs.rows.length > 0}
          <div class="table-wrap">
            <table class="table">
              <caption class="micro muted map__caption">
                Числовые условия, зафиксированные в самом узле
              </caption>
              <thead>
                <tr>
                  <th>Свойство</th>
                  <th>Условие</th>
                  <th>Значение</th>
                  <th>Единица</th>
                  <th>Пересчёт</th>
                  <th>Как в источнике</th>
                </tr>
              </thead>
              <tbody>
                {#each nodeObs.rows as obs, i (`${selected.id}-nobs-${i}`)}
                  <tr>
                    <td><code>{obs.property_name}</code></td>
                    <td>{OPERATOR_RU[obs.operator] ?? obs.operator} <code>{obs.operator}</code></td>
                    <td class="num">{obsValue(obs)}</td>
                    <td><code>{obs.unit || '—'}</code></td>
                    <td class="num">{obsNormalized(obs)}</td>
                    <td><code>{obs.raw_text}</code></td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
          <p class="micro muted">
            Локатора первоисточника здесь нет: условия пришли вместе с узлом, а не из
            находки с доказательствами.
          </p>
        {/if}
      {:else}
        {#each relatedFindings as finding (finding.id)}
          <article class="map__claim">
            <div class="row row--between">
              <p class="eyebrow">
                {finding.subject || 'субъект не задан'}
                {#if finding.predicate}· <code>{finding.predicate}</code>{/if}
                · версия <span class="num">{finding.version}</span>
              </p>
              <StatusPill status={finding.status} label={STATUS_RU[finding.status] ?? finding.status} />
            </div>

            <h4 class="h4">{finding.statement}</h4>

            <dl class="kv">
              <dt>уверенность</dt>
              <dd class="num">{fmt(finding.confidence)}</dd>
              <dt>класс данных</dt>
              <dd>{DATA_CLASS_LABELS[finding.data_class]}</dd>
              <dt>находка</dt>
              <dd><code>{finding.id}</code></dd>
              {#if finding.superseded_by}
                <dt>заменена</dt>
                <dd><code>{finding.superseded_by}</code></dd>
              {/if}
            </dl>

            <div class="bar" role="img" aria-label="Уверенность находки {fmt(finding.confidence)} из 1">
              <span
                class="bar__fill bar__fill--{finding.status === 'disputed' ? 'disputed' : 'consensus'}"
                style="width: {confidenceWidth(finding.confidence)}"
              ></span>
            </div>

            {#if finding.observations.length > 0}
              <div class="table-wrap">
                <table class="table">
                  <caption class="micro muted map__caption">Числовые условия утверждения</caption>
                  <thead>
                    <tr>
                      <th>Свойство</th>
                      <th>Условие</th>
                      <th>Значение</th>
                      <th>Единица</th>
                      <th>Пересчёт</th>
                      <th>Как в источнике</th>
                    </tr>
                  </thead>
                  <tbody>
                    {#each finding.observations as obs, i (`${finding.id}-obs-${i}`)}
                      <tr>
                        <td><code>{obs.property_name}</code></td>
                        <td>
                          {OPERATOR_RU[obs.operator] ?? obs.operator} <code>{obs.operator}</code>
                        </td>
                        <td class="num">{obsValue(obs)}</td>
                        <td><code>{obs.unit || '—'}</code></td>
                        <td class="num">{obsNormalized(obs)}</td>
                        <td><code>{obs.raw_text}</code></td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
            {:else}
              <p class="micro muted">
                Утверждение без числовых наблюдений: сравнение идёт по формулировке и
                локатору, а не по величине.
              </p>
            {/if}

            <div class="map__ev">
              <p class="small">Доказательства · <span class="num">{finding.evidence.length}</span></p>
              {#if finding.evidence.length === 0}
                <p class="micro muted">
                  У утверждения нет ни одного локатора — проверить первоисточник по этой
                  находке нельзя.
                </p>
              {:else}
                {#each finding.evidence as ev, i (`${finding.id}-ev-${i}`)}
                  <figure class="map__evidence-item">
                    <blockquote class="quote">{ev.quote}</blockquote>
                    <figcaption class="locator">
                      <Icon name="doc" size={14} />
                      <span>{ev.source_title || 'источник без названия'}</span>
                      {#if ev.page != null}<span>стр. <span class="num">{ev.page}</span></span>{/if}
                      {#if ev.sheet}<span>лист <code>{ev.sheet}</code></span>{/if}
                      {#if ev.cell_range}<span>ячейки <code>{ev.cell_range}</code></span>{/if}
                      {#if ev.char_start != null && ev.char_end != null}
                        <span>
                          символы <span class="num">{ev.char_start}</span>–<span class="num">{ev.char_end}</span>
                        </span>
                      {/if}
                      <code>{ev.document_id}</code>
                    </figcaption>
                  </figure>
                {/each}
              {/if}
            </div>

            <div class="row">
              <Button href="/findings" variant="quiet" size="sm">Раздел «Находки»</Button>
              {#if finding.status === 'disputed'}
                <Button href="/conflicts" variant="quiet" size="sm">Разбор расхождения</Button>
              {/if}
            </div>
          </article>
        {/each}
      {/if}
    </div>
  {/if}
{/snippet}

<div class="page map-page">
  <div class="wrap">
    <!-- Узкий экран отдаёт вертикаль полю карты: метку раздела на нём заменяет
         заголовок. -->
    <SectionHead level="1" eyebrow={narrow ? '' : 'Граф доказательств'} title="Карта связей корпуса">
      {#if status === 'ready'}
        <div class="map__head-tools">
          <p class="micro muted map__meta">
            срез <span class="num">{graph.nodes.length}</span> узлов ·
            <span class="num">{graph.edges.length}</span> связей
            {#if loadedAt}
              · получен <time datetime={loadedAt.toISOString()}>{loadedAtText}</time>
            {/if}
          </p>
          <Button variant="quiet" size="sm" icon="refresh" onclick={() => void load()}>
            Перечитать срез
          </Button>
        </div>
      {/if}
    </SectionHead>

    {#if access === 'checking'}
      <Panel tone="sunk">
        <div class="map__skeleton">
          <span class="skeleton map__skeleton-line"></span>
          <span class="skeleton map__skeleton-field"></span>
          <p class="micro muted">Уточняем доступ к корпусу — карту заранее не читаем.</p>
        </div>
      </Panel>
    {:else if access === 'denied' || status === 'denied'}
      <Panel tone="lav">
        <div class="panel__head">
          <h2 class="h3">Карта связей корпуса закрыта</h2>
          <StatusPill status="off" label="нет доступа" />
        </div>
        <p class="lead small">
          Карта связей и находки открываются при доступе к корпусу: его выдаёт администратор
          сервиса, на этом экране его не включить.
        </p>
        {#if error}<p class="micro muted">{error}</p>{/if}
        <div class="row">
          <Button href="/research" variant="quiet">Рабочее пространство</Button>
          <Button href="/dashboard" variant="ghost">Панель состояния</Button>
        </div>
      </Panel>
    {:else if status === 'loading' || status === 'idle'}
      <Panel tone="sunk">
        <div class="map__skeleton">
          <p class="micro muted">
            Собираем карту: узлы, связи и сообщества корпуса.
          </p>
          <span class="skeleton map__skeleton-line"></span>
          <span class="skeleton map__skeleton-line map__skeleton-line--short"></span>
          <span class="skeleton map__skeleton-field"></span>
          <span class="skeleton map__skeleton-line"></span>
          <span class="skeleton map__skeleton-line map__skeleton-line--short"></span>
        </div>
      </Panel>
    {:else if status === 'error'}
      <Panel tone="coral">
        <div class="panel__head">
          <h2 class="h3">Карта связей не загрузилась</h2>
        </div>
        <Notice tone="error" title="Карта связей не загрузилась">{error}</Notice>
        <div class="row">
          <Button variant="action" onclick={() => void load()}>Повторить запрос</Button>
          <Button href="/research" variant="quiet">Задать вопрос по корпусу</Button>
        </div>
      </Panel>
    {:else if graph.nodes.length === 0}
      <Empty
        icon="graph"
        title="Карта связей пуста"
        body="На карте пока ни одного узла: материалы, методы и выводы появятся вместе с документами корпуса и ответами на запросы."
      >
        {#snippet action()}
          <div class="row">
            <Button href="/research" variant="quiet">Рабочее пространство</Button>
            <Button href="/findings" variant="ghost">Находки корпуса</Button>
          </div>
        {/snippet}
      </Empty>
    {:else}
      <nav class="map__nav micro" aria-label="Переходы по карте">
        <a href="#corr-panel">К полю корреляции</a>
        <a href="#evidence">
          {selected ? 'К доказательствам узла' : 'Узел не выбран — доказательства появятся здесь'}
        </a>
        <a href="#map-text">К текстовому чтению карты</a>
      </nav>

      <div class="split map__work">
        <div id="corr-panel" class="map__stage">
          <GraphCanvas {graph} selectedId={selected?.id} onselect={(node) => (selected = node)} />
        </div>

        <aside class="stack map__rail" aria-label="Показания среза">
          <div class="grid grid--4 map__gauges">
            <Panel tone="sage" class="map__gauge">
              <p class="metric__num num">{graph.nodes.length}</p>
              <p class="small">{plural(graph.nodes.length, 'узел', 'узла', 'узлов')}</p>
              <p class="micro muted">типов в срезе: <span class="num">{typeCount}</span></p>
            </Panel>
            <Panel tone="lav" class="map__gauge">
              <p class="metric__num num">{graph.edges.length}</p>
              <p class="small">{plural(graph.edges.length, 'связь', 'связи', 'связей')}</p>
              <p class="micro muted">отношения читаются у выбранного узла</p>
            </Panel>
            <Panel tone="coral" class="map__gauge">
              <p class="metric__num num">{graph.communities.length}</p>
              <p class="small">
                {plural(graph.communities.length, 'сообщество', 'сообщества', 'сообществ')}
              </p>
              <p class="micro muted">
                {#if graph.communities.length > 0}
                  имена сообществ подобраны по ключевым словам меток
                {:else}
                  сообщества этого среза ещё не собраны
                {/if}
              </p>
            </Panel>
            <Panel tone="default" class="map__gauge">
              <p class="metric__num num">{classCount}</p>
              <p class="small">
                {plural(classCount, 'класс данных', 'класса данных', 'классов данных')}
              </p>
              <p class="micro muted">в срезе: {classLabels || '—'}</p>
            </Panel>
          </div>

          <div class="acc">
            <button
              type="button"
              class="acc__head"
              aria-expanded={helpOpen}
              aria-controls="map-read-body"
              onclick={() => (helpOpen = !helpOpen)}
            >
              <span>Как устроена карта</span>
              <Icon name="plus" size={16} class="acc__icon" />
            </button>
            {#if helpOpen}
              <div class="acc__body" id="map-read-body">
                <p>
                  Узлы собраны кластерами по типам онтологии: материал, метод, вывод, источник.
                  Диск — узел, его тон задаёт тип, размер — уверенность и число связей; штрихи
                  между дисками — отношения. Любой узел открывается с клавиатуры и раскрывает
                  находки с локаторами первоисточника.
                </p>
              </div>
            {/if}
          </div>
        </aside>
      </div>

      <section id="evidence" class="map__inspector">
        {#if selected && narrow}
          <Sheet
            title="Узел карты: {selected.label}"
            description="Инспектор узла с доказательствами; на широком экране он стоит под картой, а не шторкой."
            onclose={() => (selected = null)}
            width="760px"
          >
            {@render nodeBody()}
            {@render evidenceBlock()}
          </Sheet>
        {:else}
          <Panel tone={selected ? 'default' : 'sunk'} raised={Boolean(selected)}>
            {#if selected}
              <div class="panel__head">
                <div>
                  <p class="eyebrow">
                    <Icon name="pin" size={16} />
                    выбранный узел · {typeLabel(selected.type)} <code>{selected.type}</code>
                  </p>
                  <h2 class="h3">{selected.label}</h2>
                </div>
                <Button variant="ghost" size="sm" onclick={() => (selected = null)}>Снять выбор</Button>
              </div>
              <div class="split map__split">
                <div class="stack" style="--gap: var(--s5)">
                  {@render nodeBody()}
                </div>
                <div class="stack" style="--gap: var(--s4)">
                  {@render evidenceBlock()}
                </div>
              </div>
            {:else}
              <Empty
                icon="target"
                title="Узел не выбран"
                body="Tab — по дискам поля, стрелки — ход к ближайшему узлу в направлении, Enter или Space — выбрать, Esc — снять выбор. Выбранный узел подсвечивает свои связи на карте и раскрывает доказательства здесь."
              />
            {/if}
          </Panel>
        {/if}
      </section>
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
    white-space: normal;
  }

  .map__nav {
    display: flex;
    gap: var(--s4);
    flex-wrap: nowrap;
    overflow-x: auto;
    scrollbar-width: thin;
    padding-block-end: var(--s1);
  }

  .map__nav a {
    white-space: nowrap;
  }

  /* Рабочая зона экрана: поле корреляции тянется на остаток вьюпорта,
     показания среза и пояснение чтения уходят в правый рельс. */
  .map__work {
    --gap: var(--s5);
    grid-template-columns: minmax(0, 1fr) var(--rail-w);
    align-items: start;
  }

  .map__rail {
    --gap: var(--s4);
  }

  .map__gauges {
    --gap: var(--s3);
  }

  .map__gauges p {
    margin: 0;
  }

  .map__gauges .metric__num {
    font-size: clamp(24px, 2.2vw, 30px);
  }

  /* Плотность карточек показания: рельс уже страницы, поля панели тоже меньше. */
  :global(.panel.map__gauge) {
    padding: var(--s4);
    border-radius: var(--r-lg);
  }

  .map__skeleton {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  /* Загрузка без скачка вёрстки: очертания поля и строк занимают место сразу. */
  .map__skeleton-line {
    height: 14px;
    width: 100%;
  }

  .map__skeleton-line--short {
    width: 46%;
  }

  .map__skeleton-field {
    height: clamp(320px, 48dvh, 500px);
    border-radius: var(--r-xl);
  }

  .map__skeleton p {
    margin: 0;
  }

  #corr-panel,
  #evidence {
    scroll-margin-top: calc(var(--topbar-h) + var(--s5));
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

  .map__conn-list {
    list-style: none;
    margin: var(--s2) 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }

  .map__conn-list li {
    display: grid;
    grid-template-columns: 20px minmax(96px, 0.9fr) minmax(0, 1.4fr) 52px;
    gap: var(--s3);
    align-items: baseline;
    padding: var(--s2) 0;
    border-top: 1px solid var(--line-soft);
  }

  .map__dir {
    font-family: var(--font-data);
    color: var(--ink-3);
  }

  .map__rel {
    color: var(--ink-2);
  }

  .map__rel code,
  .map__hidden code,
  .kv code,
  .map__claim code {
    font-family: var(--font-data);
    font-size: var(--t-micro);
    color: var(--ink-2);
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

    /* Рельс не тащится за узким экраном: показания среза становятся лентой
       под полем карты, а не второй колонкой. */
    .map__work {
      grid-template-columns: minmax(0, 1fr);
    }

    .map__rail {
      --gap: var(--s5);
    }
  }

  @media (max-width: 700px) {
    .map__conn-list li {
      grid-template-columns: 20px minmax(0, 1fr) 52px;
    }

    .map__conf {
      display: none;
    }
  }
</style>
