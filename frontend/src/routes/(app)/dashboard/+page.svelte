<script lang="ts">
  import { api } from '$lib/api';
  import { countOf, num, pct } from '$lib/format';
  import { scrollRegion } from '$lib/scroll-region';
  import { session } from '$lib/sessionStore.svelte';
  import { AGENT_LABELS, knownTerm } from '$lib/terms';
  import {
    type CorpusStats,
    type DashboardData,
    type EvaluationRun,
    type FindingListItem,
  } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';

  const ACTION_LABELS: Record<string, string> = {
    'auth.register': 'Регистрация аккаунта',
    'auth.login': 'Вход в рабочее пространство',
    'auth.logout': 'Выход',
    'auth.password': 'Смена пароля',
    'auth.profile': 'Правка профиля',
    'document.ingest': 'Загрузка документа',
    'query.run': 'Исследовательский запрос',
    'query.stream': 'Запрос в потоке',
    'compare.run': 'Сравнение',
    'export.run': 'Экспорт ответа',
    'feedback.submit': 'Экспертная правка',
    'feedback.supersede': 'Замена утверждения',
    'proposal.review': 'Разбор предложения',
    'resolution.review': 'Разбор слияния сущностей',
  };

  const OUTCOME_LABELS: Record<string, string> = {
    allowed: 'разрешено',
    denied: 'отказано',
    success: 'успех',
    failure: 'сбой',
  };

  // Итог без русской подписи не выводим сырым ключом: экран состояния обязан
  // сказать, что итог в журнале есть, но он не распознан словарём.
  const OUTCOME_UNKNOWN = 'итог не распознан';

  function outcomeLabel(outcome: string): string {
    return OUTCOME_LABELS[outcome] ?? OUTCOME_UNKNOWN;
  }

  // Действие читают по русскому имени: ключ журнала остаётся за кадром, а id
  // объекта идёт подписью при строке — сам заголовком ячейки он не является.
  function actionTerm(action: string): string {
    return knownTerm(ACTION_LABELS, action) ?? 'Действие из журнала';
  }

  // Имя шага прохода: AGENT_LABELS даёт русское название, служебное имя узла
  // остаётся подписью при нём.
  function agentLabel(agent: string): { name: string; named: boolean } {
    const known = knownTerm(AGENT_LABELS, agent);
    return known ? { name: known, named: true } : { name: 'шаг прохода', named: false };
  }

  const METRICS: { key: keyof EvaluationRun['metrics']; label: string }[] = [
    { key: 'citation_coverage', label: 'тезисы с цитатой' },
    { key: 'numeric_support', label: 'числа с поддержкой' },
    { key: 'unsupported_claim_ratio', label: 'выводов без поддержки' },
    { key: 'mean_finding_confidence', label: 'средняя уверенность выводов' },
    { key: 'overall', label: 'итог прогона' },
  ];

  const SKELETONS = [0, 1, 2, 3, 4, 5];

  // Лента показаний — главный объект экрана; знаменатели величин свёрнуты под
  // ней, чтобы состав корпуса читался с первого взгляда.
  let bandOpen = $state(false);

  // Мобильная композиция шапки: метка раздела уходит, действие встаёт рядом с
  // заголовком — лента показаний начинается выше.
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

  const canEvaluate = $derived(session.can('evaluation:view'));
  const canAudit = $derived(session.can('audit:read'));
  const canRestricted = $derived(session.can('restricted:read'));
  const sessionPending = $derived(session.state === 'unknown');

  const sample = $derived(findings ?? []);
  const withEvidence = $derived(sample.filter((finding) => finding.evidence.length > 0).length);
  const withNumbers = $derived(sample.filter((finding) => finding.observations.length > 0).length);
  const disputed = $derived(sample.filter((finding) => finding.status === 'disputed').length);
  const agents = $derived(dash?.agent_metrics.agents ?? []);
  const llm = $derived(dash?.agent_metrics.llm ?? []);
  const corpusEmpty = $derived(dash !== null && dash.documents === 0 && dash.claims === 0);
  // Пробелы сверх выборки посчитаны, но не перечислены: показываем и выбранное,
  // и сумму — без догадок о содержимом скрытой части.
  const gapsTotal = $derived(dash ? dash.gaps + dash.gaps_omitted : 0);

  // Срез журнала формирует сервис, а не экран: панель печатает строки как есть и
  // не приписывает их текущему аккаунту. Фильтр по actor_id в браузере был бы
  // косметикой: он не отменяет то, что уже пришло в ответе.
  const activity = $derived(dash ? dash.recent_activity : []);

  function share(part: number, total: number): number {
    return total > 0 ? Math.max(0, Math.min(100, Math.round((part / total) * 100))) : 0;
  }

  function dateTime(iso: string): string {
    const parsed = new Date(iso);
    return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString('ru-RU');
  }

  async function load(): Promise<void> {
    loading = true;
    fatal = '';
    failed = [];

    const [panel, corpus, log, evals] = await Promise.allSettled([
      api.dashboard(),
      api.corpusStats(),
      api.findings(),
      canEvaluate ? api.evaluations() : Promise.resolve(null),
    ]);

    if (panel.status === 'fulfilled') {
      dash = panel.value;
    } else {
      dash = null;
      fatal = 'Не удалось считать состояние корпуса.';
    }

    stats = corpus.status === 'fulfilled' ? corpus.value : null;
    if (corpus.status === 'rejected') failed.push('Не пришли показания корпуса.');

    findings = log.status === 'fulfilled' ? log.value : null;
    if (log.status === 'rejected') failed.push('Не пришли находки корпуса.');

    // Без доступа к оценке запрос не отправляется вовсе, и раздел говорит об
    // этом прямо вместо пустой таблицы.
    if (!canEvaluate) {
      runs = null;
    } else if (evals.status === 'fulfilled') {
      runs = evals.value ?? [];
    } else {
      runs = null;
      failed.push('Не пришли прогоны оценки.');
    }

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

<div class="page dash">
  <div class="scene" aria-hidden="true">
    <span class="blob blob--sage" style="width:38vmax;height:38vmax;top:-20vmax;left:-14vmax"></span>
    <span class="blob blob--coral" style="width:40vmax;height:40vmax;top:-22vmax;right:-16vmax"></span>
    <span class="blob blob--lav" style="width:30vmax;height:30vmax;top:24vmax;right:-12vmax"></span>
  </div>

  <div class="wrap dash__inner">
    <!-- Лента показаний — главный объект экрана: шапка держит только заголовок
         и действие, пояснения читаются под лентой. -->
    <SectionHead level="1" eyebrow={narrow ? '' : 'панель состояния'} title="Что сейчас в корпусе знаний">
      <Button variant="quiet" icon="refresh" busy={loading} disabled={loading} onclick={() => void load()}>
        {loading ? 'Считываем…' : 'Обновить показания'}
      </Button>
    </SectionHead>

    {#if loading && !dash}
      <Panel tone="sunk">
        <div class="row dash__loading">
          <span class="spinner" aria-hidden="true"></span>
          <p class="small">
            {#if sessionPending}
              Подтверждаем вход — показания корпуса начнём читать сразу после.
            {:else}
              Считываем показания корпуса: состав, находки, прогоны оценки и журнал последних действий.
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
          <p class="small">{fatal} Проверьте соединение и повторите запрос.</p>
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
          <span class="failed-line">
            Разделы, зависящие от этих показаний, остались пустыми — приблизительных чисел вместо них нет.
          </span>
        </Notice>
      {/if}

      <!-- ── Состав корпуса ─────────────────────────────────────────── -->
      <section class="dash__block dash__block--first">
        <SectionHead
          level="2"
          class="dash__band-head"
          eyebrow={narrow ? '' : 'состав корпуса'}
          title="Документы, утверждения и доказательства"
        />

        <Panel>
          <!-- Шесть равных величин — строки одного списка, а не плитки:
               число монопространственным рядом справа, подпись и знаменатель
               вторым планом слева. Подложки у строк нет — карточкой уже является
               панель. -->
          <dl class="dash__metrics">
            {#each [
                { value: num(dash.documents), label: 'документов', note: 'единица: экземпляр файла в корпусе' },
                {
                  value: num(dash.claims),
                  label: 'извлечённых утверждений',
                  note: 'свёрнутые фрагменты документов; это не находки указателя',
                },
                { value: num(dash.entities), label: 'сущностей', note: 'узлы карты связей' },
                {
                  value: num(dash.evidence),
                  label: 'доказательств',
                  note: 'ссылки на фрагменты документов в открытых аккаунту находках',
                },
                {
                  value: num(dash.conflicts),
                  label: 'оспариваемых',
                  note: 'утверждений, где источники спорят о величине',
                },
                {
                  value: num(dash.gaps),
                  label: 'пробелов в выборке',
                  note:
                    dash.gaps_omitted > 0
                      ? `показано ${num(dash.gaps)}, ещё ${num(dash.gaps_omitted)} — за пределами выборки`
                      : 'список полон для текущей выборки',
                },
              ] as metric (metric.label)}
              <div class="dash__metric">
                <dt class="stack dash__metric-term">
                  <span class="small">{metric.label}</span>
                  <span class="micro muted">{metric.note}</span>
                </dt>
                <dd class="metric__num num dash__metric-value">{metric.value}</dd>
              </div>
            {/each}
          </dl>

          {#if dash.gaps_omitted > 0}
            <p class="micro muted dash__legend">
              Непокрытых комбинаций всего <span class="num">{num(gapsTotal)}</span>: в счётчик выборки попало
              <span class="num">{num(dash.gaps)}</span>, ещё
              <span class="num">{num(dash.gaps_omitted)}</span> осталось за её пределами. Поэтому на панели нет
              ни одного названия пробела — только две честные величины: сколько показано и сколько не попало
              в выборку.
            </p>
          {/if}
        </Panel>

        <!-- Знаменатели величин — справочный слой под лентой показаний. -->
        <div class="acc">
          <button
            type="button"
            class="acc__head"
            aria-expanded={bandOpen}
            aria-controls="dash-band-body"
            onclick={() => (bandOpen = !bandOpen)}
          >
            <span>Как читать эти величины</span>
            <Icon name="plus" size={16} class="acc__icon" />
          </button>
          {#if bandOpen}
            <div class="acc__body" id="dash-band-body">
              <p>
                Живые показания того же рабочего контура, что и остальные экраны: пустое поле
                означает пустой участок корпуса, а не скрытые данные.
              </p>
              <p>
                У каждой величины свой знаменатель: документы, утверждения, сущности и
                доказательства не выстраиваются на одну ось, потому что измеряют разные вещи.
              </p>
            </div>
          {/if}
        </div>

        {#if corpusEmpty}
          <Empty
            icon="layers"
            title="В корпусе нет ни документов, ни утверждений"
            body="Загрузите документы в рабочем пространстве — показания панели обновятся, как только разбор завершится."
          >
            {#snippet action()}
              <div class="row">
                <Button variant="action" href="/research">Открыть рабочее пространство</Button>
                <Button variant="quiet" icon="refresh" onclick={() => void load()}>Прочитать снова</Button>
              </div>
            {/snippet}
          </Empty>
        {/if}
      </section>

      <!-- ── Знаменатели покрытия ───────────────────────────────────── -->
      <section class="dash__block">
        <SectionHead
          level="2"
          eyebrow="доли измерены на фактических показаниях"
          title="Покрытие и его знаменатели"
          lead="Полоса рисуется только там, где получен и числитель, и знаменатель. Не получен знаменатель — не рисуется полоса."
        />

        <div class="stack dash__ratios">
          {#if stats}
            <Panel>
              {#if stats.documents > 0}
                <div class="row row--between">
                  <p class="small">Документы с семантическим разбором</p>
                  <span class="tag num">{share(stats.semantic_documents, stats.documents)} %</span>
                </div>
                <div class="bar dash__bar">
                  <span
                    class="bar__fill"
                    style="width: {share(stats.semantic_documents, stats.documents)}%;"></span>
                </div>
              {:else}
                <!-- Знаменателя нет — полосы нет: 0 % без деления выглядело бы измерением. -->
                <div class="row row--between">
                  <p class="small">Документы с семантическим разбором</p>
                  <span class="micro muted">полоса не рисуется: документов в корпусе нет</span>
                </div>
              {/if}
              <p class="micro muted">
                <span class="num">{num(stats.semantic_documents)}</span> из
                <span class="num">{num(stats.documents)}</span> документов. Фрагментов разметки:
                <span class="num">{num(stats.chunks)}</span> — к числу документов в одну полосу
                не приводятся.
              </p>
            </Panel>
          {:else}
            <Notice tone="warn" title="Знаменатель «документы» не получен">
              Показаний корпуса нет, поэтому полоса семантического покрытия не рисуется.
            </Notice>
          {/if}

          {#if findings}
            <Panel>
              <p class="eyebrow"><Icon name="target" size={16} /> по находкам, открытым текущему аккаунту</p>
              {#if sample.length > 0}
                {#each [
                  {
                    key: 'evidence',
                    label: 'Утверждения с доказательствами',
                    note: 'у утверждения есть ссылка на страницу, лист или диапазон ячеек',
                    part: withEvidence,
                    tone: '',
                  },
                  {
                    key: 'numbers',
                    label: 'Утверждения с нормализованными числами',
                    note: 'в утверждении найдено число с условием применения',
                    part: withNumbers,
                    tone: '',
                  },
                  {
                    key: 'disputed',
                    label: 'Оспоренные утверждения',
                    note: 'источники расходятся в величине',
                    part: disputed,
                    tone: 'bar__fill--disputed',
                  },
                ] as ratio (ratio.key)}
                  <div class="dash__ratio">
                    <div class="row row--between">
                      <p class="small">{ratio.label}</p>
                      <span class="tag num">{share(ratio.part, sample.length)} %</span>
                    </div>
                    <div class="bar">
                      <span
                        class="bar__fill {ratio.tone}"
                        style="width: {share(ratio.part, sample.length)}%;"></span>
                    </div>
                    <p class="micro muted">
                      <span class="num">{num(ratio.part)}</span> из
                      <span class="num">{num(sample.length)}</span> · {ratio.note}
                    </p>
                  </div>
                {/each}
                <p class="micro muted dash__legend">
                  Знаменатель этих полос — {countOf(sample.length, 'находка', 'находки', 'находок')}, открытых
                  текущему аккаунту.
                  {#if !canRestricted}
                    Находки закрытого класса в знаменатель не попали, поэтому полоса короче, чем весь корпус.
                  {:else}
                    Находки закрытого класса открыты и учтены наравне с остальными.
                  {/if}
                </p>
              {:else}
                <!-- Правило экрана: не получен знаменатель — не рисуется полоса.
                     Нулевая выборка — тот же случай, что и отсутствующий ответ. -->
                <p class="micro muted dash__legend--tight">
                  Находок в срезе нет: знаменатель равен нулю, поэтому доли покрытия не рисуются.
                  Ноль делить нечем, а полоса «0 %» выглядела бы как измеренный результат.
                </p>
              {/if}
            </Panel>
          {:else}
            <Notice tone="warn" title="Знаменатель «находки» не получен">
              Находок корпуса нет: доли покрытия доказательствами, числами и оспариванием не посчитаны
              и оценками не подменены.
            </Notice>
          {/if}
        </div>
      </section>

      <!-- ── Проходы агентов ────────────────────────────────────────── -->
      <section class="dash__block">
        <SectionHead
          level="2"
          eyebrow="работа агента над запросами"
          title="Проходы агентов"
          lead="Счётчики ведёт процесс сервиса: в них попадают все вызовы агентов с его запуска, а не только ваши запросы. Задержки считаются по последним 1000 вызовам каждого шага. Снимок датирован {dateTime(dash.agent_metrics.generated_at)}."
        />

        {#if agents.length}
          <Panel>
            {#each agents as metric (metric.agent)}
              {@const agent = agentLabel(metric.agent)}
              <div class="dash__ratio">
                <div class="row row--between">
                  <p class="small">
                    {agent.name}
                    {#if agent.named}<code class="tech">{metric.agent}</code>{/if}
                  </p>
                  <span class="tag num">{share(metric.successes, metric.calls)} %</span>
                </div>
                <div class="bar">
                  <span
                    class="bar__fill bar__fill--consensus"
                    style="width: {share(metric.successes, metric.calls)}%;"></span>
                </div>
                <p class="micro muted">
                  завершено успешно <span class="num">{num(metric.successes)}</span> вызовов из
                  <span class="num">{num(metric.calls)}</span> · сбоев
                  <span class="num">{num(metric.failures)}</span> · повторов
                  <span class="num">{num(metric.retries)}</span> · обычный шаг задержки
                  <span class="num">{num(Math.round(metric.p50_duration_ms))}</span> мс · реже
                  дольше <span class="num">{num(Math.round(metric.p95_duration_ms))}</span> мс
                </p>
              </div>
            {/each}
            <p class="micro muted dash__legend">
              Успехом считается исход последней попытки шага, повторы модели стоят отдельным
              числом. Токенов у модели с запуска процесса:
              <span class="num">{num(dash.agent_metrics.total_prompt_tokens)}</span> в запросах и
              <span class="num">{num(dash.agent_metrics.total_completion_tokens)}</span> в ответах.
              Все числа — общие для сервиса: разложения по аккаунтам сервер не отдаёт.
            </p>
          </Panel>
        {:else}
          <Empty
            icon="ask"
            title="Ни одного завершённого прохода агента"
            body="Показания накапливаются с первого запроса. Пустая полоса честнее нуля «из коробки»."
          >
            {#snippet action()}
              <Button variant="action" href="/research">Выполнить запрос</Button>
            {/snippet}
          </Empty>
        {/if}

        {#if llm.length}
          <!-- Русского имени структуры ответа в словаре нет: служебное имя идёт
               моно-подписью при строке и не становится заголовком сам по себе.
               `use:scrollRegion` даёт полосе прокрутки точку фокуса только когда
               прокручивать действительно есть что. -->
          <div
            class="table-wrap dash__table"
            role="region"
            aria-label="Вызовы модели по структуре ответа"
            use:scrollRegion
          >
            <table class="table">
              <caption class="dash__caption">
                Вызовы модели по структуре ответа. Служебное имя структуры приведено только для
                сверки с журналом сервиса: разделом продукта оно не является.
              </caption>
              <thead>
                <tr>
                  <th scope="col">структура ответа</th>
                  <th scope="col" class="n">вызовов</th>
                  <th scope="col" class="n">сбоев</th>
                  <th scope="col" class="n">повторов</th>
                  <th scope="col" class="n">средняя задержка, мс</th>
                  <th scope="col" class="n">редко дольше, мс</th>
                </tr>
              </thead>
              <tbody>
                {#each llm as snap (snap.schema_name)}
                  <tr>
                    <th scope="row" class="dash__rowhead">
                      <span class="small">структура вне словаря</span>
                      <code class="tech">{snap.schema_name}</code>
                    </th>
                    <td class="n">{num(snap.calls)}</td>
                    <td class="n">{num(snap.failures)}</td>
                    <td class="n">{num(snap.retries)}</td>
                    <td class="n">{num(Math.round(snap.average_duration_ms))}</td>
                    <td class="n">{num(Math.round(snap.p95_duration_ms))}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
          <p class="micro muted dash__legend--tight">
            «Повторов» — ответы модели, не прошедшие проверку структуры с первого раза;
            «редко дольше» — значение, превышаемое лишь в 5 вызовах из 100.
          </p>
        {/if}
      </section>

      <!-- ── Журнал последних действий ──────────────────────────────── -->
      <section class="dash__block">
        <SectionHead
          level="2"
          eyebrow={canAudit ? 'журнал контура' : 'ваши последние акты'}
          title={canAudit ? 'Последние действия рабочего контура' : 'Ваши последние акты'}
          lead={canAudit
            ? 'Право читать журнал выдано: показаны десять последних актов всех аккаунтов, столбец «аккаунт» — идентификатор автора. Срез не полный: более старые записи в него не подтягиваются.'
            : 'Показаны десять последних актов вашего аккаунта: панель запрашивает журнал с серверной границей по аккаунту, чужие строки до этого экрана не доезжают. Срез не полный: более старые записи в него не подтягиваются.'}
        />

        {#if activity.length}
          <div class="table-wrap dash__table">
            <table class="table">
              <thead>
                <tr>
                  <th scope="col">время</th>
                  {#if canAudit}
                    <th scope="col">аккаунт</th>
                  {/if}
                  <th scope="col">действие и объект</th>
                  <th scope="col">итог</th>
                </tr>
              </thead>
              <tbody>
                {#each activity as entry, i (i)}
                  <tr>
                    <td><time datetime={entry.created_at}>{dateTime(entry.created_at)}</time></td>
                    {#if canAudit}
                      <td>
                        <!-- Полный идентификатор автора: сокращение с подсказкой на
                             hover оставило бы уникальную информацию вне клавиатуры. -->
                        <code class="tech">{entry.actor_id}</code>
                      </td>
                    {/if}
                    <td>
                      <!-- Русское имя действия — заголовок строки, id объекта —
                           моно-подпись при нём. -->
                      <p class="small">{actionTerm(entry.action)}</p>
                      {#if entry.object_id}
                        <p class="micro muted">
                          объект <code class="tech">{entry.object_id}</code>
                        </p>
                      {:else}
                        <p class="micro muted">объект не указан</p>
                      {/if}
                    </td>
                    <td>{outcomeLabel(entry.outcome)}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
          <p class="micro muted dash__legend">
            В срезе <span class="num">{num(activity.length)}</span>
            {canAudit ? 'последних актов контура.' : 'последних актов этого аккаунта.'}
          </p>
        {:else}
          <Empty
            icon="clock"
            title={canAudit ? 'Актов в журнале контура пока нет' : 'Ваших актов в журнале пока нет'}
            body="Загрузите документ или выполните запрос — запись появится здесь после следующего чтения панели."
          >
            {#snippet action()}
              <Button variant="action" href="/research">Начать с запроса</Button>
            {/snippet}
          </Empty>
        {/if}
      </section>

      <!-- ── Прогоны оценки ─────────────────────────────────────────── -->
      <section class="dash__block">
        <SectionHead
          level="2"
          eyebrow="самооценка качества ответов"
          title="Прогоны оценки"
          lead="Метрики показываются только у прогонов, которые действительно выполнены: покрытие цитатами, поддержка числами, доля выводов без поддержки и самооценка модели."
        />

        {#if !canEvaluate}
          <Panel tone="lav">
            <p class="eyebrow"><Icon name="shield" size={16} /> расширенный доступ</p>
            <p class="small">
              Метрики качества ответов доступны при расширенном доступе. Запрос и находки работают как
              обычно, а процентов «качества» здесь нет — не потому что они нулевые, а потому что их
              не показывают.
            </p>
          </Panel>
        {:else if runs === null}
          <Panel tone="coral">
            <div class="dash__fault">
              <p class="eyebrow"><Icon name="alert" size={16} /> прогоны не получены</p>
              <p class="small">Не удалось прочитать журнал прогонов. Проверьте соединение и повторите запрос.</p>
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
            title="Оценка ещё не считалась"
            body="Прогон создаётся на ответе исследовательского запроса. Пока его нет, на панели нет и процентов «качества»: метрика без замера, который её породил, здесь не рисуется."
          >
            {#snippet action()}
              <Button variant="action" href="/research">Выполнить запрос</Button>
            {/snippet}
          </Empty>
        {:else}
          <div class="table-wrap dash__table">
            <table class="table">
              <caption class="dash__caption">протоколы качества · по фактически выполненным прогонам</caption>
              <thead>
                <tr>
                  <th scope="col">когда</th>
                  <th scope="col">запрос</th>
                  {#each METRICS as metric (metric.key)}
                    <th scope="col" class="n">{metric.label}</th>
                  {/each}
                  <th scope="col">вердикт</th>
                </tr>
              </thead>
              <tbody>
                {#each runs as run (run.id)}
                  <tr>
                    <td><time datetime={run.created_at}>{dateTime(run.created_at)}</time></td>
                    <td><code class="code">{run.query_id}</code></td>
                    {#each METRICS as metric (metric.key)}
                      <td class="n">{pct(run.metrics[metric.key])}</td>
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
          <div class="stack dash__footnotes">
            <p class="micro muted">
              Доли измерены на конкретном ответе. Выводов без поддержки — метрика «меньше лучше»: доля
              выводов без трассировки до источника; средняя уверенность выводов — само-оценка модели, а не
              подтверждённость факта.
            </p>
          </div>
        {/if}
      </section>
    {/if}
  </div>
</div>

<style>
  /* Экран живёт в полноэкранной рабочей оболочке: высоту задаёт .page,
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

  /* Первый блок — лента показаний: он идёт сразу за шапкой экрана. */
  .dash__block--first {
    margin-top: 0;
  }

  .dash__block--first > .acc {
    margin-top: var(--s4);
  }

  /* Заголовок ленты плотнее разделов ниже: знаменатель экрана — числа. */
  .dash__band-head :global(.h2) {
    font-size: var(--t-h3);
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

  /* Лента показаний — шесть строк списка: величина читается числом в
     монопространственном ряду справа, подпись и знаменатель — слева вторым
     планом. Строки разделены линией, подложек нет: карточкой уже является
     панель. */
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

  /* Числа ленты читаются с первого взгляда, подпись и знаменатель — вторым планом. */
  .dash__metrics .metric__num {
    font-size: var(--t-h3);
  }

  .dash__legend {
    max-width: 88ch;
    margin-top: var(--s4);
  }

  /* Заголовок строки таблицы: человекочитаемая строка впереди, id — подписью
     под ней. */
  .dash__rowhead {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
  }

  .dash__rowhead .small {
    color: var(--ink);
  }

  .dash__legend--tight {
    max-width: 88ch;
    margin-top: var(--s3);
  }

  .dash__ratios {
    --gap: var(--s5);
  }

  .dash__bar {
    margin-block: var(--s2);
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

  .dash__table {
    margin-top: var(--s5);
  }

  .dash__caption {
    padding: var(--s3) var(--s4);
    text-align: left;
    font-size: var(--t-micro);
    line-height: var(--lh-dense);
    color: var(--ink-3);
    background: var(--surface-raised);
  }

  .dash__footnotes {
    --gap: var(--s3);
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
    /* Шапка экрана на телефоне: заголовок и действие в одну строку, лента
       показаний начинается сразу под ними. */
    .dash__inner {
      gap: var(--s3);
    }

    .dash__inner :global(.section-head) {
      align-items: center;
      gap: var(--s3);
    }

    .dash__inner :global(.section-head__text) {
      min-width: 0;
      flex: 1 1 auto;
    }

    .dash__inner :global(.section-head) > :global(.btn) {
      flex: none;
    }

    .dash__inner :global(.section-head.dash__band-head) {
      margin-bottom: var(--s3);
    }

    .dash__band-head :global(.h2) {
      font-size: var(--t-h4);
    }

    /* Телефон: строка остаётся строкой, но число крупнее — подпись со
       знаменателем переносится в две строки и не спорит с величиной. */
    .dash__metric {
      column-gap: var(--s4);
      padding-block: var(--s2);
    }

    .dash__metrics .metric__num {
      font-size: var(--t-h2);
    }

    .dash__block {
      margin-top: var(--s6);
    }
  }
</style>
