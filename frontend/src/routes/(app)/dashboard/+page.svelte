<script lang="ts">
  import { api } from '$lib/api';
  import { dateTime, num, pct } from '$lib/format';
  import { navLabel } from '$lib/nav';
  import { session } from '$lib/sessionStore.svelte';
  import {
    CHECK_NOUNS,
    CHECK_TABLE_LABELS,
    COVERAGE_LABELS,
    EVAL_METRIC_LABELS,
    FINDING_NOUNS,
    JOURNAL_WINDOW_WORDS,
    MODEL_MODE_LABELS,
    PROCESS_METRIC_LABELS,
    journalIncomplete,
    journalLoadMoreOf,
    journalUnloaded,
    shownSentenceOf,
    termOf,
  } from '$lib/terms';
  import type {
    CorpusStats,
    DashboardData,
    EvaluationRun,
    FindingListItem,
    SystemStatus,
  } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';

  // Экран отвечает на три вопроса: можно ли спросить прямо сейчас, насколько
  // велик корпус и опираются ли его ответы на источники. Всё, что объясняет,
  // как нарисована полоса, сжато в единственное раскрытие «Как это устроено»;
  // технические числа процесса и идентификаторы убраны под «Служебные данные».
  // Детерминированные метрики идут одним блоком колонок, самооценка модели
  // подписана отдельной группой: её читают как слова модели, а не как проверку.
  const GROUNDED_KEYS: (keyof EvaluationRun['metrics'])[] = [
    'citation_coverage',
    'numeric_support',
    'unsupported_claim_ratio',
    'overall',
  ];
  const SELF_REPORTED_KEYS: (keyof EvaluationRun['metrics'])[] = ['mean_finding_confidence'];

  const SKELETONS = [0, 1, 2, 3, 4, 5];

  // Журнал проверок читается страницей: сервер режет её по `limit`/`offset` и
  // кладёт полное число подходящих записей в заголовок ответа. Экран называет
  // обе величины, а не выдаёт страницу за весь журнал.
  const RUNS_FEED_SIZE = 200;

  // Покрытия считается по показанным находкам. Окно запроса названо явно,
  // потому что полное число находок приходит отдельно: без него знаменатель
  // был бы догадкой.
  const COVERAGE_WINDOW = 200;

  let methodOpen = $state(false);
  let serviceOpen = $state(false);

  // Мобильная композиция шапки: метка раздела уходит, действие встаёт рядом с
  // заголовком — числа начинаются выше.
  let narrow = $state(false);
  $effect(() => {
    const media = window.matchMedia('(max-width: 640px)');
    narrow = media.matches;
    const onChange = (event: MediaQueryListEvent): void => {
      narrow = event.matches;
    };
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  });

  let dash = $state<DashboardData | null>(null);
  let stats = $state<CorpusStats | null>(null);
  let findings = $state<FindingListItem[] | null>(null);
  // Полное число подходящих находок: по нему видно, что покрытие посчитано не
  // по всему корпусу, а по показанной части.
  let findingsTotal = $state<number | null>(null);
  let runs = $state<EvaluationRun[] | null>(null);
  let runsTotal = $state<number | null>(null);
  let runsOffset = $state(0);
  let runsMoreLoading = $state(false);
  let runsMoreError = $state('');
  // Пустая страница при положительном остатке: смещение не сдвинулось,
  // показывать больше нечего — кнопка тогда звала бы в никуда.
  let runsStalled = $state(false);
  let runsSeq = 0;
  let loading = $state(true);
  let fatal = $state('');
  // Не пришедшие разделы перечисляются человеческими строками: экран говорит,
  // чего именно он не получил, без кодов ответов и путей.
  let failed = $state<string[]>([]);
  // Состояние модели приходит с сервера. Сбой его чтения — не «модель мертва»
  // и не пустая строка: экран отдельно знает, что показания не пришли.
  let health = $state<SystemStatus | null>(null);
  let healthMissing = $state(false);

  const canEvaluate = $derived(session.can('evaluation:view'));
  const canRestricted = $derived(session.can('restricted:read'));
  const sessionPending = $derived(session.state === 'unknown');

  const sample = $derived(findings ?? []);
  const withEvidence = $derived(sample.filter((finding) => finding.evidence.length > 0).length);
  const withNumbers = $derived(sample.filter((finding) => finding.observations.length > 0).length);
  const disputed = $derived(sample.filter((finding) => finding.status === 'disputed').length);
  const corpusEmpty = $derived(dash !== null && dash.documents === 0 && dash.claims === 0);
  // Пробелы сверх показанного списка посчитаны, но не перечислены: называем обе
  // величины вместо догадок о скрытой части.
  const gapsTotal = $derived(dash ? dash.gaps + dash.gaps_omitted : 0);

  const process = $derived(dash?.agent_metrics ?? null);

  // Занятость контура остаётся на экране состоянием, а не числами: по ней
  // видно, что запрос может быть отклонён. Сами числа — под «Служебными
  // данными».
  const serviceBusy = $derived.by(() => {
    if (!process) return false;
    return process.agent_runs_limit > 0 && process.agent_runs_active >= process.agent_runs_limit;
  });

  // Единственный показатель агентного контура, нужный при разборе обращения
  // в сервис: доля обращений к модели, завершившихся сбоем.
  const modelCalls = $derived((process?.agents ?? []).reduce((sum, item) => sum + item.calls, 0));
  const modelFailures = $derived(
    (process?.agents ?? []).reduce((sum, item) => sum + item.failures, 0),
  );

  // Числа процесса, которые не влияют на решение аналитика, но нужны при разборе
  // обращения в сервис. Имена берутся из словаря, служебные ключи наружу не
  // выходят.
  const serviceFacts = $derived.by(() => {
    if (!process) return [] as { key: string; value: string }[];
    return [
      { key: 'agent_runs_active', value: num(process.agent_runs_active) },
      { key: 'agent_runs_limit', value: num(process.agent_runs_limit) },
      { key: 'agent_runs_refused', value: num(process.agent_runs_refused) },
      { key: 'llm_calls_in_flight', value: num(process.llm_calls_in_flight) },
      { key: 'llm_calls_waiting', value: num(process.llm_calls_waiting) },
      { key: 'llm_slots', value: num(process.llm_slots) },
      { key: 'total_prompt_tokens', value: num(process.total_prompt_tokens) },
      { key: 'total_completion_tokens', value: num(process.total_completion_tokens) },
    ];
  });

  // Идентификаторы показанных проверок: по ним ответ ищут в рабочем
  // пространстве и при разборе обращения в сервис. В строке таблицы их нет,
  // они живут под «Служебными данными» и ограничены тем же списком, что на
  // экране.
  const runRefs = $derived((runs ?? []).slice(0, 40));

  const corpusRows = $derived.by(() => {
    if (!dash) return [] as { key: string; label: string; value: string }[];
    // Один падеж на весь список и ни одного служебного слова: строка называет
    // величину, а не этап обработки корпуса.
    const rows = [
      { key: 'documents', label: 'документов', value: num(dash.documents) },
      { key: 'claims', label: 'утверждений', value: num(dash.claims) },
      { key: 'entities', label: 'сущностей', value: num(dash.entities) },
      { key: 'evidence', label: 'доказательств', value: num(dash.evidence) },
      { key: 'conflicts', label: 'оспоренных утверждений', value: num(dash.conflicts) },
      { key: 'gaps', label: 'пробелов', value: num(dash.gaps) },
    ];
    // Фрагменты текста тоже входят в размер корпуса: отдельной строки о том, как
    // из них считается полоса, не требуется.
    if (stats) {
      rows.splice(3, 0, { key: 'chunks', label: 'фрагментов текста', value: num(stats.chunks) });
    }
    return rows;
  });

  const coverageRows = $derived([
    { key: 'evidence', part: withEvidence, tone: '' },
    { key: 'numbers', part: withNumbers, tone: '' },
    { key: 'disputed', part: disputed, tone: 'bar__fill--disputed' },
  ]);

  function share(part: number, total: number): number {
    return total > 0 ? Math.max(0, Math.min(100, Math.round((part / total) * 100))) : 0;
  }

  // Знаменатель покрытий называется честно: доли считаются по показанным
  // находкам, а сколько их всего в корпусе — отдельное число.
  const coverageShown = $derived(shownSentenceOf(sample.length, findingsTotal, FINDING_NOUNS));
  const coveragePartial = $derived(
    findingsTotal === null && sample.length >= COVERAGE_WINDOW
      ? journalIncomplete(FINDING_NOUNS)
      : '',
  );

  // Сколько проверок осталось за показанной страницей: без полного числа это
  // неизвестность, и «Показать ещё» тогда не предлагается.
  const runsLeft = $derived(
    runs === null || runsTotal === null ? null : Math.max(0, runsTotal - runs.length),
  );
  const runsUnloaded = $derived(journalUnloaded(runsTotal, CHECK_NOUNS, 'проверок'));
  const runsShown = $derived(shownSentenceOf(runs?.length ?? 0, runsTotal, CHECK_NOUNS));
  // Кнопка называет порцию, которую действительно добавляет: «Показать ещё
  // 20 проверок», а не абстрактное «ещё».
  const runsMoreLabel = $derived(
    journalLoadMoreOf(runsLeft ?? 0, RUNS_FEED_SIZE, CHECK_NOUNS),
  );
  const runsPartial = $derived(
    runsTotal === null && runs !== null && runs.length >= RUNS_FEED_SIZE
      ? journalIncomplete(CHECK_NOUNS)
      : '',
  );

  // Догружение по серверному смещению: показанное остаётся, отказ владеет
  // только своей строкой. Перезапуск load() по счётчику отсекает запоздавшую
  // страницу, иначе в обновлённый журнал примешался бы прежний срез.
  async function loadRunsMore(): Promise<void> {
    if (runs === null) return;
    const call = ++runsSeq;
    const offset = runsOffset;
    runsMoreLoading = true;
    runsMoreError = '';
    try {
      const page = await api.evaluations(RUNS_FEED_SIZE, offset);
      if (call !== runsSeq) return;
      runsOffset = offset + page.items.length;
      const seen = new Set(runs.map((run) => run.id));
      runs = [...runs, ...page.items.filter((run) => !seen.has(run.id))];
      runsStalled = page.items.length === 0;
      if (page.total !== null) runsTotal = page.total;
    } catch {
      if (call === runsSeq) runsMoreError = JOURNAL_WINDOW_WORDS.moreFailed;
    } finally {
      if (call === runsSeq) runsMoreLoading = false;
    }
  }

  async function load(): Promise<void> {
    loading = true;
    fatal = '';
    failed = [];
    runsSeq += 1;
    runsMoreError = '';
    runsStalled = false;

    const [panel, corpus, log, evals, state] = await Promise.allSettled([
      api.dashboard(),
      api.corpusStats(),
      api.findings(undefined, undefined, COVERAGE_WINDOW, 0),
      canEvaluate ? api.evaluations(RUNS_FEED_SIZE, 0) : Promise.resolve(null),
      api.status(),
    ]);

    if (panel.status === 'fulfilled') {
      dash = panel.value;
    } else {
      dash = null;
      fatal = 'Не удалось считать состояние корпуса.';
    }

    stats = corpus.status === 'fulfilled' ? corpus.value : null;
    if (corpus.status === 'rejected') failed.push('Показания корпуса не пришли.');

    // Отказ опроса находок не превращается в «корпус пуст»: список остаётся
    // null, а доли по нему не рисуются.
    findings = log.status === 'fulfilled' ? log.value.items : null;
    findingsTotal = log.status === 'fulfilled' ? log.value.total : null;
    if (log.status === 'rejected') failed.push('Находки корпуса не пришли.');

    // Без доступа к проверкам запрос не отправляется вовсе, и раздел говорит об
    // этом прямо вместо пустой таблицы. Полное число проверок приходит только
    // заголовком ответа: длина страницы — не объём журнала.
    if (!canEvaluate) {
      runs = null;
      runsTotal = null;
      runsOffset = 0;
    } else if (evals.status === 'fulfilled' && evals.value !== null) {
      runs = evals.value.items;
      runsTotal = evals.value.total;
      runsOffset = evals.value.items.length;
    } else {
      runs = null;
      runsTotal = null;
      runsOffset = 0;
      if (evals.status === 'rejected') failed.push('Проверки качества не пришли.');
    }

    health = state.status === 'fulfilled' ? state.value : null;
    healthMissing = state.status === 'rejected';

    loading = false;
  }

  $effect(() => {
    // Пока вход не подтверждён, доступа к показаниям нет и просить их рано.
    // canEvaluate читается внутри load() синхронно, поэтому приход права с
    // профиля перезапускает чтение — раздел проверок не остаётся пустым по
    // недосмотру.
    if (sessionPending) return;
    void load();
  });
