<script lang="ts">
  import { api } from '$lib/api';
  import { countOf, dateTime, num, pct } from '$lib/format';
  import { session } from '$lib/sessionStore.svelte';
  import {
    COVERAGE_LABELS,
    EVAL_METRIC_LABELS,
    MODEL_MODE_LABELS,
    PROCESS_METRIC_LABELS,
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
  // технические числа процесса убраны под «Служебные данные».
  const METRIC_KEYS: (keyof EvaluationRun['metrics'])[] = [
    'citation_coverage',
    'numeric_support',
    'unsupported_claim_ratio',
    'mean_finding_confidence',
    'overall',
  ];

  const SKELETONS = [0, 1, 2, 3, 4, 5];

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
  let runs = $state<EvaluationRun[] | null>(null);
  let loading = $state(true);
  let fatal = $state('');
  // Не пришедшие разделы перечисляются человеческими строками: экран говорит,
  // чего именно он не дочитал, без кодов ответов и путей.
  let failed = $state<string[]>([]);
  // Состояние модели приходит из /health/ready. Сбой его чтения — не «модель
  // мертва» и не пустая строка: экран отдельно знает, что показания не пришли.
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

  // Занятость контура — одна строка вместо раздела о работе агента: по ней
  // видно, что сервис занят и запрос может быть отклонён.
  const capacity = $derived.by(() => {
    if (!process) return '';
    return `Запросов в работе ${num(process.agent_runs_active)} из ${num(process.agent_runs_limit)}, отказов приёму ${num(process.agent_runs_refused)}`;
  });

  // Единственный показатель агентного контура, остающийся на читаемой части
  // экрана: доля обращений к модели, завершившихся сбоем.
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

  const corpusRows = $derived.by(() => {
    if (!dash) return [] as { key: string; label: string; value: string }[];
    const rows = [
      { key: 'documents', label: 'документов', value: num(dash.documents) },
      { key: 'claims', label: 'утверждений', value: num(dash.claims) },
      { key: 'entities', label: 'сущностей', value: num(dash.entities) },
      { key: 'evidence', label: 'доказательств', value: num(dash.evidence) },
      { key: 'conflicts', label: 'оспаривается', value: num(dash.conflicts) },
      { key: 'gaps', label: 'пробелов', value: num(dash.gaps) },
    ];
    // Фрагменты разметки — тоже размер корпуса: отдельной строкой о том, как из
    // них считается полоса, они не нуждаются.
    if (stats) {
      rows.splice(3, 0, { key: 'chunks', label: 'фрагментов разметки', value: num(stats.chunks) });
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

  async function load(): Promise<void> {
    loading = true;
    fatal = '';
    failed = [];

    const [panel, corpus, log, evals, state] = await Promise.allSettled([
      api.dashboard(),
      api.corpusStats(),
      api.findings(),
      canEvaluate ? api.evaluations() : Promise.resolve(null),
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

    findings = log.status === 'fulfilled' ? log.value : null;
    if (log.status === 'rejected') failed.push('Находки корпуса не пришли.');

    // Без доступа к оценке запрос не отправляется вовсе, и раздел говорит об
    // этом прямо вместо пустой таблицы.
    if (!canEvaluate) {
      runs = null;
    } else if (evals.status === 'fulfilled') {
      runs = evals.value ?? [];
    } else {
      runs = null;
      failed.push('Проверки качества не пришли.');
    }

    health = state.status === 'fulfilled' ? state.value : null;
    healthMissing = state.status === 'rejected';

    loading = false;
  }

  $effect(() => {
    // Пока вход не подтверждён, доступа к показаниям нет и просить их рано.
    // canEvaluate читается внутри load() синхронно, поэтому приход доступа с
    // /auth/me перезапускает чтение — раздел оценки не остаётся пустым по недосмотру.
    if (sessionPending) return;
    void load();
  });
</script>

<svelte:head>
  <title>Качество корпуса и ответов — Научный Клубок</title>
  <meta
    name="description"
    content="Размер корпуса, его покрытие и точность ответов: чем сервис отвечает сейчас и чему можно верить."
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
      title="Проверить корпус и точность ответов"
      lead="Сколько в корпусе документов, чего он не покрывает и насколько ответы опираются на источники."
    >
      <Button variant="action" icon="refresh" busy={loading} disabled={loading} onclick={() => void load()}>
        {loading ? 'Считываем…' : 'Обновить показания'}
      </Button>
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
        <p class="micro muted dash__ready-note">
          Состояние сервиса не прочитано — обновите показания, прежде чем делать вывод о модели.
        </p>
      {/if}
      {#if capacity}
        <p class="micro muted dash__ready-note">{capacity}.</p>
      {/if}
    </div>

    {#if loading && !dash}
      <Panel tone="sunk">
        <div class="row dash__loading">
          <span class="spinner" aria-hidden="true"></span>
          <p class="small">
            {#if sessionPending}
              Подтверждаем вход — показания корпуса начнём читать сразу после.
            {:else}
              Считываем показания корпуса и проверки качества ответов.
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
          <h2 class="h3">Состояние корпуса не прочитано</h2>
          <p class="small">{fatal} Это не пустой корпус — данные просто не пришли.</p>
          <div class="row dash__actions">
            <Button variant="action" icon="refresh" busy={loading} disabled={loading} onclick={() => void load()}>
              Повторить запрос
            </Button>
            <Button variant="quiet" href="/research">Рабочее пространство</Button>
          </div>
        </div>
      </Panel>
    {:else if dash}
      {#if failed.length}
        <Notice tone="warn" title="Часть показаний не получена">
          {#each failed as line (line)}
            <span class="failed-line">{line}</span>
          {/each}
          <span class="failed-line">Обновите показания — приблизительных чисел вместо них нет.</span>
        </Notice>
      {/if}

      <!-- ── Размер корпуса ─────────────────────────────────────────── -->
      <section class="dash__block dash__block--first">
        <SectionHead level="2" eyebrow={narrow ? '' : 'состав корпуса'} title="Насколько велик корпус" />

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

        <div class="stack dash__ratios">
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
                    Разобрано <span class="num">{num(stats.semantic_documents)}</span> из
                    <span class="num">{num(stats.documents)}</span> документов.
                  </p>
                {:else}
                  <p class="micro muted">
                    Документов в корпусе нет — показать долю разбора не на чем. Загрузите документ.
                  </p>
                {/if}
              </div>
            </Panel>
          {:else}
            <Notice tone="warn" title="Показания корпуса не пришли">
              Долю семантического разбора не показать: цифры не получены, а не равны нулю. Обновите
              показания.
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
                      <span class="num">{num(sample.length)}</span>
                    </p>
                  </div>
                {/each}
                <p class="micro muted dash__legend">
                  Считано по находкам, доступным вашему аккаунту:
                  {countOf(sample.length, 'находка', 'находки', 'находок')}{#if !canRestricted}, без
                  закрытого класса{/if}.
                </p>
              {:else}
                <p class="micro muted">
                  Доступных вам находок пока нет — доли по ним показать не на чем. Задайте вопрос или
                  пополните корпус.
                </p>
              {/if}
            </Panel>
          {:else}
            <Notice tone="warn" title="Находки не пришли">
              Доли по находкам не посчитаны: список не получен, а не пуст. Обновите показания.
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
                Документы, утверждения, сущности и доказательства измеряют разное, поэтому их доли не
                складываются в одну ось.
              </p>
              <p>
                Доля не рисуется, когда считать не с чего. Пустой корпус и не пришедшие цифры — разные
                состояния, и экран называет каждое своими словами.
              </p>
              <p>
                Оспориваемое — про величину, по которой источники расходятся; пробел — по паре «свойство
                и объект», которую корпус не закрыл.
              </p>
            </div>
          {/if}
        </div>

        {#if corpusEmpty}
          <Empty
            icon="layers"
            title="В корпусе нет ни документов, ни утверждений"
            body="Загрузите документы в рабочем пространстве — показания обновятся, как только разбор завершится."
          >
            {#snippet action()}
              <div class="row">
                <Button variant="action" href="/research">Открыть рабочее пространство</Button>
                <Button variant="quiet" icon="refresh" busy={loading} disabled={loading} onclick={() => void load()}>Прочитать снова</Button>
              </div>
            {/snippet}
          </Empty>
        {/if}
      </section>

      <!-- ── Качество ответов ───────────────────────────────────────── -->
      <section class="dash__block">
        <SectionHead
          level="2"
          eyebrow={narrow ? '' : 'качество ответов'}
          title="Насколько ответы опираются на источники"
        />

        {#if !canEvaluate}
          <Panel tone="lav">
            <p class="eyebrow"><Icon name="shield" size={16} /> расширенный доступ</p>
            <p class="small">
              Метрики точности ответов этому аккаунту не открыты. Запрос, находки и обратная связь
              работают как обычно.
            </p>
            <div class="row dash__actions">
              <Button variant="action" href="/research">Задать вопрос</Button>
            </div>
          </Panel>
        {:else if runs === null}
          <Panel tone="coral">
            <div class="dash__fault">
              <p class="eyebrow"><Icon name="alert" size={16} /> проверки не получены</p>
              <p class="small">Журнал проверок не прочитан. Обновите показания.</p>
              <div class="row dash__actions">
                <Button variant="action" icon="refresh" busy={loading} disabled={loading} onclick={() => void load()}>
                  Повторить
                </Button>
              </div>
            </div>
          </Panel>
        {:else if !runs.length}
          <Empty
            icon="gauge"
            title="Проверки качества ещё не считались"
            body="Проверка ставится на ответ исследовательского запроса. Пока её нет, процентов точности на экране нет тоже."
          >
            {#snippet action()}
              <Button variant="action" href="/research">Задать вопрос</Button>
            {/snippet}
          </Empty>
        {:else}
          <Panel flush>
            <div class="table-wrap dash__table">
              <table class="table">
                <thead>
                  <tr>
                    <th scope="col">когда</th>
                    {#each METRIC_KEYS as key (key)}
                      <th scope="col" class="n">{termOf(EVAL_METRIC_LABELS, key)}</th>
                    {/each}
                    <th scope="col">вердикт</th>
                  </tr>
                </thead>
                <tbody>
                  {#each runs as run (run.id)}
                    <tr>
                      <td>
                        <time datetime={run.created_at}>{dateTime(run.created_at)}</time>
                        <!-- Идентификатор ответа нужен, чтобы найти его в рабочем
                             пространстве и при разборе обращения в сервис. -->
                        <span class="dash__qid"><code class="tech">{run.query_id}</code></span>
                      </td>
                      {#each METRIC_KEYS as key (key)}
                        <td class="n">{pct(run.metrics[key])}</td>
                      {/each}
                      <td>
                        <StatusPill
                          status={run.passed ? 'consensus' : 'disputed'}
                          label={run.passed ? 'зачтён' : 'не зачтён'}
                        />
                      </td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          </Panel>
          <p class="micro muted dash__legend">
            «Выводы без поддержки» — чем меньше, тем лучше: это доля тезисов без трассировки до
            источника. «Уверенность выводов» оценивает сама модель, а не подтверждённость факта.
          </p>
          {#if modelCalls > 0}
            <p class="micro muted dash__legend--tight">
              Сбоем завершилось <span class="num">{share(modelFailures, modelCalls)}</span> % обращений к
              модели (<span class="num">{num(modelFailures)}</span> из
              <span class="num">{num(modelCalls)}</span>).
            </p>
          {/if}
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
                Числа агентного контура с запуска процесса: на решения аналитика они не влияют, но по
                ним сервис объясняет отказ приёма запроса.
              </p>
              {#if process}
                <dl class="kv dash__facts">
                  {#each serviceFacts as fact (fact.key)}
                    <dt>{termOf(PROCESS_METRIC_LABELS, fact.key)}</dt>
                    <dd class="num">{fact.value}</dd>
                  {/each}
                </dl>
                <p class="micro muted">Снимок датирован {dateTime(process.generated_at)}.</p>
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

  .dash__legend--tight {
    max-width: 88ch;
    margin-top: var(--s3);
  }

  /* Идентификатор ответа — подписью под моментом проверки, сам заголовком
     строки он не является. */
  .dash__qid {
    display: block;
    margin-top: var(--s1);
  }

  .dash__facts {
    margin-top: var(--s4);
  }

  .failed-line {
    display: block;
  }

  /* Числовые колонки: перекрываем text-align из app.css (.table th), поэтому
     селектор поднимается до уровня глобального правила. */
  .dash__table :global(.table th.n),
  .dash__table :global(.table td.n) {
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

    /* Телефон: строка остаётся строкой, но число крупнее — подпись переносится
       и не спорит с величиной. */
    .dash__metric {
      column-gap: var(--s4);
      padding-block: var(--s2);
    }

    .dash__metrics .metric__num {
      font-size: var(--t-h2);
    }
  }
</style>
