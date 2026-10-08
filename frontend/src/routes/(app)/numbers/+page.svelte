<script lang="ts">
  /**
   * Раздел «Числа»: сюда слиты прежние экраны сравнения и расхождений.
   *
   * Экран отвечает на три вопроса аналитика переключением фасета: сходятся ли
   * числа по показателю, где источники спорят и где они молчат. Все три фасета
   * читают один и тот же срез корпуса через `numbers.ts`, поэтому арифметика
   * расхождения на экране одна.
   *
   * Методы сравнения живут в одном модуле, а доказательства ведут к источнику.
   */
  import { browser } from '$app/environment';
  import { page } from '$app/state';
  import { tick } from 'svelte';
  import { api } from '$lib/api';
  import { countOf, dateTime, num } from '$lib/format';
  import {
    anyDivergence,
    bandOf,
    checkAgainstLimit,
    type Comparison,
    deltaFromBase,
    diverges,
    gapNotesOf,
    type GapKind,
    type LimitCheck,
    limitsOfFindings,
    observationText,
    propertyName,
    type PropertyLimit,
    sourceTitleOf,
    type TopicGroup,
    topicGroups,
    type ValuePoint,
    widestPair,
  } from '$lib/numbers';
  import { navLabel } from '$lib/nav';
  import { session } from '$lib/sessionStore.svelte';
  import {
    COMPARE_CELL_LABELS,
    COMPARE_LIMIT_SOURCE,
    CONFLICT_SIDE,
    DIVERGENCE_ACTION,
    DIVERGENCE_HELP,
    DIVERGENCE_SORT,
    DIVERGENCE_VIEW,
    GAP_KIND_LABELS,
    GAP_KIND_TITLES,
    GAPS_VIEW,
    QUEUE_ACTION,
    QUEUE_FAILURE,
    QUEUE_HEAD,
    QUEUE_HINTS,
    QUEUE_STATUS_LABELS,
    QUEUE_STATUS_TONE,
    QUEUE_WORDS,
    SCALE_WORDS,
    TOPIC_NO_PERCENT,
    TOPIC_NOT_COMPARABLE,
    TOPIC_WORDS,
    deltaPhrase,
    deniedNote,
    divergencesShownOf,
    gapBody,
    gapsCounter,
    limitTagText,
    noDivergenceBody,
    othersShown,
    outsideLimitText,
    queueCounter,
    queueOutcome,
    queueWaiting,
    retryNote,
    sectionLink,
    topicCounts,
    topicDifference,
    topicsShown,
  } from '$lib/terms';
  import type { ConflictCandidate, FindingListItem, NumericObservation } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Field from '$lib/ui/Field.svelte';
  import InfoDot from '$lib/ui/InfoDot.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import Select from '$lib/ui/Select.svelte';
  import Sheet from '$lib/ui/Sheet.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';
  import VerdictSheet from '$lib/ui/VerdictSheet.svelte';

  // Окна чтения названы размером: знаменатель пробелов и счётчиков это
  // загруженное окно, а не весь корпус, и экран обязан это различать.
  const FINDINGS_WINDOW = 500;
  const DISPUTED_WINDOW = 200;
  const QUEUE_WINDOW = 50;
  const POINTS_COLLAPSED = 3;

  type Facet = 'topics' | 'disputed' | 'gaps';

  const FACET_IDS: Facet[] = ['topics', 'disputed', 'gaps'];

  const FACETS: { id: Facet; label: string; lead: string }[] = [
    {
      id: 'topics',
      label: 'Сходимость чисел',
      lead:
        'Один и тот же показатель в нескольких документах: полоса значений, размах и сверка с пределом из источника.',
    },
    { id: 'disputed', label: 'Где источники спорят', lead: DIVERGENCE_VIEW.lead },
    { id: 'gaps', label: 'Где источники молчат', lead: GAPS_VIEW.lead },
  ];

  /** Подпись сверки с пределом: «нечем сверить» не выдаётся за «в пределе». */
  const LIMIT_LABEL: Record<LimitCheck, string> = {
    outside: COMPARE_CELL_LABELS.outside,
    within: COMPARE_CELL_LABELS.within,
    none: COMPARE_CELL_LABELS.unchecked,
  };

  const GAP_KINDS: GapKind[] = ['pair', 'unit', 'value', 'locator'];

  function initialFacet(): Facet {
    const incoming = page.url.searchParams.get('facet')?.trim();
    return FACET_IDS.find((id) => id === incoming) ?? 'topics';
  }

  let facet = $state<Facet>(initialFacet());
  let search = $state(page.url.searchParams.get('q')?.trim() ?? '');
  let sort = $state<string>('divergence');
  let onlyDivergent = $state(false);
  let gapKind = $state<GapKind | null>(null);

  let findings = $state<FindingListItem[]>([]);
  let disputed = $state<FindingListItem[]>([]);
  let candidates = $state<ConflictCandidate[]>([]);
  let findingsTotal = $state<number | null>(null);
  let disputedTotal = $state<number | null>(null);
  let queueTotal = $state<number | null>(null);
  let windowNote = $state('');
  let loadedAt = $state('');
  let queueLoadedAt = $state('');
  let loading = $state(false);
  let failed = $state(false);
  let queueLoading = $state(false);
  let queueFailed = $state(false);
  let decisionBusy = $state('');
  let decisionNote = $state('');
  let booted = false;

  let openGroups = $state<Record<string, boolean>>({});
  let wideGroups = $state<Record<string, boolean>>({});
  let mainChoice = $state<Record<string, string>>({});

  let trace = $state<{ finding: FindingListItem; point: ValuePoint } | null>(null);
  let verdict = $state<{ subject: string; note: string; findingId: string | null } | null>(null);

  let seg = $state<HTMLDivElement | undefined>();

  const canRead = $derived(session.can('knowledge:read'));
  const canQueue = $derived(session.can('proposal:review'));
  const canJudge = $derived(session.can('feedback:give'));
  const activeLead = $derived(FACETS.find((item) => item.id === facet)?.lead ?? '');

  const byId = $derived(
    new Map<string, FindingListItem>([...findings, ...disputed].map((finding) => [finding.id, finding])),
  );
  const limitByProperty = $derived(
    new Map<string, PropertyLimit>(
      limitsOfFindings([...findings, ...disputed]).map((limit) => [limit.property, limit]),
    ),
  );

  const groupsFromCorpus = $derived(topicGroups(findings));
  const groupsFromDisputed = $derived(topicGroups(disputed));

  const entityFilter = $derived(
    (page.url.searchParams.get('entities') ?? '')
      .split(',')
      .map((value) => value.trim().toLowerCase())
      .filter((value) => value.length > 0),
  );

  function haystack(group: TopicGroup): string {
    return [
      group.topic.title,
      ...group.findings.map((finding) => finding.statement),
      ...group.findings.map((finding) => sourceTitleOf(finding)),
      ...group.findings.flatMap((finding) => finding.observations.map((row) => row.raw_text)),
      ...group.comparisons.flatMap((comparison) => comparison.points.map((point) => point.text)),
    ]
      .join(' ')
      .toLowerCase();
  }

  function keep(group: TopicGroup): boolean {
    if (onlyDivergent && !anyDivergence(group.comparisons)) return false;
    const needle = search.trim().toLowerCase();
    if (needle.length > 0) {
      const text = haystack(group);
      return needle.split(/\s+/).every((word) => text.includes(word));
    }
    if (entityFilter.length === 0) return true;
    return group.findings.some((finding) =>
      entityFilter.some((token) => (finding.subject ?? '').toLowerCase().includes(token)),
    );
  }

  const sourceGroups = $derived(facet === 'disputed' ? groupsFromDisputed : groupsFromCorpus);
  const keptTopics = $derived(sourceGroups.filter(keep));

  const topicsView = $derived.by(() => {
    const rows = [...keptTopics];
    if (sort === 'sources') rows.sort((a, b) => b.sourceCount - a.sourceCount);
    else if (sort === 'subject') rows.sort((a, b) => a.topic.title.localeCompare(b.topic.title, 'ru'));
    else rows.sort((a, b) => (b.divergence?.ratio ?? -1) - (a.divergence?.ratio ?? -1));
    return rows;
  });

  const gapNotes = $derived(gapNotesOf(disputed));
  const gapView = $derived(gapKind === null ? gapNotes : gapNotes.filter((note) => note.kind === gapKind));
  const queueWaitingCount = $derived(
    candidates.filter((candidate) => candidate.status === 'candidate').length,
  );

  async function loadCorpus(): Promise<void> {
    loading = true;
    failed = false;
    try {
      const corpus = await api.findings(undefined, undefined, FINDINGS_WINDOW, 0);
      const contested = await api.conflicts(DISPUTED_WINDOW, 0);
      findings = corpus.items;
      findingsTotal = corpus.total;
      disputed = contested.items;
      disputedTotal = contested.total;
      windowNote = contested.windowNote === '' ? corpus.windowNote : contested.windowNote;
      loadedAt = new Date().toISOString();
      openClaimVerdict();
    } catch {
      failed = true;
    } finally {
      loading = false;
    }
    if (canQueue) void loadQueue();
  }

  /** Глубокая ссылка с утверждением открывает вердикт на месте, без перехода. */
  function openClaimVerdict(): void {
    const claim = page.url.searchParams.get('claim')?.trim() ?? '';
    if (claim.length === 0) return;
    const finding = byId.get(claim);
    if (!finding) return;
    verdict = {
      subject: finding.statement.slice(0, 180),
      note: sourceTitleOf(finding) === '' ? TOPIC_WORDS.noSource : sourceTitleOf(finding),
      findingId: finding.id,
    };
  }

  async function loadQueue(): Promise<void> {
    queueLoading = true;
    queueFailed = false;
    try {
      const rows = await api.conflictCandidates(QUEUE_WINDOW, 0);
      candidates = rows.items;
      queueTotal = rows.total;
      queueLoadedAt = new Date().toISOString();
    } catch {
      queueFailed = true;
    } finally {
      queueLoading = false;
    }
  }

  async function decide(candidate: ConflictCandidate, confirmed: boolean): Promise<void> {
    decisionBusy = candidate.id;
    decisionNote = '';
    try {
      await api.conflictReview(candidate.id, confirmed);
      const status: 'confirmed' | 'dismissed' = confirmed ? 'confirmed' : 'dismissed';
      candidates = candidates.map((row) => (row.id === candidate.id ? { ...row, status } : row));
      decisionNote = queueOutcome(status);
    } catch {
      decisionNote = QUEUE_FAILURE.transport;
    } finally {
      decisionBusy = '';
    }
  }

  function placeIndicator(): void {
    if (!browser || !seg) return;
    const active = seg.querySelector<HTMLElement>('[aria-current="true"]');
    if (!active) {
      seg.style.setProperty('--ind-o', '0');
      return;
    }
    const box = active.getBoundingClientRect();
    const host = seg.getBoundingClientRect();
    seg.style.setProperty('--ind-x', `${box.left - host.left + seg.scrollLeft}px`);
    seg.style.setProperty('--ind-y', `${box.top - host.top}px`);
    seg.style.setProperty('--ind-w', `${box.width}px`);
    seg.style.setProperty('--ind-h', `${box.height}px`);
    seg.style.setProperty('--ind-o', '1');
  }

  function chooseFacet(next: Facet): void {
    facet = next;
    void tick().then(placeIndicator);
  }

  function toggleGroup(id: string): void {
    openGroups[id] = openGroups[id] !== true;
  }

  /** Раскрытие остальных значений полосы: список тем не меняется, точек больше. */
  function toggleWide(id: string): void {
    wideGroups[id] = wideGroups[id] !== true;
  }

  function expandAll(): void {
    const next: Record<string, boolean> = {};
    for (const group of topicsView) next[group.id] = true;
    openGroups = next;
  }

  function collapseAll(): void {
    openGroups = {};
    wideGroups = {};
  }

  function resetFilters(): void {
    search = '';
    onlyDivergent = false;
    gapKind = null;
  }

  function headlinePhrase(group: TopicGroup): string {
    const delta = group.divergence;
    if (delta === null) return TOPIC_NO_PERCENT;
    return topicDifference(delta, group.headline?.unit ?? '');
  }

  function mainPointId(group: TopicGroup): string | null {
    const chosen = mainChoice[group.id];
    if (chosen) return chosen;
    return widestPair(group).base?.id ?? null;
  }

  function baseValue(group: TopicGroup, comparison: Comparison): number | null {
    const id = mainPointId(group);
    const point = comparison.points.find((row) => row.findingId === id);
    return point ? point.lo : null;
  }

  function visiblePoints(group: TopicGroup, comparison: Comparison): ValuePoint[] {
    return wideGroups[group.id] === true ? comparison.points : comparison.points.slice(0, POINTS_COLLAPSED);
  }

  function observationOf(point: ValuePoint): NumericObservation | undefined {
    return byId
      .get(point.findingId)
      ?.observations.find((row) => row.property_name === point.property);
  }

  function checkFor(point: ValuePoint): {
    limit: PropertyLimit | undefined;
    result: LimitCheck;
    offending: number | null;
  } {
    const limit = limitByProperty.get(point.property);
    const observation = observationOf(point);
    if (!observation) return { limit, result: 'none', offending: null };
    const checked = checkAgainstLimit(
      observationText(observation),
      observation.normalized_unit || observation.unit,
      limit,
    );
    return { limit, result: checked.check, offending: checked.offending };
  }

  function openVerdict(group: TopicGroup): void {
    const finding = group.findings[0];
    verdict = {
      subject: group.topic.title,
      note: topicCounts(group.findings.length, group.sourceCount),
      findingId: finding ? finding.id : null,
    };
  }

  function openTrace(point: ValuePoint): void {
    const finding = byId.get(point.findingId);
    if (finding) trace = { finding, point };
  }

  function resetMain(group: TopicGroup): void {
    delete mainChoice[group.id];
  }

  function sideValue(side: ConflictCandidate['left']): string {
    return side.value.trim().length > 0 ? side.value : QUEUE_WORDS.noValue;
  }

  $effect(() => {
    const state = session.state;
    if (!browser || state !== 'authenticated' || !canRead || booted) return;
    booted = true;
    void loadCorpus();
  });

  $effect(() => {
    const current = facet;
    if (!browser) return;
    void tick().then(() => {
      if (current === facet) placeIndicator();
    });
  });