</script>

<svelte:head>
  <title>Корпус и проверки ответов: Научный Клубок</title>
  <meta
    name="description"
    content="Размер корпуса, его покрытие и проверки ответов: чем сервис отвечает сейчас и что подтверждено источниками."
  />
</svelte:head>

<div class="page dash">
  <div class="scene" aria-hidden="true">
    <span class="blob blob--sage" style="width:38vmax;height:38vmax;top:-20vmax;left:-14vmax"></span>
    <span class="blob blob--coral" style="width:40vmax;height:40vmax;top:-22vmax;right:-16vmax"></span>
    <span class="blob blob--lav" style="width:30vmax;height:30vmax;top:24vmax;right:-12vmax"></span>
  </div>

  <div class="wrap dash__inner">
    <SectionHead
      level="1"
      eyebrow={narrow ? '' : 'качество'}
      title="Корпус и проверки ответов"
      lead="Сколько в корпусе документов, чего он не покрывает и как прошли проверки ответов."
    >
      <div class="row dash__head-actions">
        <Button variant="action" href="/research">Задать вопрос</Button>
        <Button variant="quiet" href="/conflicts">Открыть «{navLabel('/conflicts')}»</Button>
        <Button variant="ghost" icon="refresh" busy={loading} disabled={loading} onclick={() => void load()}>
          {loading ? 'Считываем…' : 'Обновить показания'}
        </Button>
      </div>
    </SectionHead>

    <!-- Можно ли спрашивать прямо сейчас: чем собирается ответ и занят ли контур. -->
    <div class="dash__ready">
      {#if health}
        <div class="row dash__ready-row">
          <StatusPill
            status={health.status === 'ready' ? 'consensus' : 'disputed'}
            label={health.status === 'ready' ? 'сервис отвечает' : 'сервис отвечает ограниченно'}
          />
          <p class="micro muted">{MODEL_MODE_LABELS[health.model_mode]}</p>
        </div>
      {:else if healthMissing && !loading}
        <p class="micro muted dash__ready-note">Состояние сервиса не получено. Обновите показания.</p>
      {/if}
      {#if serviceBusy}
        <p class="micro muted dash__ready-note">
          Сервис занят: новый вопрос может подождать или быть отклонён.
        </p>
      {/if}
    </div>

    {#if loading && !dash}
      <Panel tone="sunk">
        <div class="row dash__loading">
          <span class="spinner" aria-hidden="true"></span>
          <p class="small">
            {#if sessionPending}
              Подтверждаем вход. Показания корпуса начнём читать сразу после.
            {:else}
              Считываем показания корпуса и проверки ответов.
            {/if}
          </p>
        </div>
        <div class="dash__skeletons">
          {#each SKELETONS as i (i)}
            <div class="dash__metric">
              <span class="stack dash__metric-term">
                <span class="skeleton sk-line"></span>
                <span class="skeleton sk-line sk-line--short"></span>
              </span>
              <span class="skeleton sk-num"></span>
            </div>
          {/each}
        </div>
      </Panel>
    {:else if fatal}
      <Panel tone="coral">
        <div class="dash__fault">
          <p class="eyebrow">
            <Icon name="alert" size={16} />
            показания не получены
          </p>
          <h2 class="h3">Состояние корпуса не получено</h2>
          <p class="small">{fatal}</p>
          <div class="row dash__actions">
            <Button variant="action" icon="refresh" busy={loading} disabled={loading} onclick={() => void load()}>
              Повторить запрос
            </Button>
            <Button variant="quiet" href="/research">Задать вопрос</Button>
          </div>
        </div>
      </Panel>
    {:else if dash}
      {#if failed.length}
        <Notice tone="warn" title="Часть показаний не получена">
          {#each failed as line (line)}
            <span class="failed-line">{line}</span>
          {/each}
          <span class="failed-line">Обновите показания.</span>
        </Notice>
      {/if}

      <!-- ── Размер корпуса ─────────────────────────────────────────── -->
      <section class="dash__block dash__block--first">
        <SectionHead level="2" eyebrow={narrow ? '' : 'корпус'} title="Насколько велик корпус" />

        <Panel>
          <dl class="dash__metrics">
            {#each corpusRows as row (row.key)}
              <div class="dash__metric">
                <dt class="stack dash__metric-term"><span class="small">{row.label}</span></dt>
                <dd class="metric__num num dash__metric-value">{row.value}</dd>
              </div>
            {/each}
          </dl>

          {#if dash.gaps_omitted > 0}
            <p class="micro muted dash__legend">
              Пробелов больше, чем показывает список: всего <span class="num">{num(gapsTotal)}</span>.
            </p>
          {/if}
        </Panel>

        <div class="grid grid--2 dash__ratios">
          {#if stats}
            <Panel>
              <div class="dash__ratio">
                <div class="row row--between">
                  <p class="small">{termOf(COVERAGE_LABELS, 'semantic_documents')}</p>
                  {#if stats.documents > 0}
                    <span class="tag num">{share(stats.semantic_documents, stats.documents)} %</span>
                  {/if}
                </div>
                {#if stats.documents > 0}
                  <div class="bar dash__bar">
                    <span
                      class="bar__fill"
                      style="width: {share(stats.semantic_documents, stats.documents)}%;"></span>
                  </div>
                  <p class="micro muted">
                    <span class="num">{num(stats.semantic_documents)}</span> из
                    <span class="num">{num(stats.documents)}</span> документов.
                  </p>
                {:else}
                  <p class="micro muted">Документов в корпусе нет. Загрузите документ.</p>
                {/if}
              </div>
            </Panel>
          {:else}
            <Notice tone="warn" title="Показания корпуса не пришли">
              Долю разбора не посчитать. Обновите показания.
            </Notice>
          {/if}

          {#if findings}
            <Panel>
              {#if sample.length > 0}
                {#each coverageRows as row (row.key)}
                  <div class="dash__ratio">
                    <div class="row row--between">
                      <p class="small">{termOf(COVERAGE_LABELS, row.key)}</p>
                      <span class="tag num">{share(row.part, sample.length)} %</span>
                    </div>
                    <div class="bar dash__bar">
                      <span
                        class="bar__fill {row.tone}"
                        style="width: {share(row.part, sample.length)}%;"></span>
                    </div>
                    <p class="micro muted">
                      <span class="num">{num(row.part)}</span> из
                      <span class="num">{num(sample.length)}</span> находок.
                    </p>
                  </div>
                {/each}
                <!-- Знаменатель называется прямо: доля относится к показанной
                     части списка находок, а не ко всему корпусу. Каждая
                     величина стоит своей подписанной строкой. -->
                <div class="stack dash__notes">
                  <p class="micro muted">Доли посчитаны по показанным находкам.</p>
                  <p class="micro muted">{coverageShown}.</p>
                  {#if coveragePartial}
                    <p class="micro muted">{coveragePartial}.</p>
                  {/if}
                  {#if !canRestricted}
                    <p class="micro muted">Закрытые находки в подсчёт не входят.</p>
                  {/if}
                </div>
              {:else}
                <p class="micro muted">
                  Доступных вашему аккаунту находок пока нет. Доли по ним не посчитать.
                </p>
                <div class="row dash__actions">
                  <Button variant="quiet" href="/research">Задать вопрос</Button>
                </div>
              {/if}
            </Panel>
          {:else}
            <Notice tone="warn" title="Находки не пришли">
              Доли по находкам не посчитать. Обновите показания.
            </Notice>
          {/if}
        </div>

        <!-- Единственное место, где экран объясняет свою методику. -->
        <div class="acc">
          <button
            type="button"
            class="acc__head"
            aria-expanded={methodOpen}
            aria-controls="dash-method"
            onclick={() => (methodOpen = !methodOpen)}
          >
            <span>Как это устроено</span>
            <Icon name="plus" size={16} class="acc__icon" />
          </button>
          {#if methodOpen}
            <div class="acc__body" id="dash-method">
              <p>
                Документы, утверждения, сущности и доказательства измеряют разное: их доли не
                складываются в одну ось.
              </p>
              <p>Доля не рисуется, когда считать не с чего. Экран различает пустой корпус и не
                пришедшие показания.</p>
              <p>
                Оспаривается: по этому утверждению источники дают разные числа. Пробелом называем
                пару «свойство и объект», которую корпус не закрыл.
              </p>
            </div>
          {/if}
        </div>

        {#if corpusEmpty}
          <Empty
            icon="layers"
            title="В корпусе нет ни документов, ни утверждений"
            body="Загрузите документы в разделе «Вопрос». Показания обновятся после разбора."
          >
            {#snippet action()}
              <div class="row">
                <Button variant="action" href="/research">Задать вопрос</Button>
                <Button variant="quiet" icon="refresh" busy={loading} disabled={loading} onclick={() => void load()}>Обновить показания</Button>
              </div>
            {/snippet}
          </Empty>
        {/if}
      </section>

      <!-- ── Проверки ответов ───────────────────────────────────────── -->
      <section class="dash__block">
        <SectionHead
          level="2"
          eyebrow={narrow ? '' : 'ответы'}
          title="Проверки ответов"
          lead="Проверка считает ответ по источнику. Что о себе сказала модель, показано отдельной колонкой."
        />

        {#if !canEvaluate}
          <Panel tone="lav">
            <p class="eyebrow"><Icon name="shield" size={16} /> раздел закрыт</p>
            <p class="small">
              Проверки ответов этому аккаунту не открыты. Право выдаёт администратор сервиса.
            </p>
            <p class="small">Вопрос, находки и отзывы на ответ работают как обычно.</p>
            <div class="row dash__actions">
              <Button variant="action" href="/research">Задать вопрос</Button>
            </div>
          </Panel>
        {:else if runs === null}
          <Panel tone="coral">
            <div class="dash__fault">
              <p class="eyebrow"><Icon name="alert" size={16} /> проверки не получены</p>
              <p class="small">Журнал проверок не пришёл. Обновите показания.</p>
              <div class="row dash__actions">
                <Button variant="action" icon="refresh" busy={loading} disabled={loading} onclick={() => void load()}>
                  Повторить
                </Button>
              </div>
            </div>
          </Panel>
        {:else if !runs.length}
          <!-- Пустая таблица при ненулевом или неизвестном полном числе — это
               незагруженный журнал, а не «проверки не считались». -->
          {#if runsUnloaded}
            <Panel tone="coral">
              <div class="dash__fault">
                <p class="eyebrow"><Icon name="alert" size={16} /> проверки не загружены</p>
                <p class="small"><strong>{runsUnloaded.title}.</strong> {runsUnloaded.body}</p>
                <div class="row dash__actions">
                  <Button variant="action" icon="refresh" busy={loading} disabled={loading} onclick={() => void load()}>
                    Повторить запрос
                  </Button>
                </div>
              </div>
            </Panel>
          {:else}
            <Empty
              icon="gauge"
              title="Проверки ответов ещё не считались"
              body="Проверка считается по каждому ответу раздела «Вопрос». Пока ответов не было, проверок и процентов на экране нет."
            >
              {#snippet action()}
                <Button variant="action" href="/research">Задать вопрос</Button>
              {/snippet}
            </Empty>
          {/if}
        {:else}
          <Panel flush>
            <div class="table-wrap">
              <table class="table dash__table">
                <thead>
                  <tr>
                    <th scope="col" rowspan="2">
                      {CHECK_TABLE_LABELS.when}
                    </th>
                    <th scope="colgroup" colspan={GROUNDED_KEYS.length}>
                      {CHECK_TABLE_LABELS.grounded}
                    </th>
                    <th scope="colgroup" colspan={SELF_REPORTED_KEYS.length} class="dash__sep">
                      {CHECK_TABLE_LABELS.selfReported}
                    </th>
                    <th scope="col" rowspan="2">{CHECK_TABLE_LABELS.verdict}</th>
                  </tr>
                  <tr>
                    {#each GROUNDED_KEYS as key (key)}
                      <th scope="col" class="n">{termOf(EVAL_METRIC_LABELS, key)}</th>
                    {/each}
                    {#each SELF_REPORTED_KEYS as key (key)}
                      <th scope="col" class="n dash__sep">{termOf(EVAL_METRIC_LABELS, key)}</th>
                    {/each}
                  </tr>
                </thead>
                <tbody>
                  {#each runs as run (run.id)}
                    <tr>
                      <td data-label={CHECK_TABLE_LABELS.when}>
                        <time datetime={run.created_at}>{dateTime(run.created_at)}</time>
                      </td>
                      {#each GROUNDED_KEYS as key (key)}
                        <td class="n" data-label={termOf(EVAL_METRIC_LABELS, key)}>
                          {pct(run.metrics[key])}
                        </td>
                      {/each}
                      {#each SELF_REPORTED_KEYS as key (key)}
                        <td class="n dash__sep" data-label={termOf(EVAL_METRIC_LABELS, key)}>
                          {pct(run.metrics[key])}
                        </td>
                      {/each}
                      <td data-label={CHECK_TABLE_LABELS.verdict}>
                        <StatusPill
                          status={run.passed ? 'consensus' : 'disputed'}
                          label={run.passed ? CHECK_TABLE_LABELS.passed : CHECK_TABLE_LABELS.notPassed}
                        />
                      </td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          </Panel>
          <p class="micro muted dash__legend">
            {runsShown}.
            {#if runsPartial}{runsPartial}.{/if}
          </p>
          {#if runsStalled}
            <!-- Остаток назван, а страница пришла пустой: действие убрано, иначе
                 клик был бы пустым. Тупик отличается и от сбоя, и от «показаны
                 все», поэтому говорит о себе своей строкой. -->
            <Notice tone="warn" title={JOURNAL_WINDOW_WORDS.stalledTitle}>
              {JOURNAL_WINDOW_WORDS.stalled}
            </Notice>
          {:else if runsLeft !== null && runsLeft > 0}
            <div class="row dash__actions">
              <Button
                variant="quiet"
                busy={runsMoreLoading}
                disabled={runsMoreLoading}
                onclick={() => void loadRunsMore()}
              >
                {runsMoreLoading ? JOURNAL_WINDOW_WORDS.loadingMore : runsMoreLabel}
              </Button>
            </div>
          {/if}
          {#if runsMoreError}
            <Notice tone="error" title={JOURNAL_WINDOW_WORDS.moreFailedTitle}>
              {runsMoreError}
            </Notice>
          {/if}
          <div class="stack dash__notes">
            <p class="micro muted">{CHECK_TABLE_LABELS.groundedNote}</p>
            <p class="micro muted">{CHECK_TABLE_LABELS.selfReportedNote}</p>
            <p class="micro muted">{CHECK_TABLE_LABELS.verdictNote}</p>
          </div>
        {/if}
      </section>

      <!-- ── Служебные данные ───────────────────────────────────────── -->
      <section class="dash__block">
        <div class="acc">
          <button
            type="button"
            class="acc__head"
            aria-expanded={serviceOpen}
            aria-controls="dash-service"
            onclick={() => (serviceOpen = !serviceOpen)}
          >
            <span>Служебные данные</span>
            <Icon name="plus" size={16} class="acc__icon" />
          </button>
          {#if serviceOpen}
            <div class="acc__body" id="dash-service">
              <p>
                Числа агентного контура с момента запуска процесса: на решения аналитика они не влияют,
                но по ним сервис объясняет отказ приёма запроса.
              </p>
              {#if process}
                <dl class="kv dash__facts">
                  {#each serviceFacts as fact (fact.key)}
                    <dt>{termOf(PROCESS_METRIC_LABELS, fact.key)}</dt>
                    <dd class="num">{fact.value}</dd>
                  {/each}
                </dl>
                {#if modelCalls > 0}
                  <p class="micro muted">
                    Сбоем завершилось <span class="num">{share(modelFailures, modelCalls)}</span> %
                    обращений к модели: <span class="num">{num(modelFailures)}</span> из
                    <span class="num">{num(modelCalls)}</span>.
                  </p>
                {/if}
                <p class="micro muted">Снимок датирован {dateTime(process.generated_at)}.</p>
              {/if}
              <p class="micro muted">
                Действия этого аккаунта и, где на них есть право, действия корпуса читаются в профиле.
              </p>
              <div class="row dash__actions">
                <Button variant="quiet" href="/account">Открыть профиль</Button>
              </div>
              {#if runRefs.length > 0}
                <h3 class="h4 dash__sub">Идентификаторы показанных проверок</h3>
                <p class="micro muted">
                  По ним ответ ищут в разделе «Вопрос» и при разборе обращения в сервис.
                </p>
                <dl class="kv dash__ids">
                  {#each runRefs as run (run.id)}
                    <dt><time datetime={run.created_at}>{dateTime(run.created_at)}</time></dt>
                    <dd class="tech">{run.query_id}</dd>
                  {/each}
                </dl>
              {/if}
            </div>
          {/if}
        </div>
      </section>
    {/if}
  </div>
</div>

<style>
  /* Экран живёт в рабочей оболочке с документным скроллом: высоту задаёт .page,
     а не собственная высота блока. */
  .dash {
    position: relative;
  }

  .dash__inner {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  .dash__block {
    margin-top: var(--s7);
  }

  /* Первый блок — числа корпуса: он идёт сразу за шапкой экрана. */
  .dash__block--first {
    margin-top: var(--s5);
  }

  .dash__block--first > .acc {
    margin-top: var(--s4);
  }

  /* Готовность сервиса — плотная строка под шапкой, а не раздел. */
  .dash__ready {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  .dash__ready-row {
    --gap: var(--s4);
  }

  .dash__ready-note {
    max-width: 88ch;
  }

  /* .row уже задаёт align-items: center — селектор поднимается до двух
     классов, чтобы порядок подключения app.css не решал исход. */
  .row.dash__loading {
    --gap: var(--s4);
    align-items: flex-start;
  }

  .dash__skeletons {
    margin-top: var(--s5);
  }

  .sk-num {
    display: block;
    height: var(--s6);
    width: calc(var(--s5) * 3);
  }

  .sk-line {
    height: var(--s4);
    width: 70%;
  }

  .sk-line--short {
    width: 45%;
  }

  .dash__fault {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  .dash__actions {
    --gap: var(--s4);
    margin-top: var(--s3);
  }

  /* Лента показаний — строки одного списка: величина читается числом в
     монопространственном ряду справа, подпись — слева. Подложек у строк нет:
     карточкой уже является панель. */
  .dash__metrics,
  .dash__skeletons {
    display: grid;
  }

  .dash__metric {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: baseline;
    column-gap: var(--s5);
    padding-block: var(--s3);
  }

  .dash__metric + .dash__metric {
    border-top: 1px solid var(--line-soft);
  }

  .dash__metric-term {
    --gap: var(--s1);
  }

  .dash__metric-value {
    text-align: right;
  }

  /* Скелетон строки: у блоков нет текстовой базы, ряд центрируется. */
  .dash__skeletons .dash__metric {
    align-items: center;
  }

  .dash__metrics .metric__num {
    font-size: var(--t-h3);
  }

  /* Покрытия встают в две колонки на широком экране: первый viewport держит
     статус, числа корпуса и главное действие, а не уходит в прокрутку. */
  .dash__ratios {
    --gap: var(--s5);
    margin-top: var(--s5);
  }

  .dash__ratio {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  .dash__ratio + .dash__ratio {
    margin-top: var(--s5);
    padding-top: var(--s5);
    border-top: 1px solid var(--line-soft);
  }

  .dash__bar {
    margin-block: var(--s1);
  }

  .dash__legend {
    max-width: 88ch;
    margin-top: var(--s4);
  }

  /* Расшифровки метрик: короткие строки под таблицей, а не абзац о методике. */
  .dash__notes {
    --gap: var(--s2);
    margin-top: var(--s4);
    max-width: 88ch;
  }

  /* Отделение колонки самооценки модели: детерминированные метрики и слова
     модели читаются как два разных блока, а не одна ось. */
  .dash__sep {
    border-left: 1px solid var(--line);
    padding-left: var(--s4);
  }

  .dash__sub {
    margin-top: var(--s5);
    font-size: var(--t-small);
    font-weight: 600;
  }

  /* Идентификаторы проверок живут только здесь: в строке таблицы их нет. */
  .dash__ids {
    margin-top: var(--s3);
    grid-template-columns: minmax(0, auto) minmax(0, 1fr);
  }

  .dash__facts {
    margin-top: var(--s4);
  }

  .dash__head-actions {
    --gap: var(--s3);
  }

  .failed-line {
    display: block;
  }

  /* Числовые колонки: перекрываем text-align из app.css (.table th), поэтому
     селектор поднимается до уровня глобального правила. */
  .table.dash__table th.n,
  .table.dash__table td.n {
    text-align: right;
    white-space: normal;
    font-variant-numeric: tabular-nums;
  }

  @media (max-width: 640px) {
    .dash__inner {
      gap: var(--s3);
    }

    .dash__block {
      margin-top: var(--s6);
    }

    .dash__block--first {
      margin-top: var(--s4);
    }

    /* Шапка экрана на телефоне: заголовок и действие в одну строку, числа
       начинаются сразу под ними. */
    .dash__inner :global(.section-head) {
      align-items: center;
      gap: var(--s3);
      margin-bottom: var(--s3);
    }

    .dash__inner :global(.section-head__text) {
      min-width: 0;
      flex: 1 1 auto;
    }

    .dash__inner :global(.section-head) > :global(.btn) {
      flex: none;
    }

    /* Телефон: строка остаётся строкой, но число крупнее: подпись переносится
       и не спорит с величиной. */
    .dash__metric {
      column-gap: var(--s4);
      padding-block: var(--s2);
    }

    .dash__metrics .metric__num {
      font-size: var(--t-h2);
    }

    /* Семь колонок проверки на узком экране читаются карточкой строки: каждая
       величина встаёт своей подписанной строкой, боковая прокрутка не нужна.
       Подпись берётся из data-label, который задаётся тем же словарём, что и
       заголовок колонки. */
    .table.dash__table thead {
      display: none;
    }

    .table.dash__table,
    .table.dash__table tbody,
    .table.dash__table tr,
    .table.dash__table td {
      display: block;
      width: 100%;
    }

    .table.dash__table td {
      border-top: 0;
      padding: var(--s1) 0;
      text-align: left;
    }

    .table.dash__table td.n {
      text-align: left;
    }

    .table.dash__table td::before {
      content: attr(data-label);
      display: block;
      color: var(--ink-3);
      font-size: var(--t-micro);
    }

    .table.dash__table tr + tr {
      border-top: 1px solid var(--line-soft);
    }

    .table.dash__table td.dash__sep {
      border-left: 0;
      padding-left: 0;
    }
  }
</style>
