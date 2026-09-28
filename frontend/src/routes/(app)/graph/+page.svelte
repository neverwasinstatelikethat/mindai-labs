<script lang="ts">
  // Карта связей: обзор корпуса → цепочки области (GraphMap) + инспектор
  // выбранного узла с доказательствами. Инспектор показывается, только когда
  // узел выбран: до выбора экран принадлежит карте, и держать под ней пустую
  // панель на весь экран нечего.
  import { api, ApiError } from '$lib/api';
  import GraphMap, { classOf } from '$lib/GraphMap.svelte';
  import { navLabel } from '$lib/nav';
  import { countOf } from '$lib/format';
  import { session } from '$lib/sessionStore.svelte';
  import {
    MAP_KNOWLEDGE_STATUS_LABELS,
    MAP_META_LABELS,
    OPERATOR_WORD,
    PREDICATE_LABELS,
    STATUS_SHORT,
    SUBJECT_LABELS,
    knownTerm,
    mapNodeType,
    mapRelationKnown,
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
  // Выбранная связь — состояние экрана: поле подсвечивает её, инспектор описывает
  // словами и ведёт к обоим узлам.
  let pickedEdge = $state<GraphEdge | null>(null);

  // Доказательства берутся из настоящих находок корпуса: /api/v1/findings отдаёт
  // тот же Finding, что и ответ запроса, уже срезанный по классу данных сессии.
  let findings = $state<FindingListItem[]>([]);
  let findingsStatus = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
  let findingsError = $state('');
  let findingsRequested = false;

  // Инспектор на узком экране — нижняя шторка, а не вторая колонка.
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

  const nf = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 });
  function fmt(v: number): string {
    return nf.format(v);
  }

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
   * Узел → находки, которым он принадлежит: только совпадение идентификаторов.
   * claim-узел `claim-…` и находка `finding-claim-…`, узел документа
   * `document-<uuid>` и `evidence.document_id`.
   *
   * Прежняя привязка по подстроке метки («метка встречается в формулировке»)
   * превращала «Доказательства узла» хаба в стену из сотен несвязанных тезисов:
   * пересечение по идентификатору — единственная связка, которую нельзя
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

  /** Связи выбранного узла: направление и вторая сторона — из того же среза. */
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

  /** Смена узла обнуляет раскрытия и выбор связи: чужое состояние не держим. */
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

  // Метаданные узла: переводим известные ключи словарём карты, JSON-блоб и
  // неизвестные ключи не печатаем. Эти строки — служебные, они живут под
  // раскрытием «Служебные данные», а не в фактах узла.
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
      // год=0 — сервисный маркер «года нет», печатать его нельзя
      if (text.trim() === '' || (key === 'year' && text === '0')) continue;
      rows.push({ key, label, value: text });
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
        <dd>{mapNodeType(selected.type)}</dd>
        <dt>класс данных</dt>
        <dd><StatusPill status={cls.pill} label={cls.ru} /></dd>
        <dt>уверенность</dt>
        <dd class="num">{fmt(selected.confidence)}</dd>
        <dt>связей</dt>
        <dd class="num">
          {connections.length}
          <span class="micro muted">· исходящих {outgoingCount}, входящих {connections.length - outgoingCount}</span>
        </dd>
      </dl>

      <div class="bar" role="img" aria-label="Уверенность узла {fmt(selected.confidence)} из 1">
        <span class="bar__fill" style="width: {confidenceWidth(selected.confidence)}"></span>
      </div>

      <div class="map__conn">
        <p class="small">Связи узла · <span class="num">{connections.length}</span></p>
        {#if connections.length === 0}
          <p class="micro muted">
            В этом срезе у узла нет связей — по корпусу он стоит особняком.
          </p>
        {:else}
          <p class="micro muted map__list-head">
            направление · отношение · второй узел · уверенность
          </p>
          <ul class="map__conn-list">
            {#each shownConnections as conn, i (`${conn.edge.id}-${i}`)}
              <li>
                <span class="micro muted map__dir">{conn.outgoing ? 'исходит' : 'входит'}</span>
                <span class="small map__rel">{mapRelationLabel(conn.edge.relation)}</span>
                {#if conn.other}
                  {@const target = conn.other}
                  <button type="button" class="map__jump" onclick={() => pick(target)}>
                    {target.label}
                    <span class="micro muted">{mapNodeType(target.type)}</span>
                  </button>
                {:else}
                  <span class="micro muted">узел вне текущего среза</span>
                {/if}
                <span class="num micro map__conf">{fmt(conn.edge.confidence)}</span>
              </li>
            {/each}
          </ul>
          {#if connections.length > shownConnections.length}
            <Button variant="quiet" size="sm" onclick={() => (connectionsAll = true)}>
              Показать все связи
              <span class="num">
                {countOf(connections.length - shownConnections.length, 'связь', 'связи', 'связей')}
              </span>
            </Button>
          {/if}
        {/if}
      </div>

      <details class="map__tech">
        <summary class="micro">Служебные данные узла</summary>
        <dl class="kv">
          <dt>код узла</dt>
          <dd><code class="tech">{selected.id}</code></dd>
          <dt>тип узла</dt>
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

{#snippet evidenceBlock()}
  {#if selected}
    <div class="map__evidence">
      <div class="row row--between">
        <h3 class="h4">Доказательства узла</h3>
        {#if findingsStatus === 'ready'}
          <p class="micro muted">
            находок: <span class="num">{relatedFindings.length}</span>
            {#if relatedFindings.length > shownFindings.length}
              · показано <span class="num">{shownFindings.length}</span>
            {/if}
          </p>
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
          Этот узел стоит в срезе без связанного утверждения: он мог попасть на карту как
          сущность или документ, из которого ещё не извлечён тезис с доказательствами.
        </p>
        <div class="row">
          <Button href="/findings" variant="quiet" size="sm">Находки корпуса</Button>
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
                    <td><code class="tech">{obs.property_name}</code></td>
                    <td>{OPERATOR_WORD[obs.operator]} <code class="tech">{obs.operator}</code></td>
                    <td class="num">{obsValue(obs)}</td>
                    <td><code class="tech">{obs.unit || '—'}</code></td>
                    <td class="num">{obsNormalized(obs)}</td>
                    <td><code class="tech">{obs.raw_text}</code></td>
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
        {#each shownFindings as finding (finding.id)}
          {@const subjectName = knownTerm(SUBJECT_LABELS, finding.subject)}
          {@const predicateName = knownTerm(PREDICATE_LABELS, finding.predicate)}
          <article class="map__claim">
            <div class="row row--between">
              <p class="eyebrow">
                {#if subjectName}{subjectName}{:else}утверждение без субъекта{/if}
                {#if predicateName} · {predicateName}{/if}
                · версия <span class="num">{finding.version}</span>
              </p>
              <StatusPill status={finding.status} label={STATUS_SHORT[finding.status]} />
            </div>

            <h4 class="h4">{finding.statement}</h4>

            <dl class="kv">
              <dt>уверенность</dt>
              <dd class="num">{fmt(finding.confidence)}</dd>
              <dt>класс данных</dt>
              <dd>{DATA_CLASS_LABELS[finding.data_class]}</dd>
              {#if finding.superseded_by}
                <dt>заменена</dt>
                <dd>более новой версией — код версии в служебных данных</dd>
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
                        <td><code class="tech">{obs.property_name}</code></td>
                        <td>
                          {OPERATOR_WORD[obs.operator]} <code class="tech">{obs.operator}</code>
                        </td>
                        <td class="num">{obsValue(obs)}</td>
                        <td><code class="tech">{obs.unit || '—'}</code></td>
                        <td class="num">{obsNormalized(obs)}</td>
                        <td><code class="tech">{obs.raw_text}</code></td>
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
                      {#if ev.sheet}<span>лист <code class="tech">{ev.sheet}</code></span>{/if}
                      {#if ev.cell_range}
                        <span>ячейки <code class="tech">{ev.cell_range}</code></span>
                      {/if}
                      {#if ev.char_start != null && ev.char_end != null}
                        <span>
                          символы <span class="num">{ev.char_start}</span>–<span class="num">{ev.char_end}</span>
                        </span>
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
              </dl>
            </details>

            <div class="row">
              <Button href="/findings" variant="quiet" size="sm">Раздел «Находки»</Button>
              {#if finding.status === 'disputed'}
                <Button href="/conflicts" variant="quiet" size="sm">Разбор расхождения</Button>
              {/if}
            </div>
          </article>
        {/each}

        {#if relatedFindings.length > shownFindings.length}
          <Button variant="quiet" size="sm" onclick={() => (findingsAll = true)}>
            Показать все
            <span class="num">
              {countOf(relatedFindings.length - shownFindings.length, 'находка', 'находки', 'находок')}
            </span>
          </Button>
        {/if}
      {/if}
    </div>
  {/if}
{/snippet}

<div class="page map-page">
  <div class="wrap">
    <!-- Узкий экран отдаёт вертикаль полю карты: метку раздела на нём заменяет
         заголовок. -->
    <SectionHead level="1" eyebrow={narrow ? '' : 'Корпус · связи'} title="Карта связей корпуса">
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
          <Button href="/dashboard" variant="ghost">{navLabel('/dashboard')}</Button>
        </div>
      </Panel>
    {:else if status === 'loading' || status === 'idle'}
      <Panel tone="sunk">
        <div class="map__skeleton">
          <p class="micro muted">
            Собираем карту: узлы, связи и сообщества корпуса.
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
        body="Сервер вернул срез без узлов: на карте появляются только те документы, из которых уже извлечены утверждения. Пустой или непрочитанный корпус остаётся без дисков и штрихов."
      >
        {#snippet action()}
          <div class="row">
            <Button href="/findings" variant="quiet">Загрузить документ в находках</Button>
            <Button href="/research" variant="ghost">Задать вопрос по корпусу</Button>
          </div>
        {/snippet}
      </Empty>
    {:else}
      <GraphMap
        {graph}
        selectedId={selected ? selected.id : undefined}
        edgeId={pickedEdge ? pickedEdge.id : undefined}
        onselect={pick}
        onpickEdge={pickEdge}
      />

      {#if selected}
        <section id="evidence" class="map__inspector">
          {#if narrow}
            <Sheet
              title="Узел карты: {selected.label}"
              onclose={() => pick(null)}
              width="760px"
            >
              {@render nodeBody()}
              {@render evidenceBlock()}
            </Sheet>
          {:else}
            <Panel tone="default" raised>
              <div class="panel__head">
                <div>
                  <p class="eyebrow">
                    <Icon name="pin" size={16} />
                    выбранный узел · {mapNodeType(selected.type)}
                  </p>
                  <h2 class="h3">{selected.label}</h2>
                </div>
                <Button variant="ghost" size="sm" onclick={() => pick(null)}>Снять выбор</Button>
              </div>

              {#if pickedEdge}
                {@const fromNode = nodeIndex.get(pickedEdge.source) ?? null}
                {@const toNode = nodeIndex.get(pickedEdge.target) ?? null}
                <div class="map__edge-card">
                  <p class="eyebrow">
                    <Icon name="link" size={16} />
                    выбранная связь
                  </p>
                  <p class="small map__edge-line">
                    {mapRelationLabel(pickedEdge.relation)}
                    {#if !mapRelationKnown(pickedEdge.relation)}
                      <code class="tech">{pickedEdge.relation}</code>
                    {/if}
                    :
                    <button type="button" class="map__jump" onclick={() => fromNode && pick(fromNode)}>
                      {fromNode?.label ?? 'узел недоступен в этом срезе'}
                    </button>
                    <span aria-hidden="true">→</span>
                    <button type="button" class="map__jump" onclick={() => toNode && pick(toNode)}>
                      {toNode?.label ?? 'узел недоступен в этом срезе'}
                    </button>
                  </p>
                  <p class="micro muted">
                    уверенность связи <span class="num">{fmt(pickedEdge.confidence)}</span>
                    · класс данных {DATA_CLASS_LABELS[pickedEdge.data_class]}
                  </p>
                  <Button variant="ghost" size="sm" onclick={() => pickEdge(null)}>
                    Снять связь
                  </Button>
                </div>
              {/if}

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
    white-space: normal;
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

  .map__rel {
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
