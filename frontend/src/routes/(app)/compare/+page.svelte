<script lang="ts">
  // Сравнение технологий по измерениям: отметка сущностей, таблица и главное
  // действие читаются с первого экрана, списки корпуса живут в правом рельсе (на
  // узком экране они свёрнуты под результатом). Отметить технологию или измерение
  // можно единственным способом: нажатием по списку корпуса, поэтому в условиях
  // сравнения остаётся итог выбора, а не второй и третий параллельный ввод. Ниже
  // 900px сравнение разворачивается в стек заметок на технологию, а не сжимается
  // в горизонтальный скролл: скроллится таблица внутри своей области.
  import { onMount, tick } from 'svelte';
  import { page } from '$app/state';
  import { api, ApiError } from '$lib/api';
  import { countOf, pct, plural } from '$lib/format';
  import { navLabel } from '$lib/nav';
  import { scrollRegion } from '$lib/scroll-region';
  import { session } from '$lib/sessionStore.svelte';
  import {
    COMPARE_CELL_LABELS,
    COMPARE_LEGEND,
    COMPARE_LIMIT_SOURCE,
    COMPARE_ROW_LABELS,
    OPERATOR_WORD,
    PROPERTY_LABELS,
    SUBJECT_LABELS,
    limitTagText,
    outsideLimitText,
    serviceKeysOf,
    termLabelsOf,
  } from '$lib/terms';
  import Button from '$lib/ui/Button.svelte';
  import Chip from '$lib/ui/Chip.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Field from '$lib/ui/Field.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';
  import type {
    ComparisonCell,
    ComparisonRow,
    ComparisonTable,
    FindingListItem,
    NumericObservation,
  } from '$lib/types';

  // Предел корпуса: направление, величина, единица и где он взят: из оператора
  // наблюдения (`operator`) или из его формулировки (`wording`).
  type Limit = { kind: 'lte' | 'gte'; at: number; unit: string; from: 'operator' | 'wording' };
  type Check = 'outside' | 'within' | 'none';

  // Находки корпуса читаются окном: список выбора растёт вместе с корпусом, и
  // экран обязан называть, сколько находок показано и сколько их всего.
  const CORPUS_PAGE_SIZE = 200;

  // Право проверяет сервер (`export:run`); клиентская сверка нужна, чтобы не
  // слать заведомо отклоняемый вызов, а отказ по доступу показывается тем же
  // человеческим текстом.
  const allowed = $derived(
    session.state === 'unknown' ? null : session.can('export:run'),
  );

  let question = $state('');
  // Вопрос сравнения необязателен и ни на какие числа не влияет: это подпись к
  // результату, а не пропуск на сравнение. Серверному контракту нужен ориентир
  // прогона (от 3 символов), поэтому при пустом поле уходит подпись по отмеченным
  // технологиям, а на экран выводится только вопрос человека (`questionEcho`).
  let questionEcho = $state('');
  let entities = $state<string[]>([]);
  let dimensions = $state<string[]>([]);
  let matrix = $state<ComparisonTable | null>(null);
  let corpus = $state<FindingListItem[] | null>(null);
  let corpusTotal = $state<number | null>(null);
  let corpusOffset = $state(0);
  let corpusMoreLoading = $state(false);
  let corpusMoreError = $state('');
  let corpusSeq = 0;
  let corpusError = $state('');
  let loading = $state(false);
  let attempted = $state(false);
  let error = $state('');
  let forbidden = $state('');
  let needsEntities = $state(false);

  // Мобильная композиция сравнения: стек заметок на технологию вместо таблицы.
  // Точка перестройки 900px: ниже него и рельс уходит под условия, и матрица
  // разворачивается в заметки, поэтому страница не идёт горизонтальным скроллом
  // ни на 781 px, ни на 375 px.
  let narrow = $state(false);
  // Списки корпуса и пояснение чтения — вторичный слой: на широком экране они
  // в правом рельсе, на узком свёрнуты, чтобы условия и результат читались сразу.
  let corporaOpen = $state(false);
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

  const sample = $derived(corpus ?? []);

  // Имена ключей корпуса для интерфейса: русское имя словаря, иначе
  // человекочитаемая строка сервера, иначе заглушка. Сырое имя онтологии в
  // заголовок столбца не встаёт и живёт под «Служебными данными».
  const subjectKeys = $derived(
    [...new Set(sample.map((f) => f.subject).filter((name): name is string => Boolean(name)))],
  );
  const propertyKeys = $derived([
    ...new Set(sample.flatMap((f) => f.observations.map((o) => o.property_name))),
  ]);
  const subjectLabels = $derived(termLabelsOf(subjectKeys, SUBJECT_LABELS, 'subject'));
  const propertyLabels = $derived(termLabelsOf(propertyKeys, PROPERTY_LABELS, 'property'));
  const subjectServiceKeys = $derived(
    serviceKeysOf(subjectKeys, SUBJECT_LABELS, 'subject'),
  );
  const propertyServiceKeys = $derived(
    serviceKeysOf(propertyKeys, PROPERTY_LABELS, 'property'),
  );
  const serviceKeys = $derived([...subjectServiceKeys, ...propertyServiceKeys]);

  const labelOf = (labels: Record<string, string>, name: string): string => labels[name] ?? name;

  const subjectNames = $derived(
    [...subjectKeys].sort((a, b) =>
      labelOf(subjectLabels, a).localeCompare(labelOf(subjectLabels, b), 'ru'),
    ),
  );
  const propertyNames = $derived(
    [...propertyKeys].sort((a, b) =>
      labelOf(propertyLabels, a).localeCompare(labelOf(propertyLabels, b), 'ru'),
    ),
  );

  function entityLabel(name: string): string {
    return labelOf(subjectLabels, name);
  }

  function dimensionLabel(name: string): string {
    return labelOf(propertyLabels, name);
  }

  // Знак направления серверного значения становится словом: «≥92.5» превращается
  // в «не меньше 92,5» вместе с десятичной запятой, как на остальных экранах.
  const SIGN_WORD: Record<string, string> = {
    '<=': OPERATOR_WORD.lte,
    '>=': OPERATOR_WORD.gte,
    '<': OPERATOR_WORD.lt,
    '>': OPERATOR_WORD.gt,
    '≤': OPERATOR_WORD.lte,
    '≥': OPERATOR_WORD.gte,
    '=': OPERATOR_WORD.eq,
  };

  function serverValue(text: string): string {
    return text
      .replace(/(<=|>=|[≤≥<>])\s*(?=\d)/g, (sign) => `${SIGN_WORD[sign.trim()] ?? sign} `)
      .replace(/(\d)\.(\d)/g, '$1,$2');
  }

  const limits = $derived.by<Record<string, Limit>>(() => {
    const map: Record<string, Limit> = {};
    for (const finding of sample) {
      for (const observation of finding.observations) {
        const bound = boundOf(observation);
        if (!bound) continue;
        const held = map[bound.property];
        // У свойства бывает несколько распознанных пределов: сверка идёт по
        // самому строгому — верхней границей («≤») берём меньшее число, нижней
        // («≥») большее. Разнонаправленные пределы («от — до») одним числом не
        // выразить, поэтому при разных направлениях остаётся первый распознанный.
        if (
          !held ||
          (held.kind === bound.kind &&
            (bound.kind === 'lte' ? bound.at < held.at : bound.at > held.at))
        ) {
          map[bound.property] = bound;
        }
      }
    }
    return map;
  });
  const declaredLimits = $derived(Object.entries(limits));

  // Строка получает «сверки выдержаны» только когда хотя бы одна сверка прошла
  // (within): «none» — предела нет, единицы не совпали или число не разобралось —
  // сверенным не считается, и без единой сверки строка честно помечена
  // «нечего сверять», а не зелёным выводом о соответствии.
  function rowAssessment(
    row: ComparisonRow,
    headers: string[],
  ): { pill: 'consensus' | 'disputed' | 'off'; label: string; why: string } {
    let checked = 0;
    for (const header of headers) {
      const result = checkOf(header, row.cells[header]);
      if (result.check === 'outside') {
        return { pill: 'disputed', label: COMPARE_ROW_LABELS.outside, why: result.why };
      }
      if (result.check === 'within') checked += 1;
    }
    return checked > 0
      ? { pill: 'consensus', label: COMPARE_ROW_LABELS.passed, why: '' }
      : { pill: 'off', label: COMPARE_ROW_LABELS.nothing, why: '' };
  }

  const resultStatus = $derived.by(() => {
    if (forbidden) return 'Сравнение закрыто: нужен доступ к корпусу.';
    if (error) return 'Сравнение не выполнено.';
    if (loading) return 'Считаем сравнение…';
    if (!matrix) return 'Сравнения пока нет. Отметьте технологии в списках корпуса.';
    if (matrix.headers.length === 0) return 'Указанных измерений в находках нет.';
    if (matrix.rows.length === 0) return 'Таких технологий в находках нет.';
    return `Готово: ${countOf(
      matrix.rows.length,
      'технология',
      'технологии',
      'технологий',
    )} и ${countOf(matrix.headers.length, 'измерение', 'измерения', 'измерений')}.`;
  });

  // Направление предела берётся из оператора наблюдения, а когда оператора нет —
  // из формулировки самого наблюдения (`≤`, «не выше»). Это распознавание, а не
  // структурированное поле корпуса, и экран обязан называть его распознанным.
  function boundOf(observation: NumericObservation): ({ property: string } & Limit) | null {
    const at = observation.normalized_value ?? observation.value;
    if (at == null) return null;
    const property = observation.property_name;
    const unit = observation.normalized_unit || observation.unit || '';
    if (observation.operator === 'lte' || observation.operator === 'lt') {
      return { property, kind: 'lte', at, unit, from: 'operator' };
    }
    if (observation.operator === 'gte' || observation.operator === 'gt') {
      return { property, kind: 'gte', at, unit, from: 'operator' };
    }
    const raw = observation.raw_text;
    if (/[≤<]/.test(raw) || /не (выше|более)/.test(raw)) {
      return { property, kind: 'lte', at, unit, from: 'wording' };
    }
    if (/[≥>]/.test(raw) || /выше|не (ниже|менее)/.test(raw)) {
      return { property, kind: 'gte', at, unit, from: 'wording' };
    }
    return null;
  }

  /** Числа из значения ячейки: «92,5», «≥ 95» и диапазоны вида «85–92». */
  function numbersOf(text: string): number[] {
    return [...text.matchAll(/-?\d+(?:[.,]\d+)?/g)].map((m) => Number(m[0].replace(',', '.')));
  }

  /**
   * Результат сверки ячейки: `outside` — значение за распознанным пределом того
   * же свойства и единицы, `within` — предел есть и выдержан, `none` — сверять
   * не с чем (значения нет, предела нет или единицы не совпали).
   */
  function checkOf(header: string, cell: ComparisonCell | undefined): { check: Check; why: string } {
    const limit: Limit | undefined = limits[header];
    if (!cell?.value || !limit || (cell.unit ?? '') !== limit.unit) return { check: 'none', why: '' };
    const numbers = numbersOf(cell.value);
    if (!numbers.length) return { check: 'none', why: '' };
    const property = dimensionLabel(header);
    if (limit.kind === 'lte' && Math.max(...numbers) > limit.at) {
      return {
        check: 'outside',
        why: outsideLimitText(property, Math.max(...numbers), limit.unit, 'lte', limit.at),
      };
    }
    if (limit.kind === 'gte' && Math.min(...numbers) < limit.at) {
      return {
        check: 'outside',
        why: outsideLimitText(property, Math.min(...numbers), limit.unit, 'gte', limit.at),
      };
    }
    return { check: 'within', why: '' };
  }

  /**
   * Статус ячейки выводится только из того, что есть в данных: значения нет —
   * «наблюдения нет»; предел распознан и нарушен — «вне предела»; сверка прошла
   * и предел выдержан — «в пределе»; сверка не состоялась (предела нет, единицы
   * не совпали, число не разобралось) — «не сверено», а не зелёное «в пределе».
   */
  function cellState(
    header: string,
    cell: ComparisonCell | undefined,
  ): { pill: 'consensus' | 'hypothesis' | 'disputed' | 'off'; label: string } {
    if (!cell?.value) return { pill: 'off', label: COMPARE_CELL_LABELS.novalue };
    if (!limits[header]) return { pill: 'hypothesis', label: COMPARE_CELL_LABELS.unchecked };
    const result = checkOf(header, cell);
    if (result.check === 'outside') return { pill: 'disputed', label: COMPARE_CELL_LABELS.outside };
    if (result.check === 'within') return { pill: 'consensus', label: COMPARE_CELL_LABELS.within };
    return { pill: 'hypothesis', label: COMPARE_CELL_LABELS.unchecked };
  }

  // Отметка ставится и снимается одним движением: нажатием по списку корпуса.
  function toggleEntity(name: string): void {
    entities = entities.includes(name) ? entities.filter((v) => v !== name) : [...entities, name];
    needsEntities = false;
  }

  function toggleDimension(name: string): void {
    dimensions = dimensions.includes(name) ? dimensions.filter((v) => v !== name) : [...dimensions, name];
  }

  // Находки корпуса приходят окном, поэтому полное число берётся из ответа
  // сервера: молчаливого среза в списках выбора нет.
  async function loadCorpus(): Promise<void> {
    corpusSeq += 1;
    try {
      const window = await api.findings(undefined, undefined, CORPUS_PAGE_SIZE, 0);
      corpus = window.items;
      corpusTotal = window.total;
      corpusOffset = window.items.length;
      corpusMoreError = '';
      corpusError = '';
    } catch (reason) {
      corpus = null;
      corpusTotal = null;
      corpusOffset = 0;
      // Отказ по праву и технический сбой различаются словами и действием.
      corpusError =
        reason instanceof ApiError && reason.status === 403
          ? 'Находки корпуса этому аккаунту не открыты. Право выдаёт администратор сервиса.'
          : 'Списки корпуса не загрузились. Проверьте соединение и повторите.';
    }
  }

  async function loadCorpusMore(): Promise<void> {
    const call = ++corpusSeq;
    corpusMoreLoading = true;
    corpusMoreError = '';
    try {
      const window = await api.findings(undefined, undefined, CORPUS_PAGE_SIZE, corpusOffset);
      if (call !== corpusSeq) return;
      const seen = new Set((corpus ?? []).map((finding) => finding.id));
      corpus = [...(corpus ?? []), ...window.items.filter((finding) => !seen.has(finding.id))];
      corpusOffset += window.items.length;
      if (window.total !== null) corpusTotal = window.total;
      // Страница пустая: дальше показывать нечего, кнопка осталась бы пустым
      // действием.
      if (window.items.length === 0) corpusTotal = corpus.length;
    } catch {
      if (call === corpusSeq) corpusMoreError = 'Следующие находки не добавились. Повторите.';
    } finally {
      if (call === corpusSeq) corpusMoreLoading = false;
    }
  }

  async function runCompare(): Promise<void> {
    if (entities.length === 0) {
      // Без технологий сравнивать нечего: пустая таблица выглядела бы как «данных
      // нет», поэтому называем пропущенный выбор и не идём на сервер.
      needsEntities = true;
      error = '';
      forbidden = '';
      return;
    }
    needsEntities = false;
    loading = true;
    attempted = true;
    error = '';
    forbidden = '';
    // Смысл поля — подпись к результату, а не пропуск: короткую строку (<3 символов
    // требует контракт) не показываем и не отправляем, сравнение идёт по отмеченным
    // технологиям.
    const asked = question.trim().length >= 3 ? question.trim() : '';
    questionEcho = asked;
    try {
      matrix = await api.compare(
        asked || `Сравнение: ${entities.map(entityLabel).join(', ')}`,
        entities,
        dimensions,
      );
    } catch (reason) {
      matrix = null;
      // Отказ по доступу и технический сбой две разные строки, и обе человеческие.
      if (reason instanceof ApiError && reason.status === 403) {
        forbidden =
          'Сравнение открыто аккаунтам с доступом к корпусу. Право выдаёт администратор сервиса.';
      } else {
        error = 'Не удалось получить сравнение. Проверьте соединение и повторите.';
      }
    } finally {
      loading = false;
    }
  }

  async function runWithoutDimensions(): Promise<void> {
    dimensions = [];
    await runCompare();
  }

  function takeSubjects(): void {
    entities = subjectNames.slice(0, 4);
    needsEntities = false;
  }

  /**
   * Способ выбрать технологию и измерение один: списки корпуса. Рельс на широком
   * экране, свёрнутый аккордеон под результатом на узком. Подсказка в условиях
   * раскрывает его и прокручивает к нему вместо второго поля ввода.
   */
  function openCorpora(): void {
    corporaOpen = true;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    void tick().then(() =>
      document
        .getElementById('cmp-corpora')
        ?.scrollIntoView({ block: 'nearest', behavior: reduced ? 'auto' : 'smooth' }),
    );
  }

  // Сколько находок показалось за списком выбора: без полного числа оговорки нет.
  const corpusLeft = $derived(
    corpusTotal === null ? 0 : Math.max(0, corpusTotal - corpusOffset),
  );

  // Вывод по сравнению записывают в отзыв на ответ (в навигации раздел
  // «Отзывы»): ссылка передаёт, о чём именно вывод, чтобы не набирать его заново.
  const feedbackHref = $derived(
    `/feedback?about=${encodeURIComponent(
      questionEcho || entities.map(entityLabel).join(', ') || 'сравнение технологий',
    )}`,
  );

  // Переход с карты или из ответа: ?q=…&entities=a,b запускает сравнение, как
  // только сервер подтвердил права сессии (порядок инициализации эффекта и
  // onMount не должен влиять на это).
  let autoRun = $state(false);

  onMount(() => {
    const params = page.url.searchParams;
    const incomingQuestion = params.get('q');
    const incomingEntities = params.get('entities');
    if (incomingQuestion) question = incomingQuestion;
    if (incomingEntities) {
      entities = incomingEntities.split(',').map((value) => value.trim()).filter(Boolean);
      autoRun = entities.length > 0;
    }
  });

  // Два независимых триггера. В общем эффекте чтение и запись `autoRun`
  // встречались в одном теле: записанное значение перезапускало эффект, и на
  // каждый переход по ссылке списки корпуса читались дважды.
  $effect(() => {
    if (allowed === true) void loadCorpus();
  });

  $effect(() => {
    if (allowed !== true || !autoRun) return;
    autoRun = false;
    void runCompare();
  });