</script>

<svelte:head>
  <title>{navLabel('/numbers')}: Научный Клубок</title>
</svelte:head>

<div class="page numbers">
  <!-- На поверхности экрана остаётся одна фраза фасета. Методика размаха,
       несопоставимости и порядка отбора переехала к тому, что объясняет: в
       строку размаха, в признак несопоставимости и к порядку отбора. -->
  <SectionHead level="1" title={navLabel('/numbers')} lead={activeLead} />

  <div class="seg numbers__seg" bind:this={seg} role="group" aria-label="Что показывать в разделе">
    <span class="seg__ind" aria-hidden="true"></span>
    {#each FACETS as item (item.id)}
      <button
        type="button"
        class="seg__item"
        aria-current={facet === item.id ? 'true' : undefined}
        onclick={() => chooseFacet(item.id)}
      >
        {item.label}
      </button>
    {/each}
  </div>

  {#if loading}
    <p class="small muted" role="status">{DIVERGENCE_VIEW.loading}</p>
  {:else if failed}
    <Notice tone="error" title={DIVERGENCE_VIEW.failedTitle}>
      {retryNote('числа корпуса')}
      <Button variant="quiet" size="sm" onclick={() => void loadCorpus()}>
        {DIVERGENCE_VIEW.readAgain}
      </Button>
    </Notice>
  {:else if !canRead}
    <Empty title={DIVERGENCE_VIEW.deniedTitle} body={deniedNote('числа корпуса')} />
  {:else}
    <div class="numbers__controls">
      <div class="numbers__search">
        <Field
          name="numbers-q"
          label={DIVERGENCE_VIEW.filterSearch}
          type="search"
          bind:value={search}
          placeholder={DIVERGENCE_VIEW.filterSearchPlaceholder}
        />
      </div>
      {#if facet !== 'gaps'}
        <div class="numbers__order">
          <Select
            name="numbers-sort"
            label={DIVERGENCE_VIEW.filterSort}
            bind:value={sort}
            options={DIVERGENCE_SORT.map((item) => ({ value: item.value, label: item.label }))}
          />
          <!-- Порядок отбора опирается на предел из источника, поэтому
               пояснение едет у порядка, а не в углу заголовка. -->
          <InfoDot title={DIVERGENCE_HELP.limitTitle} align="end">
            <p>{COMPARE_LIMIT_SOURCE.operator}</p>
            <p>{COMPARE_LIMIT_SOURCE.wording}</p>
          </InfoDot>
        </div>
      {/if}
      <div class="numbers__filters" role="group" aria-label="Фильтры списка">
        {#if facet === 'gaps'}
          {#each GAP_KINDS as kind (kind)}
            <Button
              size="sm"
              variant={gapKind === kind ? 'ink' : 'quiet'}
              current={gapKind === kind}
              onclick={() => (gapKind = gapKind === kind ? null : kind)}
            >
              {GAP_KIND_LABELS[kind]}
            </Button>
          {/each}
        {:else}
          <Button
            size="sm"
            variant={onlyDivergent ? 'ink' : 'quiet'}
            current={onlyDivergent}
            onclick={() => (onlyDivergent = !onlyDivergent)}
          >
            {DIVERGENCE_VIEW.filterDivergent}
          </Button>
        {/if}
        <Button variant="link" size="sm" onclick={resetFilters}>{DIVERGENCE_VIEW.filterReset}</Button>
      </div>
    </div>

    {#if windowNote !== ''}
      <Notice tone="warn">{windowNote}</Notice>
    {/if}

    {#if facet === 'topics' || facet === 'disputed'}
      <p class="micro muted numbers__count">
        <span>{topicsShown(topicsView.length, sourceGroups.length)}</span>
        {#if facet === 'disputed'}
          <span>{divergencesShownOf(disputed.length, disputedTotal)}</span>
        {/if}
        {#if loadedAt !== ''}
          <span>{DIVERGENCE_VIEW.loadedAt}: {dateTime(loadedAt)}</span>
        {/if}
        <!-- Один «i» на весь список: он объясняет и размах полосы, и то, от
             чего считается процент. Построчных кружков нет, потому что их
             количество росло вместе с числом тем. -->
        <InfoDot title={DIVERGENCE_HELP.spreadTitle} body={DIVERGENCE_HELP.spread} align="end" />
      </p>

      {#if topicsView.length === 0}
        {#if findings.length === 0 && disputed.length === 0}
          <Empty title={DIVERGENCE_VIEW.emptyCorpusTitle} body={DIVERGENCE_VIEW.emptyCorpusBody} />
        {:else if (facet === 'disputed' ? disputedTotal : findingsTotal) === null}
          <Empty title={DIVERGENCE_VIEW.unknownTotalTitle} body={DIVERGENCE_VIEW.unknownTotalBody} />
        {:else if facet === 'disputed' && disputed.length === 0}
          <Empty title={DIVERGENCE_VIEW.noDivergenceTitle} body={noDivergenceBody(disputed.length)} />
        {:else}
          <Empty title={DIVERGENCE_VIEW.filterEmptyTitle} body={DIVERGENCE_VIEW.filterEmptyBody} />
        {/if}
      {:else}
        <div class="numbers__bulk">
          <Button variant="ghost" size="sm" onclick={expandAll}>{DIVERGENCE_ACTION.expandAll}</Button>
          <Button variant="ghost" size="sm" onclick={collapseAll}>{DIVERGENCE_ACTION.collapseAll}</Button>
        </div>

        {#each topicsView as group (group.id)}
          {@const first = group.findings[0]}
          <article class="topic">
            <h3 class="h4 topic__title">
              <button
                type="button"
                class="topic__toggle"
                aria-expanded={openGroups[group.id] === true}
                aria-controls={`body-${group.id}`}
                onclick={() => toggleGroup(group.id)}
              >
                <span class="grow">{group.topic.title}</span>
                <span class="topic__figure">{headlinePhrase(group)}</span>
              </button>
            </h3>
            <p class="micro muted topic__meta">
              <span>{topicCounts(group.findings.length, group.sourceCount)}</span>
              {#if group.comparisons.length === 0}
                <span class="topic__flag">{TOPIC_NOT_COMPARABLE}</span>
              {/if}
              {#if !group.topic.named}
                <span class="topic__flag">{propertyName(first?.subject ?? '').name}</span>
              {/if}
            </p>

            {#if openGroups[group.id] === true}
              <div class="topic__body" id={`body-${group.id}`}>
                {#if !first?.scope || Object.keys(first.scope).length === 0}
                  <p class="micro muted">{TOPIC_WORDS.noScope}</p>
                {/if}

                {#if group.comparisons.length === 0}
                  <p class="small muted">{TOPIC_WORDS.noNumbers}</p>
                  <p class="small muted">{SCALE_WORDS.noScale}</p>
                {/if}

                {#each group.comparisons as comparison (comparison.key)}
                  {@const limit = limitByProperty.get(comparison.property)}
                  {@const displayName = propertyName(comparison.property).name}
                  {@const relative = comparison.relative}
                  {@const overlap = comparison.overlap}
                  <div class="scale">
                    <p class="scale__name">
                      {displayName}
                      <span class="micro muted">
                        {comparison.unit === '' ? TOPIC_WORDS.noUnit : `(${comparison.unit})`}
                      </span>
                    </p>

                    {#if limit}
                      <p class="micro scale__limit">
                        {limitTagText(displayName, limit)}
                        <InfoDot title={DIVERGENCE_ACTION.mainSource} body={COMPARE_LIMIT_SOURCE[limit.from]} />
                      </p>
                    {/if}

                    <p class="micro scale__spread">
                      <span>{SCALE_WORDS.range}: {num(comparison.lo)} {num(comparison.hi)}</span>
                      {#if relative}
                        <span>{topicDifference(relative, comparison.unit)}</span>
                      {:else if comparison.spread === 0}
                        <span>{SCALE_WORDS.equal}</span>
                      {:else}
                        <span>{TOPIC_NO_PERCENT}</span>
                      {/if}
                    </p>

                    {#if !diverges(comparison)}
                      <p class="micro muted">{SCALE_WORDS.noband}</p>
                    {:else if comparison.disjoint}
                      <p class="micro">{SCALE_WORDS.noOverlap}</p>
                    {/if}

                    {#if overlap}
                      <p class="micro">
                        {SCALE_WORDS.overlap}: {num(overlap[0])} {num(overlap[1])}
                      </p>
                    {/if}

                    <ul class="points">
                      {#each visiblePoints(group, comparison) as point (point.findingId)}
                        {@const band = bandOf(point, comparison)}
                        {@const check = checkFor(point)}
                        {@const delta = deltaPhrase(deltaFromBase(baseValue(group, comparison), point.lo), comparison.unit)}
                        <li class="point">
                          <span class="point__src">
                            {point.source === '' ? TOPIC_WORDS.noSource : point.source}
                          </span>
                          <span class="point__value">{point.text}</span>
                          {#if band}
                            <span class="band" aria-hidden="true">
                              <span
                                class="band__fill"
                                style="inset-inline-start: {band.left}%; inline-size: {band.width}%;"
                              ></span>
                            </span>
                          {/if}
                          <span class="point__delta">
                            {#if delta.value}
                              {delta.lead} {delta.value} ({delta.basis})
                            {:else}
                              {delta.lead}
                            {/if}
                          </span>
                          <StatusPill
                            status={check.result === 'outside' ? 'disputed' : check.result === 'within' ? 'consensus' : 'off'}
                            label={LIMIT_LABEL[check.result]}
                          />
                          {#if check.result === 'outside' && check.offending !== null && check.limit}
                            <p class="micro point__violation">
                              {outsideLimitText(
                                displayName,
                                check.offending,
                                check.limit.unit,
                                check.limit.kind,
                                check.limit.at,
                              )}
                            </p>
                          {/if}
                          <span class="point__actions">
                            <Button variant="link" size="sm" onclick={() => openTrace(point)}>
                              {TOPIC_WORDS.quoteAndPlace}
                            </Button>
                            {#if mainPointId(group) !== point.findingId}
                              <Button
                                variant="link"
                                size="sm"
                                onclick={() => (mainChoice[group.id] = point.findingId)}
                              >
                                {DIVERGENCE_ACTION.mainSource}
                              </Button>
                            {:else}
                              <Button variant="link" size="sm" onclick={() => resetMain(group)}>
                                {DIVERGENCE_ACTION.resetMain}
                              </Button>
                            {/if}
                          </span>
                        </li>
                      {/each}
                    </ul>

                    {#if comparison.points.length > POINTS_COLLAPSED}
                      <p class="micro muted">
                        <span>{othersShown(visiblePoints(group, comparison).length, comparison.points.length)}</span>
                        <Button variant="link" size="sm" onclick={() => toggleWide(group.id)}>
                          {wideGroups[group.id] === true ? TOPIC_WORDS.collapseOthers : TOPIC_WORDS.showMore}
                        </Button>
                      </p>
                    {/if}
                  </div>
                {/each}

                <div class="topic__foot">
                  <Button variant="ink" size="sm" onclick={() => openVerdict(group)}>
                    {DIVERGENCE_ACTION.record}
                  </Button>
                  {#if facet === 'disputed' && group.findings.length < 2}
                    <span class="micro muted">{DIVERGENCE_VIEW.noSecondBody}</span>
                  {/if}
                </div>
              </div>
            {/if}
          </article>
        {/each}
      {/if}

      {#if facet === 'disputed' && canQueue}
        <section class="queue">
          <p class="eyebrow">{QUEUE_HEAD.eyebrow}</p>
          <h3 class="h3">{QUEUE_HEAD.title}</h3>
          <p class="small muted">{QUEUE_HEAD.lead}</p>

          {#if queueLoading}
            <p class="small muted" role="status" aria-busy="true">{QUEUE_WORDS.skeleton}</p>
          {:else if queueFailed}
            <Notice tone="error" title={QUEUE_WORDS.queueFailedTitle}>
              {QUEUE_FAILURE.queueFailed}
              <Button variant="quiet" size="sm" onclick={() => void loadQueue()}>
                {QUEUE_ACTION.readAgain}
              </Button>
            </Notice>
          {:else if candidates.length === 0}
            <Empty title={QUEUE_WORDS.emptyTitle} body={QUEUE_WORDS.emptyBody} />
          {:else}
            <p class="micro muted queue__count">
              <span>{queueCounter(candidates.length, queueTotal)}</span>
              <span>{queueWaiting(queueWaitingCount)}</span>
              {#if queueLoadedAt !== ''}
                <span>{QUEUE_WORDS.loadedAt}: {dateTime(queueLoadedAt)}</span>
              {/if}
              <InfoDot title={QUEUE_HINTS.confirmTitle} body={QUEUE_HINTS.confirm} />
            </p>

            {#if decisionNote !== ''}
              <Notice tone="ok">{decisionNote}</Notice>
            {/if}

            {#each candidates as candidate (candidate.id)}
              <article class="pair">
                <p class="pair__topic">
                  <span>{candidate.subject === '' ? QUEUE_WORDS.noTopic : candidate.subject}</span>
                  <span class="micro muted">{propertyName(candidate.property_name).name}</span>
                  <StatusPill
                    status={QUEUE_STATUS_TONE[candidate.status]}
                    label={QUEUE_STATUS_LABELS[candidate.status]}
                  />
                </p>
                <p class="small">
                  {candidate.reason.trim() === '' ? QUEUE_WORDS.noReason : candidate.reason}
                </p>
                <ul class="pair__sides">
                  <li>
                    <span class="micro muted">{CONFLICT_SIDE.first}</span>
                    <span class="point__value">{sideValue(candidate.left)}</span>
                    <span class="small">
                      {candidate.left.statement.trim() === '' ? QUEUE_WORDS.noStatement : candidate.left.statement}
                    </span>
                  </li>
                  <li>
                    <span class="micro muted">{CONFLICT_SIDE.second}</span>
                    <span class="point__value">{sideValue(candidate.right)}</span>
                    <span class="small">
                      {candidate.right.statement.trim() === '' ? QUEUE_WORDS.noStatement : candidate.right.statement}
                    </span>
                  </li>
                </ul>
                <div class="pair__actions">
                  {#if candidate.status === 'candidate'}
                    <span class="micro muted">{QUEUE_WORDS.waiting}</span>
                  {:else if candidate.decided_by === session.account?.id}
                    <span class="micro muted">{QUEUE_WORDS.decidedSelf}</span>
                  {:else if candidate.decided_by}
                    <span class="micro muted">{QUEUE_WORDS.decidedBy}</span>
                  {/if}
                  <Button
                    variant="action"
                    size="sm"
                    busy={decisionBusy === candidate.id}
                    disabled={decisionBusy === candidate.id}
                    onclick={() => void decide(candidate, true)}
                  >
                    {decisionBusy === candidate.id ? QUEUE_ACTION.pending : QUEUE_ACTION.confirm}
                  </Button>
                  <Button
                    variant="quiet"
                    size="sm"
                    disabled={decisionBusy === candidate.id}
                    onclick={() => void decide(candidate, false)}
                  >
                    {QUEUE_ACTION.dismiss}
                  </Button>
                </div>
              </article>
            {/each}
          {/if}
        </section>
      {/if}
    {:else}
      <p class="micro muted numbers__count">
        <span>{gapsCounter(gapView.length, gapNotes.length, DISPUTED_WINDOW, disputedTotal)}</span>
        <InfoDot title={DIVERGENCE_HELP.gapsTitle} body={DIVERGENCE_HELP.gaps} />
      </p>

      {#if gapView.length === 0}
        <Empty title={GAPS_VIEW.emptyTitle} body={GAPS_VIEW.emptyBody} />
      {:else}
        <ul class="gaps">
          {#each gapView as note (note.id)}
            <li class="gap">
              <p class="gap__title">
                <span>{GAP_KIND_TITLES[note.kind]}</span>
                <span class="micro muted">{GAP_KIND_LABELS[note.kind]}</span>
              </p>
              <p class="small">
                {gapBody(
                  note.kind,
                  note.topic.title,
                  countOf(note.findings, 'утверждение', 'утверждения', 'утверждений'),
                  note.units,
                )}
              </p>
              <p class="micro muted">
                <span>{note.source === '' ? TOPIC_WORDS.noSource : note.source}</span>
                {#if note.locators.length === 0}
                  <span>{TOPIC_WORDS.noLocatorPlace}</span>
                {:else}
                  {#each note.locators as locator (locator.kind)}
                    <span>{locator.kind} {locator.value}</span>
                  {/each}
                {/if}
              </p>
            </li>
          {/each}
        </ul>
      {/if}
    {/if}
  {/if}
</div>

{#if trace}
  <Sheet
    title={TOPIC_WORDS.quoteAndPlace}
    description={trace.point.source === '' ? TOPIC_WORDS.noSource : trace.point.source}
    onclose={() => (trace = null)}
  >
    {#if trace.finding.evidence.length === 0}
      <p class="small">{TOPIC_WORDS.noEvidence}</p>
    {:else}
      <p class="prose">{trace.finding.statement}</p>
      <ul class="locs">
        {#each trace.point.locators as locator (locator.kind)}
          <li>{locator.kind} {locator.value}</li>
        {:else}
          <li>{TOPIC_WORDS.noLocators}</li>
        {/each}
      </ul>
    {/if}
    {#snippet footer()}
      <Button variant="quiet" size="sm" href="/findings">{sectionLink(navLabel('/findings'))}</Button>
    {/snippet}
  </Sheet>
{/if}

{#if verdict}
  <VerdictSheet
    subject={verdict.subject}
    subjectNote={verdict.note}
    findingId={verdict.findingId}
    candidates={facet === 'disputed' ? disputed : findings}
    gate={canJudge ? '' : deniedNote('вердикт по теме')}
    onclose={() => (verdict = null)}
    onsent={() => (verdict = null)}
  />
{/if}

<style>
  .numbers {
    display: grid;
    gap: var(--s5);
    /* Гуттер экрана свой: без него правая колонка строки темы заканчивалась
       ровно на краю окна, и последнее слово причины («...знаменателя нет»)
       читалось обрезанным. */
    padding-inline: var(--pad-page);
  }

  /* Grid-строка не вправе растягивать документ: любой блок раздела ужается до
     ширины вьюпорта, а то, что сжать нельзя (полоса фасетов) крутится внутри
     своей области. */
  .numbers > * {
    min-width: 0;
  }

  /* Раскрытие привязано к тому, что объясняет: методика поиска стоит у поля
     поиска, порядок отбора — у своего селекта. Под заголовком экрана их кучи
     больше нет. */
  .numbers__search,
  .numbers__order {
    display: flex;
    align-items: flex-end;
    gap: var(--s2);
    min-width: 0;
  }

  .numbers__search > :global(.field),
  .numbers__order > :global(.field) {
    flex: 1 1 auto;
    min-width: 0;
  }

  /* Живые действия раздела (отбор, раскрытие, сверка с пределом) были строкой
     21 px: цель нажатия поднимается геометрией контрола, кегль остаётся micro.
     На пальце и ниже 640px цель держит общее правило тач-целей из app.css. */
  .numbers :global(.btn) {
    min-block-size: 32px;
  }

  /* Полоса фасетов на узком экране переносится целиком: третий фасет обязан
     быть прочитан, а не обрезан краем. Если переносить нечего и полоса всё же
     шире своей области — она прокручивается с видимой полосой прокрутки. */
  .numbers__seg {
    overflow-x: auto;
    flex-wrap: wrap;
    scrollbar-width: thin;
    scrollbar-color: var(--ink-4) var(--surface-sunk);
  }

  /* app.css красит выбранный пункт только по aria-current="page", а фасеты
     помечены aria-current="true". Выбранное состояние держит пилюля самого
     пункта: общий индикатор полосы после переноса строки замирает на прежней
     координате и выбранное состояние читается сдвигом. */
  .numbers__seg .seg__ind {
    display: none;
  }

  .numbers__seg .seg__item[aria-current='true'] {
    background: var(--surface);
    box-shadow: var(--shadow-soft);
    color: var(--ink);
    font-weight: 600;
  }

  .numbers__controls {
    display: grid;
    gap: var(--s4);
    grid-template-columns: minmax(0, 1fr) minmax(0, auto);
    align-items: end;
  }

  /* Фраза размаха занимает свою колонку и переносится: она не имеет права
     упираться концом в край окна, потому что в ней причина отсутствия
     процента. */
  .topic__toggle > .topic__figure {
    flex: 1 1 22ch;
    overflow-wrap: break-word;
    hyphens: auto;
  }

  .numbers__filters {
    grid-column: 1 / -1;
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
    align-items: center;
  }

  .numbers__count,
  .numbers__bulk {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s3);
    align-items: center;
    margin: 0;
  }

  .topic {
    border-block-start: 1px solid var(--line);
    padding-block: var(--s4);
  }

  .topic__title {
    margin: 0;
  }

  .topic__toggle {
    display: flex;
    gap: var(--s3);
    align-items: baseline;
    flex-wrap: wrap;
    inline-size: 100%;
    background: none;
    border: 0;
    /* Строка темы — единственный способ раскрыть показатель, а попадала она по
       21 px текста. Отступ даёт цель 32+ px, отрицательная компенсация оставляет
       плотность списка прежней: визуально строка не сдвигается. */
    min-block-size: 32px;
    padding-block: var(--s2);
    margin-block: calc(var(--s2) * -1);
    text-align: start;
    cursor: pointer;
    color: inherit;
    font: inherit;
  }

  .topic__toggle:focus-visible {
    outline: 2px solid var(--edge-active);
    outline-offset: 3px;
    border-radius: var(--r-xs);
  }

  /* Фраза размаха обязана ужаться вместе с заголовком темы: nowrap на длинной
     формулировке («Различий нет: значения…») задавал минимальную ширину всей
     grid-строки страницы, и на телефоне документ уходил за вьюпорт. */
  .topic__figure {
    min-width: 0;
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
  }

  .topic__meta {
    margin: var(--s1) 0 0;
    display: flex;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .topic__flag {
    color: var(--action-ink);
  }

  .topic__body {
    display: grid;
    gap: var(--s4);
    margin-block-start: var(--s3);
    padding-inline-start: var(--s3);
    border-inline-start: 2px solid var(--line-soft);
  }

  .scale {
    display: grid;
    gap: var(--s2);
  }

  .scale__name {
    margin: 0;
    font-weight: 600;
  }

  .scale__limit,
  .scale__spread {
    margin: 0;
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
    display: flex;
    gap: var(--s3);
    flex-wrap: wrap;
    align-items: center;
  }

  .points {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--s2);
  }

  .point {
    display: grid;
    gap: var(--s2) var(--s3);
    grid-template-columns: minmax(160px, 1fr) auto minmax(90px, 2fr) auto auto;
    align-items: center;
  }

  .point__src {
    color: var(--ink-2);
  }

  .point__value {
    font-family: var(--font-data);
    font-variant-numeric: tabular-nums;
  }

  .point__delta {
    color: var(--ink-3);
    font-size: var(--t-micro);
  }

  .point__violation {
    margin: 0;
    color: var(--disputed);
    grid-column: 1 / -1;
  }

  .point__actions {
    display: flex;
    gap: var(--s2);
    align-items: center;
    flex-wrap: wrap;
  }

  .band {
    position: relative;
    block-size: 8px;
    border-radius: var(--r-pill);
    background: var(--surface-sunk);
    min-inline-size: 90px;
  }

  .band__fill {
    position: absolute;
    inset-block: 0;
    border-radius: var(--r-pill);
    background: var(--action);
  }

  .topic__foot {
    display: flex;
    gap: var(--s3);
    align-items: center;
    flex-wrap: wrap;
  }

  .queue {
    border-block-start: 1px solid var(--line);
    padding-block-start: var(--s5);
    display: grid;
    gap: var(--s2);
  }

  .queue__count {
    display: flex;
    gap: var(--s3);
    align-items: center;
    flex-wrap: wrap;
    margin: 0;
  }

  .pair {
    border: 1px solid var(--line-soft);
    border-radius: var(--r-xs);
    padding: var(--s4);
    display: grid;
    gap: var(--s2);
    background: var(--surface);
  }

  .pair__topic {
    margin: 0;
    display: flex;
    gap: var(--s3);
    align-items: center;
    flex-wrap: wrap;
    font-weight: 600;
  }

  .pair__sides {
    margin: 0;
    padding-inline-start: var(--s4);
    display: grid;
    gap: var(--s2);
  }

  .pair__sides li {
    display: flex;
    gap: var(--s3);
    align-items: baseline;
    flex-wrap: wrap;
  }

  .pair__actions {
    display: flex;
    gap: var(--s3);
    align-items: center;
    flex-wrap: wrap;
  }

  .gaps {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--s3);
  }

  .gap {
    border-inline-start: 2px solid var(--sage-deep);
    padding-inline-start: var(--s3);
    display: grid;
    gap: var(--s1);
  }

  .gap__title {
    margin: 0;
    display: flex;
    gap: var(--s3);
    align-items: baseline;
    flex-wrap: wrap;
  }

  .locs {
    margin: 0;
    padding-inline-start: var(--s4);
    display: grid;
    gap: var(--s1);
  }

  @media (prefers-reduced-motion: reduce) {
    .topic__toggle,
    .band__fill {
      transition: none;
    }
  }

  @media (max-width: 720px) {
    .numbers__controls {
      grid-template-columns: minmax(0, 1fr);
    }

    .point {
      grid-template-columns: minmax(0, 1fr) auto;
    }

    .point__value,
    .topic__title {
      overflow-wrap: break-word;
    }
  }
</style>
