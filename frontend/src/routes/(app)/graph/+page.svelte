<script lang="ts">
  // Карта связей: поле связей открыто сразу, поиск стоит строкой над ним, а
  // сведения о выбранной записи или выбранной связи живёт в одной колонке
  // рядом с полем. Колонка заполняется на месте выбора: до деталей не нужно
  // прокручивать экран, и они не прячутся в модалке.
  import { page } from '$app/state';
  import { api, ApiError } from '$lib/api';
  import GraphMap from '$lib/GraphMap.svelte';
  import SourceRef from '$lib/ui/SourceRef.svelte';
  import { navLabel } from '$lib/nav';
  import { countOf, num, plainName } from '$lib/format';
  import { session } from '$lib/sessionStore.svelte';
  import {
    ASK_SOURCE,
    MAP_INSPECTOR,
    MAP_LABEL_DENSITY,
    MAP_LINK_MISS,
    MAP_NAME_FALLBACK,
    MAP_NOUN,
    PROPERTY_LABELS,
    STATUS_SHORT,
    SUBJECT_LABELS,
    TERM_FALLBACKS,
    describeValue,
    knownTerm,
    mapAreaName,
    mapNodeType,
    mapRelationLabel,
  } from '$lib/terms';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import Segmented from '$lib/ui/Segmented.svelte';
  import Sheet from '$lib/ui/Sheet.svelte';
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
  let selected = $state<GraphNode | null>(null);
  // Выбранная связь живёт на экране: поле подсвечивает её, инспектор описывает
  // словами и ведёт к обеим записям.
  let pickedEdge = $state<GraphEdge | null>(null);
  // Подписи связей по умолчанию только у выбранной записи; человек может
  // включить их для всего поля. Это единственный переключатель вида на экране:
  // режимов «поиск» и «карта» больше нет, поле связей открыто сразу.
  let labelMode = $state<'focus' | 'all'>('focus');
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
    // Ключ реестра записывается в двух формах: сырой (`carbon_dioxide`) и
    // человеческой (`carbon dioxide`). Метка узла на экран выходит уже без
    // подчёркиваний, и по обеим формам она находится одинаково.
    const map = new Map<string, string>();
    const register = (key: string, value: string) => {
      const clean = key.trim().toLowerCase();
      if (clean === '') return;
      map.set(clean, value);
      map.set(clean.replaceAll('_', ' '), value);
    };
    for (const node of graph.nodes) {
      register(node.id, node.label);
      register(node.label, node.label);
      const aliases = node.metadata['aliases'];
      if (typeof aliases === 'string') {
        for (const alias of aliases.split(',')) register(alias, node.label);
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

  /** Область записи: тем же именем, что и на поле, — со служебным ключом вне. */
  function areaOf(node: GraphNode): string {
    return mapAreaName(node.metadata['community']);
  }

  /**
   * Ссылка на находку и её источник: `/findings` читает `claim` (открывает
   * шторку первоисточника) и `document` (оставляет в списке этот документ).
   * Контракт тот же, что у перехода из ответа в «Находках»: карта своих
   * параметров не изобретает. Для записи-документа источник называется прямо:
   * находка может ссылаться и на другой документ этого среза.
   */
  function findingsHref(finding: FindingListItem, documentId = ''): string {
    const params = new URLSearchParams();
    params.set('claim', finding.id);
    const doc = documentId !== '' ? documentId : (finding.evidence[0]?.document_id ?? '');
    if (doc !== '') params.set('document', doc);
    return `/findings?${params.toString()}`;
  }

  /** Ссылка на источник: `/findings?document=` оставляет в списке этот документ. */
  function documentHref(node: GraphNode): string | undefined {
    if (!node.id.startsWith('document-')) return undefined;
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

  /** Имена связей отсутствуют у всего списка сразу: тогда заглушку нечего
   *  повторять в каждой строке, и факт говорит одной строкой над списком. */
  const relationsUnnamed = $derived(
    shownConnections.length > 0 &&
      shownConnections.every(
        (conn) => mapRelationLabel(conn.edge.relation) === mapRelationLabel(''),
      ),
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
   * находка самой записи вместе с её источником, затем документ этой записи,
   * если находки в показанной части корпуса нет, и только потом источник по
   * связи. Когда нет ни того, ни другого, ссылки нет: кнопка, ведущая в никуда,
   * на экран не выходит.
   */
  const findingHref = $derived.by<string | undefined>(() => {
    if (!selected) return undefined;
    const docId = selected.id.startsWith('document-')
      ? selected.id.slice('document-'.length)
      : '';
    const own = relatedFindings[0];
    if (own) return findingsHref(own, docId);
    if (docId !== '') return documentHref(selected);
    if (selected.type === 'claim') {
      const code = findingIdFor(selected) ?? `finding-${selected.id}`;
      return `/findings?claim=${encodeURIComponent(code)}`;
    }
    return sourceDocument ? documentHref(sourceDocument) : undefined;
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

  /** Единственное «Снять выбор» на экране: оно гасит и связь, и запись. */
  function clearSelection(): void {
    pickedEdge = null;
    pick(null);
  }

  /**
   * Концы выбранной связи: записи берутся из этого же среза, а если конца в
   * срезе нет, строка называется словами, а не служебным кодом.
   */
  const edgeEnds = $derived.by<{ from: GraphNode | null; to: GraphNode | null }>(() => {
    if (!pickedEdge) return { from: null, to: null };
    return {
      from: nodeIndex.get(pickedEdge.source) ?? null,
      to: nodeIndex.get(pickedEdge.target) ?? null,
    };
  });

  /**
   * Заголовок колонки сведений: он один и для записи, и для связи, поэтому
   * человек понимает, что открыто, не сравнивая два разных контейнера.
   */
  const inspectorTitle = $derived(
    pickedEdge
      ? mapRelationLabel(pickedEdge.relation)
      : (selected?.label ?? ''),
  );

  /** Подпись над заголовком: что именно выбрано. */
  const inspectorKind = $derived(
    pickedEdge ? MAP_INSPECTOR.link : MAP_INSPECTOR.record,
  );

  let requestSeq = 0;
  async function load(): Promise<void> {
    const call = ++requestSeq;
    status = 'loading';
    error = '';
    try {
      const snapshot = await api.graph();
      if (call !== requestSeq) return;
      // Узлы приходят из графа служебной формой имени (`Carbon_dioxide`).
      // Исправляется один раз на входе: поиск, подписи поля, строки цепочки и
      // шапка инспектора читают уже человеческое имя.
      graph = {
        ...snapshot,
        nodes: snapshot.nodes.map((node) => ({ ...node, label: plainName(node.label) })),
      };
      pick(null);
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
      // Строка сервера называется не `page`: адрес экрана читается через `page`
      // из `$app/state`, и две разные «страницы» в одном файле читались бы плохо.
      const served = await api.findings(
        undefined,
        undefined,
        FINDINGS_PAGE_SIZE,
        more ? findingsShown : 0,
      );
      findings = more ? mergePages(findings, served.items) : served.items;
      findingsShown = more ? findingsShown + served.items.length : served.items.length;
      findingsTotal = served.total;
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

  /**
   * Глубокая ссылка `/graph?claim=`: переход из ответа обещает открыть на карте
   * ту же запись, к которой он ведёт. Код находки и код записи различаются
   * соглашением `finding-`, поэтому узел ищется по всем вариантам этого среза.
   * Записи на карте нет — экран говорит об этом словами: молчаливый обзор
   * областей ответом на переход не считается.
   */
  let linkHandledFor = $state('');
  let linkMiss = $state(false);

  /** Запись карты по коду находки: `finding-claim-…` против `claim-…`. */
  function nodeByFindingCode(code: string): GraphNode | null {
    const bare = code.startsWith('finding-') ? code.slice('finding-'.length) : code;
    for (const id of [code, bare, `claim-${bare}`, `finding-${bare}`]) {
      const node = nodeIndex.get(id);
      if (node) return node;
    }
    return null;
  }

  $effect(() => {
    if (status !== 'ready') return;
    const code = (page.url.searchParams.get('claim') ?? '').trim();
    if (code === '' || code === linkHandledFor) return;
    linkHandledFor = code;
    const node = nodeByFindingCode(code);
    linkMiss = node === null;
    if (!node) return;
    // Переход просит детали прямо: колонка сведений заполняется выбором на
    // месте, и прокручивать к ней экран больше не нужно.
    pick(node);
  });

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

</script>

<svelte:head>
  <title>Связи — StormIdea</title>
</svelte:head>

{#snippet nodeBody()}
  {#if selected}
    <div class="stack map__node-facts">
      <!-- Вид записи назван подписью над заголовком колонки: вторая строка с
           тем же словом внутри колонки только повторяла бы его. -->
      <dl class="kv">
        <dt>область на карте</dt>
        <dd>{areaOf(selected)}</dd>
      </dl>

      <div class="map__conn">
        <div class="row row--between">
          <h3 class="h4">Связи этой записи</h3>
          {#if connections.length > shownConnections.length}
            <!-- Число называет только скрытую часть: когда список весь на
                 экране, счётчик повторял бы то, что посчитано глазами. -->
            <p class="micro muted map__count">
              показано
              <span class="num">{shownConnections.length}</span>
              из
              <span class="num">{connections.length}</span>
            </p>
          {/if}
        </div>
        {#if connections.length === 0}
          <p class="micro muted">В этой карте у записи нет связей.</p>
        {:else}
          {#if relationsUnnamed}
            <p class="micro muted">
              Имя связи в графе не задано ни у одной строки: показаны направление и запись.
            </p>
          {/if}
          <ul class="map__conn-list">
            {#each shownConnections as conn, i (`${conn.edge.id}-${i}`)}
              <!-- Направление и имя связи стоят строкой выше, а запись занимает
                   всю ширину колонки: в три колонки длинное имя утверждения
                   ломалось по буквам. -->
              <li>
                <span class="micro muted map__conn-meta">
                  <span>{conn.outgoing ? 'исходит' : 'входит'}</span>
                  {#if !relationsUnnamed}
                    <button
                      type="button"
                      class="map__rel"
                      aria-pressed={pickedEdge?.id === conn.edge.id}
                      onclick={() => pickEdge(pickedEdge?.id === conn.edge.id ? null : conn.edge)}
                    >
                      <span>{mapRelationLabel(conn.edge.relation)}</span>
                    </button>
                  {/if}
                </span>
                {#if conn.other}
                  {@const target = conn.other}
                  <button type="button" class="map__jump" onclick={() => pick(target)}>
                    {target.label}
                    <span class="micro muted">{mapNodeType(target.type)}</span>
                  </button>
                {:else}
                  <span class="micro muted">запись вне этой карты</span>
                {/if}
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
{/snippet}

{#snippet evidenceBlock()}
  {#if selected}
    <div class="map__evidence">
      <div class="row row--between">
        <h3 class="h4">Доказательства этой записи</h3>
        {#if findingsStatus === 'ready' && relatedFindings.length > 0}
          <!-- Нуль здесь только повторил бы строку «доказательств не найдено». -->
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
                <span>{predicateName(finding)}: {subjectName(finding)}</span>
                {#if finding.version > 1}
                  <span>версия <span class="num">{num(finding.version)}</span></span>
                {/if}
              </p>
              <span class="micro status-word" data-status={finding.status}>
                {STATUS_SHORT[finding.status]}
              </span>
            </div>

            <h4 class="h4 map__claim-title">
              <!-- Формулировка утверждения и есть вход в него: отдельная кнопка
                   «Открыть» под текстом только повторяет тот же путь. -->
              <a class="map__claim-open" href={findingsHref(finding)}>{finding.statement}</a>
            </h4>

            {#if finding.observations.length > 0}
              {@render obsTable(finding.observations, 'Числовые условия утверждения')}
            {/if}

            {#if finding.evidence.length > 0}
              <div class="map__ev">
                <p class="small">Места в источнике</p>
                {#each finding.evidence as ev, i (`${finding.id}-ev-${i}`)}
                  <SourceRef evidence={ev} quote />
                {/each}
              </div>
            {/if}

            {#if finding.status === 'disputed'}
              <Button href="/findings?facet=numbers" variant="link" size="sm">
                Открыть расхождение
              </Button>
            {/if}
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

{#snippet edgeBody()}
  {#if pickedEdge}
    <div class="stack map__edge-card">
      <dl class="kv">
        <dt>отношение</dt>
        <dd>{mapRelationLabel(pickedEdge.relation)}</dd>
        <dt>область на карте</dt>
        <dd>{edgeEnds.from ? areaOf(edgeEnds.from) : MAP_NAME_FALLBACK.offDictionary}</dd>
      </dl>

      <!-- Оба конца связи живут в той же колонке, что и выбранная запись:
           отдельного модального окна для связи на широком экране больше нет. -->
      <div class="map__conn">
        <h3 class="h4">{MAP_INSPECTOR.linkOf}</h3>
        <ul class="map__edge-ends">
          <li>
            <span class="micro muted">исходит</span>
            {#if edgeEnds.from}
              {@const start = edgeEnds.from}
              <button type="button" class="map__jump" onclick={() => pick(start)}>
                {start.label}
                <span class="micro muted">{mapNodeType(start.type)}</span>
              </button>
            {:else}
              <span class="small">{MAP_NAME_FALLBACK.record}</span>
            {/if}
          </li>
          <li>
            <span class="micro muted">входит</span>
            {#if edgeEnds.to}
              {@const finish = edgeEnds.to}
              <button type="button" class="map__jump" onclick={() => pick(finish)}>
                {finish.label}
                <span class="micro muted">{mapNodeType(finish.type)}</span>
              </button>
            {:else}
              <span class="small">{MAP_NAME_FALLBACK.record}</span>
            {/if}
          </li>
        </ul>
      </div>

      {#if selected}
        <p class="small muted map__edge-back">
          {MAP_INSPECTOR.linkInRecord}:
          <button type="button" class="map__jump" onclick={() => pick(selected)}>
            {selected.label}
          </button>
        </p>
      {/if}
    </div>
  {/if}
{/snippet}

{#snippet inspectorHead()}
  <div class="map__insp-head">
    <div class="map__insp-name">
      <p class="eyebrow">
        <Icon name="pin" size={16} />
        {inspectorKind}
        {#if !pickedEdge && selected}
          <span>{mapNodeType(selected.type)}</span>
        {/if}
      </p>
      <h2 class="h3">{inspectorTitle}</h2>
    </div>
    <div class="row">
      {#if !pickedEdge && findingHref}
        <Button href={findingHref} variant="quiet" size="sm">
          {ASK_SOURCE.findings}
        </Button>
      {/if}
      <!-- «Снять выбор» на экране один: он живёт здесь, а повторный клик по
           выбранной строке поля делает то же самое. -->
      {#if selected || pickedEdge}
        <Button variant="ghost" size="sm" onclick={clearSelection}>
          {MAP_INSPECTOR.clear}
        </Button>
      {/if}
    </div>
  </div>
{/snippet}

<div class="page map-page">
  <div class="wrap">
    <SectionHead
      level="1"
      title="Связи"
      lead="Изучите, как связаны факты, гипотезы и источники."
    />

    {#if status === 'ready'}
      <!-- Инструменты поля идут по левому краю, а не в углу заголовка: колонка
           сведений прижата к правому краю и накрывала бы переключатель
           подписей ровно в тот момент, когда выбор сделан. -->
      <div class="map__head-tools">
        {#if graph.nodes.length > 0}
          <!-- Единственный переключатель вида на экране: плотность подписей
               на линиях. Режимов «поиск» и «карта» больше нет, поэтому
               переключать сюда нечего. -->
          <Segmented
            class="map__density-mode"
            label={MAP_LABEL_DENSITY.group}
            items={[
              { value: 'focus', label: MAP_LABEL_DENSITY.focus },
              { value: 'all', label: MAP_LABEL_DENSITY.all },
            ]}
            bind:value={labelMode}
          />
        {/if}
        <Button variant="quiet" size="sm" icon="refresh" onclick={() => void load()}>
          Обновить
        </Button>
      </div>
    {/if}

    {#if access === 'checking'}
      <Panel tone="sunk">
        <div class="map__skeleton">
          <span class="skeleton map__skeleton-line"></span>
          <span class="skeleton map__skeleton-field"></span>
          <p class="micro muted">Загружаем связи…</p>
        </div>
      </Panel>
    {:else if access === 'denied' || status === 'denied'}
      <Panel tone="lav">
        <div class="panel__head">
          <h2 class="h3">Нет доступа к материалам</h2>
        </div>
        <p class="lead small">
          Попросите владельца пространства открыть доступ к материалам.
        </p>
        {#if error}<p class="micro muted">{error}</p>{/if}
        <div class="row">
          <Button href="/research" variant="quiet">Перейти в рабочее пространство</Button>
        </div>
      </Panel>
    {:else if status === 'loading' || status === 'idle'}
      <Panel tone="sunk">
        <div class="map__skeleton">
          <p class="micro muted">
            Загружаем карту связей…
          </p>
          <span class="skeleton map__skeleton-line"></span>
          <span class="skeleton map__skeleton-field"></span>
        </div>
      </Panel>
    {:else if status === 'error'}
      <Panel tone="coral">
        <div class="panel__head">
          <h2 class="h3">Не удалось загрузить связи</h2>
        </div>
        <Notice tone="error">{error}</Notice>
        <div class="row">
          <Button variant="action" onclick={() => void load()}>Повторить</Button>
          <Button href="/research" variant="quiet">Вернуться в чат</Button>
        </div>
      </Panel>
    {:else if graph.nodes.length === 0}
      <Empty
        icon="graph"
        title="Связей пока нет"
        body="Загрузите материалы или начните с вопроса. Связанные факты появятся здесь."
      >
        {#snippet action()}
          <div class="row">
            <Button href="/findings" variant="quiet">Добавить материалы</Button>
            <Button href="/research" variant="ghost">Открыть чат</Button>
          </div>
        {/snippet}
      </Empty>
    {:else}
      {#if linkMiss}
        <Notice tone="warn" title={MAP_LINK_MISS.title}>{MAP_LINK_MISS.body}</Notice>
      {/if}

      <div class="map-lab">
        <div class="map-lab__field">
          <GraphMap
            {graph}
            {narrow}
            labelMode={labelMode}
            selectedId={selected ? selected.id : undefined}
            edgeId={pickedEdge ? pickedEdge.id : undefined}
            onselect={pick}
            onpickEdge={pickEdge}
          />
        </div>

        <!-- Колонка сведений: на широком экране справа от поля и прилипшая, на
             среднем становится нижней, но остаётся в том же контейнере с тем же
             заголовком. Пустое состояние говорит, куда нажать. -->
        {#if !narrow}
          <aside id="inspector" class="panel map-lab__insp">
            {#if pickedEdge}
              {@render inspectorHead()}
              {@render edgeBody()}
            {:else if selected}
              {@render inspectorHead()}
              <div class="stack map__insp-body">
                {@render nodeBody()}
                {@render evidenceBlock()}
              </div>
            {:else}
              <div class="map__insp-empty">
                <h2 class="h4">{MAP_INSPECTOR.emptyTitle}</h2>
                <p class="small muted">{MAP_INSPECTOR.emptyBody}</p>
                <p class="micro muted">{MAP_INSPECTOR.emptyLink}</p>
              </div>
            {/if}
          </aside>
        {/if}
      </div>

      <!-- На узком экране сведений справа нет места: они приходят шторкой
         снизу, тем же содержимым и тем же заголовком. -->
      {#if narrow && (selected || pickedEdge)}
        <Sheet
          title={pickedEdge ? inspectorTitle : (selected?.label ?? '')}
          onclose={clearSelection}
          width="760px"
        >
          <p class="eyebrow map__sheet-kind">
            <Icon name="pin" size={16} />
            {inspectorKind}
          </p>
          {#if pickedEdge}
            {@render edgeBody()}
          {:else}
            <div class="stack map__insp-body">
              {@render nodeBody()}
              {@render evidenceBlock()}
            </div>
          {/if}
        </Sheet>
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
    margin-block: calc(var(--s6) * -1 + var(--s3)) var(--s5);
  }

  /* Поле связей и колонка сведений. На широком экране это две колонки одной
     сетки: колонка прилипает к верху и прокручивается сама, поэтому конец
     доказательств дочитывается и без конца страницы. Поверх поля колонку не
     ставить — под ней пропала бы целая цепочка сущностей, а выбор записи как раз
     и делается ради этой цепочки. Ниже 1280 px колонка встаёт в поток под поле и
     не меняет ни заголовка, ни содержания. */
  .map-lab {
    display: grid;
    gap: var(--s5);
    grid-template-columns: minmax(0, 1fr);
    align-items: start;
  }

  .map-lab__field,
  .map-lab__insp {
    min-width: 0;
  }

  @media (min-width: 1280px) {
    .map-lab {
      grid-template-columns:
        minmax(0, 1fr)
        clamp(300px, 27vw, 460px);
    }

    .map-lab__insp {
      position: sticky;
      top: calc(var(--topbar-h) + var(--s4));
      max-height: calc(100dvh - var(--topbar-h) - var(--s8));
      padding-block-end: var(--s6);
      overflow-y: auto;
      overscroll-behavior: contain;
      /* Колонка прилипает и держит тень сцены: под ней прокручиваются карточки,
         и граница слоёв должна быть видна. Полоса прокрутки названа явно — без
         неё конец списка обрывается на сгибе, и не понять, что ниже есть. */
      box-shadow: var(--shadow-lift);
      scrollbar-width: thin;
      scrollbar-color: var(--line-strong) var(--surface-sunk);
    }
  }

  /* Шапка колонки одна и для записи, и для связи: два выбора выглядят
     одинаково, и человек понимает, что открыто. */
  .map__insp-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--s4);
    flex-wrap: wrap;
    margin-bottom: var(--s5);
  }

  .map__insp-name {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
    min-width: 0;
  }

  .map__insp-name h2 {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .map__insp-body {
    --gap: var(--s5);
  }

  /* Пустая колонка обязана сказать, куда нажать: без этой строки она
     выглядела бы сломанной панелью. */
  .map__insp-empty {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  .map__insp-empty h2,
  .map__insp-empty p {
    margin: 0;
  }

  .map__sheet-kind {
    margin: 0 0 var(--s4);
  }

  /* ── Состояния экрана ───────────────────────────────────────────────────── */
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

  /* ── Колонка сведений: запись ───────────────────────────────────────────── */
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

  /* Плотный список связей: линия между строками работает здесь разделителем
     колонок, а не рамкой блока. За его пределами рамок на экране нет. */
  .map__conn-list li {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
    padding: var(--s3) 0;
    border-top: 1px solid var(--line-soft);
  }

  .map__conn-meta {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: var(--s1) var(--s3);
    color: var(--ink-3);
  }

  /* Тип записи — короткое слово, резать его по слогам нельзя: перенос
     остаётся метке записи, тип стоит цельным словом. */
  .map__jump > span {
    flex: none;
    white-space: nowrap;
  }

  /* Отношение связи — действие: с клавиатуры оно выбирает связь и раскрывает
     её в этой же колонке, а не только перескакивает на вторую запись. */
  .map__rel {
    display: inline-flex;
    align-items: baseline;
    gap: var(--s2);
    min-width: 0;
    padding: var(--s1) var(--s2);
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

  /* Выбор связи читается тоном: обводка внутри плашки была вторым контуром. */
  .map__rel[aria-pressed='true'] {
    color: var(--ink);
    background: var(--sage);
  }

  .map__jump {
    display: inline-flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: var(--s1) var(--s2);
    border: 0;
    border-radius: var(--r-xs);
    background: none;
    padding: 0;
    color: var(--ink);
    font: inherit;
    text-align: left;
    cursor: pointer;
    min-width: 0;
    /* Метки узлов — часто одно длинное слово (например, имя материала или файла):
       без переноса оно распирает колонку и уходит за край шторки. */
    overflow-wrap: anywhere;
  }

  .map__jump:hover {
    color: var(--action-ink);
  }

  /* ── Колонка сведений: связь ────────────────────────────────────────────── */
  .map__edge-card {
    --gap: var(--s4);
  }

  /* Оба конца связи читаются плотным списком: направление слева, запись
     справа, тот же разделитель, что и у связей записи. */
  .map__edge-ends {
    list-style: none;
    margin: var(--s2) 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }

  .map__edge-ends li {
    display: grid;
    grid-template-columns: 62px minmax(0, 1fr);
    gap: var(--s2) var(--s3);
    align-items: baseline;
    padding: var(--s2) 0;
    border-top: 1px solid var(--line-soft);
  }

  /* ── Доказательства выбранной записи ────────────────────────────────────── */
  .map__evidence {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  /* Находка — плоская строка: блок от блока отделяется расстоянием, а не
     линией, и заполнение остаётся цитате. */
  .map__claim {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  /* Субъект, отношение и версия находки читаются подписями одной строки:
     плашка вокруг каждого слова удваивала бы форму там, где нужно слово. */
  .map__claim-tags {
    flex-wrap: wrap;
    min-width: 0;
    gap: var(--s1) var(--s4);
    color: var(--ink-3);
    font-weight: 400;
  }

  .map__claim-tags span {
    overflow-wrap: anywhere;
  }

  .map__claim-title {
    margin: 0;
    max-width: var(--maxw-measure);
  }

  .map__claim-open {
    color: inherit;
    text-decoration: none;
    border-radius: var(--r-sm);
    overflow-wrap: anywhere;
  }

  .map__claim-open:hover {
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .map__claim-open:focus-visible {
    outline: 2px solid var(--action-ink);
    outline-offset: 3px;
  }

  .map__ev {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
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
      flex-wrap: wrap;
    }

    .map__head-tools :global(.btn) {
      flex: none;
    }

    /* Концы связи в шторке выбранной связи: сетка на ширине шторки оставляла
       метке ~30 px, и слово разваливалось по буквам. */
    .map__edge-ends li {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: var(--s1) var(--s2);
    }
  }

</style>