</script>

<svelte:head>
  <title>Сравнение технологий: Научный Клубок</title>
</svelte:head>

{#snippet corporaBlock()}
  <Panel tone="sunk" class="cmp__list">
    <p class="micro muted">
      Нажатие добавляет технологию или измерение в условия сравнения, повторное
      нажатие убирает.
    </p>

    <p class="field__label">
      Технологии
      <!-- Число при заголовке говорит, сколько меток в списке всего: в рельсе
           сразу видно не все. Во время загрузки счётчика нет: «0» выглядел бы как
           пустой корпус. -->
      {#if corpus}
        <span class="muted">(<span class="num">{subjectNames.length}</span>)</span>
      {/if}
    </p>
    {#if corpus}
      {#if subjectNames.length === 0}
        <!-- Пустой корпус это не «нет совпадений»: сравнивать нечего, пока в него
             не положен документ. Вводить вручную нечего, поэтому подсказка ведёт
             туда, где корпус пополняется. -->
        <p class="micro muted">
          В находках корпуса нет ни одной технологии: технология приходит вместе с
          документом.
        </p>
        <Button href="/findings" variant="quiet" size="sm">Пополнить корпус</Button>
      {:else}
        <!-- Чипы переносятся строками: в рельсе 272 px горизонтальная полоса
             показала бы один чип и срезала остальные без подсказки. -->
        <div class="cmp__chips">
          {#each subjectNames as name (name)}
            <Chip pressed={entities.includes(name)} onclick={() => toggleEntity(name)}>
              {entityLabel(name)}
            </Chip>
          {/each}
        </div>
      {/if}
    {:else if corpusError}
      <p class="micro muted">{corpusError}</p>
      <Button variant="quiet" size="sm" onclick={() => void loadCorpus()}>Обновить списки корпуса</Button>
    {:else}
      <div class="cmp__skeleton" role="status">
        <span class="skeleton cmp__skeleton-line"></span>
        <span class="skeleton cmp__skeleton-line cmp__skeleton-line--short"></span>
        <p class="micro muted">Загружаем находки корпуса…</p>
      </div>
    {/if}

    <p class="field__label">
      Измерения
      {#if corpus}
        <span class="muted">(<span class="num">{propertyNames.length}</span>)</span>
      {/if}
    </p>
    {#if corpus}
      {#if propertyNames.length === 0}
        <p class="micro muted">
          Измерений в находках нет: колонок не будет, пока в корпус не придут числа.
        </p>
      {:else}
        <div class="cmp__chips">
          {#each propertyNames as name (name)}
            <Chip pressed={dimensions.includes(name)} onclick={() => toggleDimension(name)}>
              {dimensionLabel(name)}
            </Chip>
          {/each}
        </div>
      {/if}
    {:else if corpusError}
      <p class="micro muted">Измерения не получены: обновите списки корпуса.</p>
    {:else}
      <p class="micro muted" role="status">Измерения появятся, когда загрузим находки корпуса.</p>
    {/if}

    <!-- Ось «сколько показано»: список выбора построен по окну находок, поэтому
         знаменатель назван и неполнота закрыта одной кнопкой. -->
    {#if corpus}
      <p class="micro muted">
        {#if corpusTotal !== null}
          Показано <span class="num">{countOf(corpus.length, 'находка', 'находки', 'находок')}</span> из
          <span class="num">{corpusTotal}</span>.
        {:else}
          Показано <span class="num">{countOf(corpus.length, 'находка', 'находки', 'находок')}</span>.
        {/if}
      </p>
      {#if corpusLeft > 0}
        <Button
          variant="quiet"
          size="sm"
          busy={corpusMoreLoading}
          disabled={corpusMoreLoading}
          onclick={() => void loadCorpusMore()}
        >
          {corpusMoreLoading ? 'Показываем ещё…' : 'Показать ещё'}
        </Button>
      {/if}
      {#if corpusMoreError}
        <p class="micro muted">{corpusMoreError}</p>
      {/if}
    {/if}

    <!-- Служебные имена ключей, которые экран заменил заглушкой: сырой ключ
         корпуса больше не становится заголовком столбца. -->
    {#if serviceKeys.length}
      <details class="cmp__svc">
        <summary class="micro">Служебные данные</summary>
        <ul class="cmp__svc-list">
          {#each serviceKeys as item (item.key)}
            <li><span>{item.label}</span> <span class="tech">{item.key}</span></li>
          {/each}
        </ul>
      </details>
    {/if}

    <div class="cmp__list-actions">
      <Button variant="quiet" size="sm" disabled={loading} onclick={() => void loadCorpus()}>
        Обновить списки корпуса
      </Button>
      {#if subjectNames.length}
        <Button
          variant="ghost"
          size="sm"
          disabled={loading}
          title="Отмечаем не больше четырёх технологий, остальное отмечаете сами"
          onclick={takeSubjects}
        >
          Отметить первые 4 технологии
        </Button>
      {/if}
    </div>
  </Panel>
{/snippet}

{#snippet helpBlock()}
  <div class="acc">
    <button
      type="button"
      class="acc__head"
      aria-expanded={helpOpen}
      aria-controls="cmp-help-body"
      onclick={() => (helpOpen = !helpOpen)}
    >
      <span>Как читать сравнение</span>
      <Icon name="plus" size={16} class="acc__icon" />
    </button>
    <!-- Тело остаётся в DOM и получает `hidden`: ссылка aria-controls ведёт к
         существующему элементу в обоих состояниях раскрытия. -->
    <div class="acc__body" id="cmp-help-body" hidden={!helpOpen}>
      <p>В строках таблицы технологии, в колонках измерения. В ячейке то же число, что
        в находке корпуса, с единицей и цитатой источника: расхождение источников не
        прячется за одной удачной цифрой. Уверенность модели, с которой число извлекли,
        спрятана в «Служебных данных» ячейки.
      </p>
      <p>
        Технология ищется в названии утверждения целым словом, а не фрагментом: «Fe» не
        соберёт в одну строку Fe2O3 и FeS. Если измерений не отмечено, сравниваем по всем
        измерениям выбранных технологий.
      </p>
      <p>
        Предел это ограничение из текста источника: «не выше 92,5». Мы находим его по знаку
        в наблюдении или по словам «не выше» и «не ниже» и сверяем с ним числа в таблице.
        Такое распознавание не записано в корпусе отдельным полем, поэтому у каждой метки
        предела открыто сказано, откуда он взят. Если у измерения несколько пределов,
        сверяем по самому строгому: «не выше» по меньшему числу, «не ниже» по большему.
      </p>
    </div>
  </div>
{/snippet}

{#snippet cellContent(header: string, cell: ComparisonCell | undefined)}
  {#if cell?.value}
    {@const state = cellState(header, cell)}
    <div class="cmp__cell">
      <span class="num cmp__value">{serverValue(cell.value)}</span>
      {#if cell.unit}<span class="micro muted cmp__unit">{cell.unit}</span>{/if}
      <!-- В пути чтения остаётся цветовой итог сверки словом: полоса и процент
           уверенности модели ушли в раскрытие ниже. -->
      <StatusPill status={state.pill} label={state.label} />
      {#if cell.evidence}<blockquote class="quote cmp__quote">{cell.evidence}</blockquote>{/if}
      <details class="cmp__svc">
        <summary class="micro">Служебные данные</summary>
        <p class="micro muted cmp__conf">
          уверенность извлечения <span class="num">{pct(cell.confidence)}</span>
        </p>
      </details>
    </div>
  {:else}
    <span class="small muted">{COMPARE_CELL_LABELS.novalue}</span>
  {/if}
{/snippet}

<div class="page cmp-page">
  <div class="wrap">
    <!-- Узкий экран отдаёт вертикаль условиям и результату: метку раздела
         на нём заменяет заголовок. -->
    <SectionHead
      level="1"
      eyebrow={narrow ? '' : 'Корпус, измерения, пределы'}
      title="Сравнение технологий"
      lead="Технологии стоят рядом по каждому измерению: число, единица и цитата источника в ячейке."
    />

    {#if allowed === null}
      <Panel tone="sunk">
        <div class="cmp__skeleton">
          <span class="skeleton cmp__skeleton-line"></span>
          <span class="skeleton cmp__skeleton-block"></span>
          <p class="micro muted">Уточняем доступ к сравнению.</p>
        </div>
      </Panel>
    {:else if !allowed}
      <Panel tone="lav">
        <div class="panel__head">
          <h2 class="h3">Сравнение недоступно</h2>
          <StatusPill status="off" label="нет доступа" />
        </div>
        <p class="lead small">
          Сравнение технологий открыто аккаунтам с доступом к корпусу. Право выдаёт
          администратор сервиса.
        </p>
        <!-- Переходы называются разделами из `nav.ts`: «Рабочее пространство»
             было третьим именем раздела «Вопрос». -->
        <div class="row">
          <Button href="/research" variant="quiet">{navLabel('/research')}</Button>
          <Button href="/findings" variant="ghost">{navLabel('/findings')}</Button>
        </div>
      </Panel>
    {:else}
      <div class="split cmp__work">
        <Panel tone="sunk" class="cmp__form-panel">
          <div class="cmp__form-head">
            <h2 class="h3">Условия сравнения</h2>
            <Button variant="action" busy={loading} disabled={loading} onclick={() => void runCompare()}>
              Сравнить
            </Button>
          </div>

          <div class="cmp__form">
            <div class="cmp__field">
              <p class="field__label">
                Технологии
                <span class="muted">(<span class="num">{entities.length}</span>)</span>
              </p>
              <!-- Здесь только итог выбора. Метка и есть действие: нажатие убирает
                   технологию из сравнения, отдельной кнопки-крестика нет. -->
              {#if entities.length}
                <div class="cmp__picker">
                  {#each entities as name (name)}
                    <Chip pressed onclick={() => toggleEntity(name)}>{entityLabel(name)}</Chip>
                  {/each}
                </div>
              {:else}
                <p class="small muted cmp__none">Ни одна технология не отмечена.</p>
              {/if}
              {#if needsEntities}
                <p class="field__error">
                  Нужна хотя бы одна технология. Отметьте её в списках корпуса.
                </p>
              {/if}
              <Button variant="link" size="sm" onclick={openCorpora}>
                Отметить технологии
              </Button>
            </div>

            <div class="cmp__field">
              <p class="field__label">
                Измерения
                <span class="muted">(<span class="num">{dimensions.length}</span>)</span>
                <span class="micro muted">необязательно</span>
              </p>
              {#if dimensions.length}
                <div class="cmp__picker">
                  {#each dimensions as name (name)}
                    <Chip pressed onclick={() => toggleDimension(name)}>{dimensionLabel(name)}</Chip>
                  {/each}
                </div>
                <p class="field__hint">Нажатие на метку убирает её из сравнения.</p>
              {:else}
                <p class="small muted cmp__none">
                  Без отметок сравниваем по всем измерениям выбранных технологий.
                </p>
              {/if}
            </div>

            <div class="cmp__field">
              <Field
                label="Вопрос сравнения (необязательно)"
                name="cmp-question"
                bind:value={question}
                placeholder="Например: какая технология лучше очищает воду на морозе"
                hint="Одна строка контекста к результату: на числа она не влияет."
                onenter={() => void runCompare()}
              />
            </div>
          </div>
        </Panel>

        <section class="cmp__result" aria-label="Результат сравнения">
          <div class="panel__head">
            <h2 class="h3">Результат</h2>
            <p class="micro muted">сравнение по находкам корпуса</p>
          </div>
          <p class="micro cmp__status" role="status" aria-live="polite">{resultStatus}</p>

          {#if forbidden}
            <Panel tone="coral">
              <Notice tone="error" title="Сравнение недоступно">
                {forbidden}
              </Notice>
              <div class="row">
                <Button href="/findings" variant="quiet">{navLabel('/findings')}</Button>
                <Button href="/account" variant="ghost">Что открыто моему аккаунту</Button>
              </div>
            </Panel>
          {:else if error}
            <Panel tone="coral">
              <Notice tone="error" title="Сравнение не выполнено">{error}</Notice>
              <div class="row">
                <Button variant="action" disabled={loading} onclick={() => void runCompare()}>
                  Повторить сравнение
                </Button>
                <Button variant="quiet" disabled={loading} onclick={() => void loadCorpus()}>
                  Обновить списки корпуса
                </Button>
              </div>
            </Panel>
          {:else if loading}
            <Panel tone="sunk">
              <div class="cmp__skeleton">
                <p class="micro muted">Считаем сравнение по находкам корпуса.</p>
                <span class="skeleton cmp__skeleton-line"></span>
                <span class="skeleton cmp__skeleton-block"></span>
                <span class="skeleton cmp__skeleton-line"></span>
                <span class="skeleton cmp__skeleton-line cmp__skeleton-line--short"></span>
              </div>
            </Panel>
          {:else if !matrix && !attempted}
            <Empty
              icon="compare"
              title="До первого сравнения результата нет"
              body="Отметьте технологии в списках корпуса: покажем их числа по общим измерениям, с цитатой источника в ячейке."
            >
              {#snippet action()}
                <div class="row">
                  <Button variant="quiet" size="sm" onclick={openCorpora}>Отметить технологии</Button>
                  <Button variant="ghost" size="sm" href="/graph">{navLabel('/graph')}</Button>
                </div>
              {/snippet}
            </Empty>
          {:else if matrix && matrix.headers.length === 0}
            <Empty
              icon="filter"
              title="Указанных измерений в находках нет"
              body="У отмеченных технологий этих измерений нет. Снимите отметки, и останутся все измерения."
            >
              {#snippet action()}
                <div class="row">
                  <Button variant="quiet" size="sm" onclick={() => void runWithoutDimensions()}>
                    Сравнить без отмеченных измерений
                  </Button>
                  <Button variant="ghost" size="sm" onclick={() => void loadCorpus()}>
                    Обновить списки корпуса
                  </Button>
                </div>
              {/snippet}
            </Empty>
          {:else if matrix && matrix.rows.length === 0}
            <Empty
              icon="search"
              title="Таких технологий в находках нет"
              body="Ни одно утверждение корпуса не называет их целым словом: название ищется целиком, а не по фрагменту."
            >
              {#snippet action()}
                <!-- Третьего способа выбрать технологию здесь нет: ряд быстрых кнопок
                     дублировал бы списки корпуса. Ведём к ним. -->
                <div class="row">
                  <Button variant="quiet" size="sm" onclick={openCorpora}>Отметить технологии</Button>
                  <Button variant="ghost" size="sm" href="/findings">{navLabel('/findings')}</Button>
                </div>
              {/snippet}
            </Empty>
          {:else if matrix}
            <Panel tone="sunk" class="cmp__band">
              <div class="panel__head">
                {#if questionEcho}
                  <div>
                    <p class="eyebrow"><Icon name="scale" size={16} /> вопрос сравнения</p>
                    <p class="h4">{questionEcho}</p>
                  </div>
                {/if}
                <!-- Счётчики подписаны отдельно: знаменатель у каждой свой. -->
                <p class="micro muted cmp__counts">
                  <span class="num">{matrix.rows.length}</span>
                  {plural(matrix.rows.length, 'технология', 'технологии', 'технологий')}
                </p>
                <p class="micro muted cmp__counts">
                  <span class="num">{matrix.headers.length}</span>
                  {plural(matrix.headers.length, 'измерение', 'измерения', 'измерений')}
                </p>
              </div>
              <!-- Легенда цветовых итогов над таблицей: смысл метки читается там,
                   где на неё смотрят, а не только в справке внизу. -->
              <ul class="cmp__legend">
                {#each COMPARE_LEGEND as item (item.label)}
                  <li>
                    <StatusPill status={item.pill} label={item.label} />
                    <span class="micro muted">{item.note}</span>
                  </li>
                {/each}
              </ul>

              {#if narrow}
                <div class="cmp__notes">
                  {#each matrix.rows as row (row.item)}
                    {@const assess = rowAssessment(row, matrix.headers)}
                    <article class="cmp__note">
                      <div class="row row--between">
                        <h3 class="h4">{entityLabel(row.item)}</h3>
                        <StatusPill status={assess.pill} label={assess.label} />
                      </div>
                      {#if assess.why}<p class="micro cmp__why">{assess.why}</p>{/if}
                      <dl class="cmp__note-list">
                        {#each matrix.headers as header (header)}
                          <div class="cmp__note-cell">
                            <dt>{dimensionLabel(header)}</dt>
                            <dd>{@render cellContent(header, row.cells[header])}</dd>
                          </div>
                        {/each}
                      </dl>
                    </article>
                  {/each}
                </div>
              {:else}
                <!-- Широкая матрица остаётся в своём рабочем блоке: горизонтально
                     скроллится таблица, а не страница, и скроллится она тоже
                     внутри блока, поэтому шапка и первая колонка не уплывают при
                     десятках технологий. `use:scrollRegion` ставит `tabindex`
                     только когда есть что прокручивать. -->
                <div
                  class="table-wrap cmp__matrix"
                  role="region"
                  aria-label="Таблица сравнения"
                  use:scrollRegion
                >
                  <table class="table cmp__table">
                    <caption class="cmp__caption">
                      Сравнение {countOf(matrix.rows.length, 'технология', 'технологии', 'технологий')} по
                      {countOf(matrix.headers.length, 'измерению', 'измерениям', 'измерениям')}
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col" class="cmp__corner">технология</th>
                        {#each matrix.headers as header (header)}
                          <th scope="col">{dimensionLabel(header)}</th>
                        {/each}
                      </tr>
                    </thead>
                    <tbody>
                      {#each matrix.rows as row (row.item)}
                        {@const assess = rowAssessment(row, matrix.headers)}
                        <tr>
                          <th scope="row" class="cmp__rowhead">
                            <span class="cmp__item">{entityLabel(row.item)}</span>
                            <StatusPill status={assess.pill} label={assess.label} />
                            {#if assess.why}<span class="micro cmp__why">{assess.why}</span>{/if}
                          </th>
                          {#each matrix.headers as header (header)}
                            <td>{@render cellContent(header, row.cells[header])}</td>
                          {/each}
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                </div>
              {/if}
            </Panel>

            {#if declaredLimits.length}
              <Panel tone="sage">
                <p class="micro muted">
                  Пределы, по которым сверяем значения. Направление предела мы распознаём в
                  тексте источника: отдельного поля для него в корпусе нет, поэтому источник
                  подписан при каждой метке.
                </p>
                <div class="cmp__limits">
                  {#each declaredLimits as [property, limit] (property)}
                    <!-- Откуда взят предел, открытой строкой, а не только подсказкой:
                         на тач-устройстве в `title` не посмотреть. -->
                    <div class="cmp__limit">
                      <span class="tag">{limitTagText(dimensionLabel(property), limit)}</span>
                      <span class="micro muted">{COMPARE_LIMIT_SOURCE[limit.from]}</span>
                    </div>
                  {/each}
                </div>
              </Panel>
            {:else}
              <p class="micro muted cmp__nolimit">
                Ни в одном наблюдении предел не распознан, поэтому меток соответствия здесь
                нет: строки помечены «нечего сверять», ячейки «не сверено».
              </p>
            {/if}

            <!-- У сравнения есть конец: вывод записывают в отзыв на ответ (в
                 навигации раздел «Отзывы»), иначе сопоставление остаётся
                 наблюдением без следа. -->
            {#if matrix.rows.length > 0}
              <Panel tone="lav">
                <div class="cmp__close-row">
                  <div class="stack">
                    <h3 class="h4">Вывод по этим числам</h3>
                    <p class="micro muted">
                      Запишите его в отзыве на ответ: вывод попадёт в журнал отзывов вместе
                      с решением и комментарием.
                    </p>
                  </div>
                  <Button href={feedbackHref} variant="action">Записать отзыв</Button>
                </div>
              </Panel>
            {/if}
          {/if}
        </section>

        <!-- Рельс — колонка рядом с условиями и результатом, а не блок перед
             ними: каким бы длинным ни оказался список корпуса, результат
             остаётся под условиями. Сам рельс ограничен высотой вьюпорта и
             прокручивается своей полосой (см. `.cmp__rail`). -->
        {#if !narrow}
          <aside id="cmp-corpora" class="cmp__rail" aria-label="Списки корпуса" use:scrollRegion>
            {@render corporaBlock()}
          </aside>
        {/if}
      </div>

      <!-- Узкий экран: списки корпуса — под результатом, чтобы условия и
           сравнение остались в первом вьюпорте. -->
      {#if narrow}
        <div id="cmp-corpora" class="acc cmp__corpora">
          <button
            type="button"
            class="acc__head"
            aria-expanded={corporaOpen}
            aria-controls="cmp-corpora-body"
            onclick={() => (corporaOpen = !corporaOpen)}
          >
            <span>Списки корпуса</span>
            <Icon name="plus" size={16} class="acc__icon" />
          </button>
          <!-- Тело живёт в DOM всегда и получает `hidden`: `aria-controls` не
               ведёт в никуда, когда список свёрнут. -->
          <div class="acc__body cmp__corpora-body" id="cmp-corpora-body" hidden={!corporaOpen}>
            {@render corporaBlock()}
          </div>
        </div>
      {/if}

      <!-- Пояснение чтения матрицы — справочный слой: он под результатом,
           а не над условиями. -->
      {@render helpBlock()}
    {/if}
  </div>
</div>

<style>
  .cmp-page .wrap {
    display: flex;
    flex-direction: column;
    gap: var(--s5);
  }

  /*
   * Рабочая зона: условия (строка 1) и результат (строка 2) — в главной
   * колонке, списки корпуса — в правом рельсе на обе строки. Разводка по
   * строкам и колонкам задана явно, потому что порядок в разметке — условия,
   * результат, рельс: рельс стоит рядом и не может отодвинуть результат на
   * свою полную высоту. Вторая строка — `minmax(0, 1fr)`: если списков больше,
   * чем вмещает главная колонка, излишек высоты grid делит в её пользу, а не
   * втискивает результат вниз под условия.
   */
  .cmp__work {
    /* `gap` объявлен здесь, а не наследуется: общий `.split` держит 32 px,
       а ритм этой страницы — `--s5` (24 px) и между колонками, и между
       условиями с результатом. */
    gap: var(--s5);
    grid-template-columns: minmax(0, 1fr) var(--rail-w);
    grid-template-rows: auto minmax(0, 1fr);
    align-items: start;
  }

  /*
   * Рельс — прокручиваемый region с высотой не больше вьюпорта: длинные
   * списки корпуса кончаются там, где кончается экран, и уползают в собственную
   * полосу, а не растягивают рабочую зону на тысячи пикселей. Жёлоб полосы
   * утоплен в бумагу тем же приёмом, что и у `.table-wrap`, — поэтому «списка
   * не видно целиком» различимо с первого взгляда; `use:scrollRegion` добавляет
   * к этому прокрутку с клавиатуры, но только когда прокручивать есть что.
   * Отступ по горизонтали держит рамку фокуса чипа: скролл-область обрезает по
   * границе паддинга, а `:focus-visible` уходит на 5 px за метку.
   * `position: sticky` с тем же отступом, что у `.app-body`, нужен, чтобы
   * собственная полоса рельса не уползала за нижний край экрана: без него
   * нижняя часть рельса (кнопка «Обновить списки корпуса») оказывалась под
   * вьюпортом: рельс приходилось бы крутить внутри уже прокрученной страницы —
   * два вложенных скролла там, где договорён один.
   */
  .cmp__rail {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    grid-column: 2;
    grid-row: 1 / span 2;
    align-self: start;
    position: sticky;
    top: calc(var(--topbar-h) + var(--s4));
    max-height: calc(100dvh - var(--topbar-h) - var(--s6));
    padding-inline: var(--s2);
    overflow: auto;
    scroll-padding: var(--s2);
    scrollbar-width: thin;
    scrollbar-color: var(--ink-4) var(--surface-sunk);
  }

  .cmp__rail::-webkit-scrollbar-track {
    background: var(--surface-sunk);
  }

  .cmp__form {
    display: grid;
    gap: var(--s5);
    grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr));
  }

  .cmp__field {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    min-width: 0;
  }

  /* Панель условий — рабочая строка: заголовок и главное действие в одном
     плотном ряду, без отдельного подвала кнопок. */
  .cmp__form-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s4);
    flex-wrap: wrap;
    margin-bottom: var(--s4);
  }

  /* Итог выбора в условиях сравнения: пустой отбор называется словами, а не
     пустой коробкой с полем ввода — способа ввести вручную больше нет. */
  .cmp__none {
    margin: 0;
  }

  /* Закрывающий блок результата: заголовок, одна строка смысла и действие. */
  .cmp__close-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s4);
    flex-wrap: wrap;
  }

  .cmp__picker {
    display: flex;
    align-items: center;
    gap: var(--s2);
    flex-wrap: wrap;
    padding: var(--s2);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    background: var(--surface);
  }

  /* Метку сущности и измерения нажимают целиком: она же добавляет, она же
     убирает, поэтому цель не меньше проектных 44 px. Перенос внутри метки
     обязателен: общее правило `.chip` держит её одной строкой, и длинное имя —
     а при отсутствии перевода и сырой ключ корпуса — вылезало бы за pill и
     срезалось краем рельса. */
  .cmp__picker :global(.chip),
  .cmp__chips :global(.chip) {
    min-width: 0;
    max-width: 100%;
    min-height: 44px;
    line-height: var(--lh-dense);
    white-space: normal;
    overflow-wrap: anywhere;
  }

  /* Плотные списки рельса: метки идут строками, панель держит общий ритм
     условий и не превращается в карточку внутри карточки. `flex: none` — чтобы
     панель не сжималась под высотой рельса: прокручивается рельс, а содержимое
     панели остаётся целым. */
  :global(.panel.cmp__list) {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    flex: none;
    padding: var(--s4);
    border-radius: var(--r-lg);
  }

  :global(.panel.cmp__list) p {
    margin: 0;
  }

  .cmp__list-actions {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
    margin-top: var(--s2);
    padding-top: var(--s3);
    border-top: 1px solid var(--line-soft);
  }

  /*
   * Метки списков и быстрых действий — текучий ряд с переносом, а не
   * горизонтальная полоса: общего `.scroll-x` здесь больше нет, потому что в
   * рельсе 272 px такая полоса показывала бы один чип и срезала остальные, а на
   * 390 px метки уходили бы за край блока. Перенос оставляет читаемыми и имена
   * целиком, и то, что список закончился.
   */
  .cmp__chips {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
    min-width: 0;
  }

  /* Тело свёрнутых списков на узком экране держит тот же ритм, что и рельс. */
  .cmp__corpora-body {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    max-width: none;
    padding-top: var(--s2);
  }

  .cmp__result {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    grid-column: 1;
    grid-row: 2;
  }

  .cmp__result > .panel__head {
    margin-bottom: 0;
  }

  /* Классы уходят внутрь Panel, поэтому объявляем их как глобальные: имена
     принадлежат этой странице и в app.css не встречаются. */
  :global(.panel.cmp__band) {
    padding: clamp(var(--s5), 3vw, var(--s6));
  }

  .cmp__counts {
    margin: 0;
  }

  /* Легенда итогов сверки: метка и её смысл в одной строке, рядом с таблицей,
     а не в справке внизу страницы. */
  .cmp__legend {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .cmp__legend li {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  /* Служебные имена и величины под одним раскрытием: в пути чтения их нет. */
  .cmp__svc {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
    width: 100%;
  }

  .cmp__svc summary {
    cursor: pointer;
    color: var(--ink-3);
  }

  .cmp__svc-list {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .cmp__svc-list li {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    flex-wrap: wrap;
    font-size: var(--t-micro);
    color: var(--ink-3);
  }

  .cmp__caption {
    caption-side: top;
    text-align: left;
    padding: var(--s3) var(--s4);
    font-size: var(--t-micro);
    color: var(--ink-3);
    background: var(--surface-sunk);
  }

  /*
   * Матрица живёт в своём прокручиваемом блоке на обе оси: страница горизонтальным
   * скроллом не идёт никогда, а при десятках технологий имена строк и заголовки
   * колонок остаются на месте (`position: sticky`). Потолок высоты держит таблицу
   * внутри рабочей области, вертикальная полоса у неё своя.
   */
  .cmp__matrix {
    overflow-y: auto;
    max-height: calc(100dvh - var(--topbar-h) - var(--s8));
    scrollbar-color: var(--ink-4) var(--surface-sunk);
  }

  .cmp__matrix::-webkit-scrollbar-track {
    background: var(--surface-sunk);
  }

  .cmp__matrix thead th {
    position: sticky;
    top: 0;
    z-index: 1;
    background: var(--surface-raised);
  }

  /* Угловая клетка перекрывает и шапку, и первую колонку: под ней не должно
     быть ни полосы таблицы, ни имени строки. */
  .cmp__corner {
    left: 0;
    z-index: 3;
  }

  .cmp__rowhead {
    position: sticky;
    left: 0;
    z-index: 2;
    background: var(--surface-raised);
    max-width: 240px;
  }

  .cmp__table td {
    min-width: 208px;
  }

  .cmp__item {
    display: block;
    font-weight: 600;
    color: var(--ink);
  }

  .cmp__cell {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--s2);
  }

  .cmp__value {
    font-size: var(--t-h4);
    font-weight: 600;
    color: var(--ink);
  }

  .cmp__unit {
    font-family: var(--font-data);
  }

  .cmp__conf {
    margin: 0;
  }

  .cmp__quote {
    width: 100%;
  }

  .cmp__why {
    display: block;
    margin: 0;
    color: var(--disputed);
    /* Причина нарушения — текст с числами: моно только для цифр, поэтому
       табулярные разряды вместо `--font-data`. */
    font-variant-numeric: tabular-nums;
  }

  .cmp__notes {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  .cmp__note {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    padding: var(--s5);
    border-radius: var(--r-lg);
    background: var(--surface);
    box-shadow: var(--shadow-soft);
  }

  .cmp__note-list {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    margin: 0;
  }

  .cmp__note-cell {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    padding-top: var(--s3);
    border-top: 1px solid var(--line-soft);
  }

  .cmp__note-cell dt {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s3);
    font-size: var(--t-small);
    color: var(--ink-2);
  }

  .cmp__note-cell dd {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--s2);
  }

  /* Метка предела и её источник парой: откуда взят предел видно рядом с числом,
     а не только в подсказке. */
  .cmp__limits {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s3) var(--s5);
    margin-top: var(--s3);
  }

  .cmp__limit {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
  }

  .cmp__limit .micro {
    color: var(--ink-3);
  }

  .cmp__nolimit {
    margin: 0;
    max-width: var(--maxw-measure);
  }

  .cmp__skeleton {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  .cmp__skeleton p {
    margin: 0;
  }

  .cmp__skeleton-line {
    height: 14px;
    width: 100%;
  }

  .cmp__skeleton-line--short {
    width: 44%;
  }

  .cmp__skeleton-block {
    height: clamp(120px, 22vh, 190px);
    border-radius: var(--r-lg);
  }

  /* Условия сравнения читаются плотнее: панель формы — это рабочая строка,
     а не документ с полями в 4rem. Её координата в сетке задана явно: строка 1
     главной колонки, рядом с которой встаёт рельс. */
  :global(.panel.cmp__form-panel) {
    grid-column: 1;
    grid-row: 1;
    padding: clamp(var(--s4), 2vw, var(--s5));
  }

  .cmp__status {
    margin: 0;
    color: var(--ink-2);
  }

  @media (max-width: 900px) {
    /* Рельс уходит под условия: списки корпуса больше не делят ширину с
       матрицей, результат остаётся единственным продолжением экрана.
       Разметка рельса на этом пороге снимается состоянием `narrow`, но
       сетку сбрасываем и здесь: до первой проверки медиаячеек колонка 2 не
       должна рисовать призрачный трек, а высота — держать список в скролле. */
    .cmp__work {
      grid-template-columns: minmax(0, 1fr);
    }

    .cmp__rail {
      grid-column: 1;
      grid-row: auto;
      position: static;
      max-height: none;
      padding-inline: 0;
      overflow: visible;
    }
  }

  @media (max-width: 640px) {
    /* Мобильная композиция условий: поле ввода и кнопка в одну строку,
       панель без карточных полей — матрица начинается выше. */
    .cmp__form-head :global(.btn) {
      flex: none;
    }

    :global(.panel.cmp__form-panel) {
      padding: var(--s4);
    }

    .cmp__form {
      gap: var(--s4);
    }

    .cmp__field {
      gap: var(--s2);
    }

    .cmp__form-head {
      margin-bottom: var(--s3);
    }

    /* Заголовок панели и главное действие — в одну строку даже на телефоне. */
    .cmp__form-head :global(.h3) {
      font-size: var(--t-h4);
    }
  }
</style>
