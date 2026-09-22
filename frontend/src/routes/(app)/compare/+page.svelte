<script lang="ts">
  // Сравнение объектов по измерениям: та же матрица, что и в старом листе, но
  // собранная композициями Softly — широкая утопленная панель с условиями
  // сравнения и таблицей в `.table-wrap`, а на мобильном матрица разворачивается
  // в стек заметок на объект, а не сжимается до горизонтального скролла.
  // Списки корпуса стоят в правом рельсе отдельной колонкой сетки: он не
  // отодвигает результат и не растёт выше вьюпорта.
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { api, ApiError } from '$lib/api';
  import { countOf, num, pct } from '$lib/format';
  import { scrollRegion } from '$lib/scroll-region';
  import { session } from '$lib/sessionStore.svelte';
  import { OPERATOR_SYMBOL, PROPERTY_LABELS, SUBJECT_LABELS, termOf } from '$lib/terms';
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

  // Предел корпуса: направление, величина, единица и где он взят — из оператора
  // наблюдения (`operator`) или из его формулировки (`wording`).
  type Limit = { kind: 'lte' | 'gte'; at: number; unit: string; from: 'operator' | 'wording' };
  type Check = 'outside' | 'within' | 'none';

  // Право проверяет сервер (`export:run`); клиентская сверка нужна, чтобы не
  // слать заведомо отклоняемый вызов, а отказ по доступу показывается тем же
  // человеческим текстом.
  const allowed = $derived(
    session.state === 'unknown' ? null : session.can('export:run'),
  );

  let question = $state('');
  let entities = $state<string[]>([]);
  let dimensions = $state<string[]>([]);
  let entityDraft = $state('');
  let dimensionDraft = $state('');
  let matrix = $state<ComparisonTable | null>(null);
  let corpus = $state<FindingListItem[] | null>(null);
  let corpusError = $state('');
  let loading = $state(false);
  let attempted = $state(false);
  let error = $state('');
  let forbidden = $state('');
  let questionErr = $state('');
  let entitiesErr = $state('');
  let submitted = $state(false);

  // Мобильная композиция матрицы: стек заметок на объект вместо таблицы.
  // Точка перестройки — проектный брейкпоинт 900px: ниже него и рельс уходит
  // под условия, и матрица разворачивается в заметки, поэтому ничего не
  // сжимается в горизонтальный скролл на 781–900px.
  let narrow = $state(false);
  // Списки корпуса и пояснение матрицы — вторичный слой: на широком экране они
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
  const subjectNames = $derived(
    [...new Set(sample.map((f) => f.subject).filter((name): name is string => Boolean(name)))].sort(
      (a, b) => termOf(SUBJECT_LABELS, a).localeCompare(termOf(SUBJECT_LABELS, b), 'ru'),
    ),
  );
  const propertyNames = $derived(
    [
      ...new Set(sample.flatMap((f) => f.observations.map((o) => o.property_name))),
    ].sort((a, b) => termOf(PROPERTY_LABELS, a).localeCompare(termOf(PROPERTY_LABELS, b), 'ru')),
  );

  // Имя ключа корпуса для интерфейса: служит то же «terms.ts», что и в находках.
  function entityLabel(name: string): string {
    return termOf(SUBJECT_LABELS, name);
  }

  function dimensionLabel(name: string): string {
    return termOf(PROPERTY_LABELS, name);
  }

  // Общий формат числа для обеих таблиц: сервер отдаёт «≥92.5» с точкой и без
  // пробела, экран показывает «≥ 92,5» — как `num()` в находках.
  function serverValue(text: string): string {
    return text
      .replace(/([≤≥<>]=?)(?=\d)/g, '$1 ')
      .replace(/(\d)\.(\d)/g, '$1,$2');
  }

  const limits = $derived.by<Record<string, Limit>>(() => {
    const map: Record<string, Limit> = {};
    for (const finding of sample) {
      for (const observation of finding.observations) {
        const bound = boundOf(observation);
        if (bound && !(bound.property in map)) map[bound.property] = bound;
      }
    }
    return map;
  });
  const declaredLimits = $derived(Object.entries(limits));

  // Метка строки честная только тогда, когда хотя бы одно измерение этой строки
  // имеет распознанный предел: вывод о соответствии не ставится строке, которой
  // сверять не с чем.
  function rowAssessment(
    row: ComparisonRow,
    headers: string[],
  ): { pill: 'consensus' | 'disputed' | 'off'; label: string; why: string } {
    let checked = 0;
    for (const header of headers) {
      const result = checkOf(header, row.cells[header]);
      if (result.check === 'outside') return { pill: 'disputed', label: 'вне предела', why: result.why };
      if (result.check === 'within') checked += 1;
    }
    return checked > 0
      ? { pill: 'consensus', label: 'сверки выдержаны', why: '' }
      : { pill: 'off', label: 'предел не распознан', why: '' };
  }

  const resultStatus = $derived.by(() => {
    if (forbidden) return 'Сравнение закрыто: нужен доступ к корпусу.';
    if (error) return 'Сравнение не выполнено.';
    if (loading) return 'Считаем матрицу…';
    if (!matrix) return 'Матрица ещё не построена.';
    if (matrix.headers.length === 0) return 'Указанных измерений в находках нет.';
    if (matrix.rows.length === 0) return 'Субъекты не найдены.';
    return `Матрица готова: ${countOf(matrix.rows.length, 'строка', 'строки', 'строк')}, ${countOf(
      matrix.headers.length,
      'измерение',
      'измерения',
      'измерений',
    )}.`;
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
        why: `${property}: ${num(Math.max(...numbers))} ${limit.unit} ${OPERATOR_SYMBOL.gt} ${num(limit.at)} ${limit.unit}`,
      };
    }
    if (limit.kind === 'gte' && Math.min(...numbers) < limit.at) {
      return {
        check: 'outside',
        why: `${property}: ${num(Math.min(...numbers))} ${limit.unit} ${OPERATOR_SYMBOL.lt} ${num(limit.at)} ${limit.unit}`,
      };
    }
    return { check: 'within', why: '' };
  }

  /**
   * Статус ячейки выводится только из того, что есть в данных: значения нет —
   * «наблюдения нет»; предел распознан и нарушен — «вне предела»; предел
   * распознан и выдержан — «в пределе»; предела нет — «не сверено».
   */
  function cellState(
    header: string,
    cell: ComparisonCell | undefined,
  ): { pill: 'consensus' | 'hypothesis' | 'disputed' | 'off'; label: string } {
    if (!cell?.value) return { pill: 'off', label: 'наблюдения нет' };
    if (!limits[header]) return { pill: 'hypothesis', label: 'не сверено' };
    const result = checkOf(header, cell);
    return result.check === 'outside'
      ? { pill: 'disputed', label: 'вне предела' }
      : { pill: 'consensus', label: 'в пределе' };
  }

  // Цвет полосы уверенности честный: зелёный и красный — только когда сверку
  // действительно удалось выполнить; во всех остальных случаях — коралл действия.
  function barModifier(pill: 'consensus' | 'hypothesis' | 'disputed' | 'off'): string {
    return pill === 'consensus' || pill === 'disputed' ? ` bar__fill--${pill}` : '';
  }

  function confidenceWidth(value: number): string {
    return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
  }

  function toggleEntity(name: string): void {
    entities = entities.includes(name) ? entities.filter((v) => v !== name) : [...entities, name];
    entitiesErr = '';
  }

  function toggleDimension(name: string): void {
    dimensions = dimensions.includes(name) ? dimensions.filter((v) => v !== name) : [...dimensions, name];
  }

  function commitEntity(): void {
    const value = entityDraft.trim();
    if (value && !entities.includes(value)) entities = [...entities, value];
    entityDraft = '';
    entitiesErr = '';
  }

  function commitDimension(): void {
    const value = dimensionDraft.trim();
    if (value && !dimensions.includes(value)) dimensions = [...dimensions, value];
    dimensionDraft = '';
  }

  function onEntityKey(event: KeyboardEvent): void {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    commitEntity();
  }

  function onDimensionKey(event: KeyboardEvent): void {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    commitDimension();
  }

  async function loadCorpus(): Promise<void> {
    try {
      corpus = await api.findings();
      corpusError = '';
    } catch (reason) {
      corpus = null;
      corpusError =
        reason instanceof ApiError && reason.status === 403
          ? 'находки корпуса этому аккаунту не открыты'
          : 'списки корпуса не загрузились';
    }
  }

  async function runCompare(): Promise<void> {
    submitted = true;
    error = '';
    forbidden = '';
    questionErr = question.trim().length < 3 ? 'Вопрос сравнения — от 3 символов.' : '';
    entitiesErr = entities.length === 0 ? 'Добавьте хотя бы одну сущность.' : '';
    if (questionErr || entitiesErr) {
      matrix = null;
      attempted = false;
      return;
    }

    loading = true;
    attempted = true;
    try {
      matrix = await api.compare(question.trim(), entities, dimensions);
    } catch (reason) {
      matrix = null;
      // Отказ по доступу и технический сбой — две разные строки, и обе человеческие.
      if (reason instanceof ApiError && reason.status === 403) {
        forbidden =
          'Сравнение открыто аккаунтам с доступом к корпусу — запросите его у администратора сервиса.';
      } else {
        error = 'Не удалось посчитать матрицу. Проверьте соединение и повторите сравнение.';
      }
    } finally {
      loading = false;
    }
  }

  async function runWithoutDimensions(): Promise<void> {
    dimensions = [];
    await runCompare();
  }

  async function compareWithSubject(name: string): Promise<void> {
    entities = [name];
    await runCompare();
  }

  function takeSubjects(): void {
    entities = subjectNames.slice(0, 4);
  }

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
  <title>Сравнение технологий — Научный Клубок</title>
</svelte:head>

{#snippet corporaBlock()}
  <Panel tone="sunk" class="cmp__list">
    <p class="micro muted">
      Нажатие на субъект или свойство добавляет его в условия сравнения, повторное нажатие
      убирает.
    </p>

    <p class="field__label">
      Субъекты текущего корпуса
      <!-- Число при заголовке говорит, сколько меток в списке всего: в рельсе
           сразу видно не все. Во время загрузки счётчика нет — «0» выглядел бы
           как пустой корпус. -->
      {#if corpus}
        <span class="muted">(<span class="num">{subjectNames.length}</span>)</span>
      {/if}
    </p>
    {#if corpus}
      {#if subjectNames.length === 0}
        <p class="micro muted">
          В находках корпуса пока нет ни одного субъекта — сущность можно ввести вручную.
        </p>
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
      <p class="micro muted">Список субъектов не получен: {corpusError}</p>
    {:else}
      <div class="cmp__skeleton" role="status">
        <span class="skeleton cmp__skeleton-line"></span>
        <span class="skeleton cmp__skeleton-line cmp__skeleton-line--short"></span>
        <p class="micro muted">Читаем находки корпуса…</p>
      </div>
    {/if}

    <p class="field__label">
      Свойства наблюдений
      {#if corpus}
        <span class="muted">(<span class="num">{propertyNames.length}</span>)</span>
      {/if}
    </p>
    {#if corpus}
      {#if propertyNames.length === 0}
        <p class="micro muted">
          В находках корпуса нет ни одного числового свойства — измерения можно ввести
          вручную или снять фильтр.
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
      <p class="micro muted">Свойства не получены: {corpusError}</p>
    {:else}
      <p class="micro muted" role="status">Свойства появятся, когда прочитаем находки корпуса.</p>
    {/if}

    <div class="cmp__list-actions">
      <Button variant="quiet" size="sm" disabled={loading} onclick={() => void loadCorpus()}>
        Обновить списки корпуса
      </Button>
      {#if subjectNames.length}
        <Button variant="ghost" size="sm" disabled={loading} onclick={takeSubjects}>
          Взять первые четыре субъекта
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
      <span>Как устроена матрица</span>
      <Icon name="plus" size={16} class="acc__icon" />
    </button>
    <!-- Тело остаётся в DOM и получает `hidden`: ссылка aria-controls ведёт к
         существующему элементу в обоих состояниях раскрытия. -->
    <div class="acc__body" id="cmp-help-body" hidden={!helpOpen}>
      <p>
        Строки — субъекты утверждений, колонки — свойства числовых наблюдений. В ячейке
        значение с единицей, цитата источника и уверенность извлечения.
      </p>
      <p>
        Сущность ищется как подстрока субъекта утверждения, измерение сопоставляется со
        свойством числового наблюдения. Как считаются метки предела — сказано под таблицей,
        там же, где сами пределы.
      </p>
    </div>
  </div>
{/snippet}

<div class="page cmp-page">
  <div class="wrap">
    <!-- Узкий экран отдаёт вертикаль условиям и результату: метку раздела
         на нём заменяет заголовок. -->
    <SectionHead
      level="1"
      eyebrow={narrow ? '' : 'Корпус · объекты · измерения'}
      title="Матрица по объектам и измерениям"
    />

    {#if allowed === null}
      <Panel tone="sunk">
        <div class="cmp__skeleton">
          <span class="skeleton cmp__skeleton-line"></span>
          <span class="skeleton cmp__skeleton-block"></span>
          <p class="micro muted">Уточняем доступ к сравнению — матрицу строим после этого.</p>
        </div>
      </Panel>
    {:else if !allowed}
      <Panel tone="lav">
        <div class="panel__head">
          <h2 class="h3">Сравнение недоступно</h2>
          <StatusPill status="off" label="нет доступа" />
        </div>
        <p class="lead small">
          Матрица по объектам и измерениям открыта аккаунтам с доступом к корпусу —
          запросите его у администратора сервиса.
        </p>
        <div class="row">
          <Button href="/research" variant="quiet">Рабочее пространство</Button>
          <Button href="/findings" variant="ghost">Находки корпуса</Button>
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
              <Field
                label="Вопрос сравнения"
                name="cmp-question"
                bind:value={question}
                placeholder="Какие технологии сопоставляем?"
                error={submitted ? questionErr : ''}
                hint="От 3 символов — короче не принять."
                onenter={() => void runCompare()}
              />
            </div>

            <div class="cmp__field">
              <p class="field__label">
                Сущности
                <span class="muted">({entities.length})</span>
              </p>
              <div class="cmp__picker">
                <!-- Метка выбранной сущности и есть действие: нажатие убирает её
                     из сравнения. Отдельной кнопки-крестика в 22 px нет. -->
                {#each entities as name (name)}
                  <Chip pressed onclick={() => toggleEntity(name)}>{entityLabel(name)}</Chip>
                {/each}
                <input
                  class="input cmp__draft"
                  aria-label="Добавить сущность: субъект утверждения и Enter"
                  type="text"
                  bind:value={entityDraft}
                  onkeydown={onEntityKey}
                  placeholder="субъект и Enter"
                />
                <Button variant="quiet" size="sm" onclick={commitEntity} disabled={!entityDraft.trim()}>
                  Добавить
                </Button>
              </div>
              {#if submitted && entitiesErr}
                <p class="field__error">{entitiesErr}</p>
              {:else}
                <p class="field__hint">
                  Достаточно одной сущности, можно несколько; нажатие на метку убирает её.
                </p>
              {/if}
            </div>

            <div class="cmp__field">
              <p class="field__label">
                Измерения
                <span class="muted">({dimensions.length}) · необязательно</span>
              </p>
              <div class="cmp__picker">
                {#each dimensions as name (name)}
                  <Chip pressed onclick={() => toggleDimension(name)}>{dimensionLabel(name)}</Chip>
                {/each}
                <input
                  class="input cmp__draft"
                  aria-label="Добавить измерение: свойство наблюдения и Enter"
                  type="text"
                  bind:value={dimensionDraft}
                  onkeydown={onDimensionKey}
                  placeholder="свойство и Enter"
                />
                <Button variant="quiet" size="sm" onclick={commitDimension} disabled={!dimensionDraft.trim()}>
                  Добавить
                </Button>
              </div>
              <p class="field__hint">
                Без измерений матрица идёт по всем свойствам наблюдений; нажатие на метку убирает её.
              </p>
            </div>
          </div>
        </Panel>

        <section class="cmp__result" aria-label="Результат сравнения">
          <div class="panel__head">
            <h2 class="h3">Результат</h2>
            <p class="micro muted">матрица строится по находкам корпуса</p>
          </div>
          <p class="micro cmp__status" role="status" aria-live="polite">{resultStatus}</p>

          {#if forbidden}
            <Panel tone="coral">
              <Notice tone="error" title="Сравнение недоступно">
                {forbidden}
              </Notice>
              <div class="row">
                <Button href="/findings" variant="quiet">Находки корпуса</Button>
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
                  Перечитать корпус
                </Button>
              </div>
            </Panel>
          {:else if loading}
            <Panel tone="sunk">
              <div class="cmp__skeleton">
                <p class="micro muted">
                  В ячейке — все различные значения наблюдений по свойству: расхождение
                  источников не прячется за одной удачной цифрой.
                </p>
                <span class="skeleton cmp__skeleton-line"></span>
                <span class="skeleton cmp__skeleton-block"></span>
                <span class="skeleton cmp__skeleton-line"></span>
                <span class="skeleton cmp__skeleton-line cmp__skeleton-line--short"></span>
              </div>
            </Panel>
          {:else if !matrix && !attempted}
            <Empty
              icon="compare"
              title="До первого запроса матрицы нет"
              body="Добавьте сущности — сопоставим их субъекты со свойствами наблюдений и покажем таблицу."
            >
              {#snippet action()}
                <div class="row">
                  {#if subjectNames.length}
                    <Button variant="quiet" size="sm" onclick={takeSubjects}>Взять первые четыре субъекта</Button>
                  {/if}
                  <Button variant="ghost" size="sm" href="/graph">Посмотреть узлы на карте</Button>
                </div>
              {/snippet}
            </Empty>
          {:else if matrix && matrix.headers.length === 0}
            <Empty
              icon="filter"
              title="Указанных измерений в находках нет"
              body="У выбранных субъектов нет этих измерений: снимите фильтр — и в таблице останутся все свойства наблюдений."
            >
              {#snippet action()}
                <div class="row">
                  <Button variant="quiet" size="sm" onclick={() => void runWithoutDimensions()}>
                    Снять фильтр измерений
                  </Button>
                  <Button variant="ghost" size="sm" onclick={() => void loadCorpus()}>
                    Перечитать свойства корпуса
                  </Button>
                </div>
              {/snippet}
            </Empty>
          {:else if matrix && matrix.rows.length === 0}
            <Empty
              icon="search"
              title="Субъекты не найдены"
              body="Ни одно утверждение корпуса не имеет субъекта, содержащего указанную строку."
            >
              {#snippet action()}
                <!-- Быстрый выбор субъекта есть только когда корпус его отдал:
                     пустой ряд не должен оставлять лишнюю строку в пустом
                     состоянии. -->
                {#if subjectNames.length}
                  <div class="cmp__chips">
                    {#each subjectNames as name (name)}
                      <Button variant="quiet" size="sm" onclick={() => void compareWithSubject(name)}>
                        {entityLabel(name)}
                      </Button>
                    {/each}
                  </div>
                {/if}
              {/snippet}
            </Empty>
          {:else if matrix}
            <Panel tone="sunk" class="cmp__band">
              <div class="panel__head">
                <div>
                  <p class="eyebrow"><Icon name="scale" size={16} /> вопрос сравнения</p>
                  <p class="h4">{matrix.question}</p>
                </div>
                <p class="micro muted cmp__counts">
                  {countOf(matrix.rows.length, 'строка', 'строки', 'строк')} ·
                  {countOf(matrix.headers.length, 'измерение', 'измерения', 'измерений')}
                </p>
              </div>

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
                          {@const cell = row.cells[header]}
                          {@const state = cellState(header, cell)}
                          <div class="cmp__note-cell">
                            <dt>
                              {dimensionLabel(header)}
                              <StatusPill status={state.pill} label={state.label} />
                            </dt>
                            <dd>
                              {#if cell?.value}
                                <span class="num cmp__value">{serverValue(cell.value)}</span>
                                {#if cell.unit}<span class="micro muted cmp__unit">{cell.unit}</span>{/if}
                                <!-- Полоса — повтор числа: значение и так сказано
                                     строкой ниже, поэтому экрану чтения она не нужна. -->
                                <div class="bar" aria-hidden="true">
                                  <span
                                    class="bar__fill{barModifier(state.pill)}"
                                    style="width: {confidenceWidth(cell.confidence)}"
                                  ></span>
                                </div>
                                <p class="micro muted">
                                  уверенность извлечения <span class="num">{pct(cell.confidence)}</span>
                                </p>
                                {#if cell.evidence}<blockquote class="quote">{cell.evidence}</blockquote>{/if}
                              {:else}
                                <span class="small muted">наблюдения нет</span>
                              {/if}
                            </dd>
                          </div>
                        {/each}
                      </dl>
                    </article>
                  {/each}
                </div>
              {:else}
                <!-- Широкая матрица на 1024 px и при пяти измерениях уходит за
                     блок: `.table-wrap` режет её с видимым жёлобом, но полоса
                     должна крутиться и с клавиатуры. `use:scrollRegion` ставит
                     `tabindex` только когда есть что прокручивать, поэтому на
                     целиком видимой таблице лишней точки фокуса не появляется. -->
                <div
                  class="table-wrap"
                  role="region"
                  aria-label="Таблица сравнения"
                  use:scrollRegion
                >
                  <table class="table cmp__table">
                    <caption class="cmp__caption">
                      Сравнение {countOf(matrix.rows.length, 'объект', 'объекта', 'объектов')} по
                      {countOf(matrix.headers.length, 'измерению', 'измерениям', 'измерениям')}
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">объект</th>
                        {#each matrix.headers as header (header)}
                          <th scope="col">{dimensionLabel(header)}</th>
                        {/each}
                      </tr>
                    </thead>
                    <tbody>
                      {#each matrix.rows as row (row.item)}
                        {@const assess = rowAssessment(row, matrix.headers)}
                        <tr>
                          <th scope="row">
                            <span class="cmp__item">{entityLabel(row.item)}</span>
                            <StatusPill status={assess.pill} label={assess.label} />
                            {#if assess.why}<span class="micro cmp__why">{assess.why}</span>{/if}
                          </th>
                          {#each matrix.headers as header (header)}
                            {@const cell = row.cells[header]}
                            {@const state = cellState(header, cell)}
                            <td>
                              {#if cell?.value}
                                <div class="cmp__cell">
                                  <span class="num cmp__value">{serverValue(cell.value)}</span>
                                  {#if cell.unit}<span class="micro muted cmp__unit">{cell.unit}</span>{/if}
                                  <StatusPill status={state.pill} label={state.label} />
                                  <!-- Полоса повторяет число из строки под ней. -->
                                  <div class="bar" aria-hidden="true">
                                    <span
                                      class="bar__fill{barModifier(state.pill)}"
                                      style="width: {confidenceWidth(cell.confidence)}"
                                    ></span>
                                  </div>
                                  <p class="micro muted cmp__conf">
                                    уверенность извлечения <span class="num">{pct(cell.confidence)}</span>
                                  </p>
                                  {#if cell.evidence}
                                    <blockquote class="quote cmp__quote">{cell.evidence}</blockquote>
                                  {/if}
                                </div>
                              {:else}
                                <span class="small muted">наблюдения нет</span>
                              {/if}
                            </td>
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
                <p class="small">
                  Предел измеряется по самому наблюдению: если у наблюдения есть оператор
                  «≤» или «≥», пределом считается его нормализованное число, если оператора
                  нет — направление распознаётся по формулировке наблюдения («не выше»,
                  «выше»). Это распознавание, а не поле корпуса: откуда взят каждый предел,
                  подписано под меткой. «Вне предела» — значение вышло за распознанный предел
                  того же свойства и единицы, «не сверено» — предела для измерения нет.
                </p>
                <div class="cmp__limits">
                  {#each declaredLimits as [property, limit] (property)}
                    <span class="tag">
                      {dimensionLabel(property)} {OPERATOR_SYMBOL[limit.kind]}
                      <span class="num">{num(limit.at)}</span> {limit.unit}
                      <span class="micro cmp__limit-from">
                        {limit.from === 'operator' ? 'по оператору наблюдения' : 'по формулировке'}
                      </span>
                    </span>
                  {/each}
                </div>
              </Panel>
            {:else}
              <p class="micro muted cmp__nolimit">
                Ни в одном наблюдении корпуса предел не распознаётся — ни оператором «≤»/«≥»,
                ни формулировкой вида «не выше». Поэтому на этой таблице нет меток «в пределе»
                и «вне предела»: строки помечены «предел не распознан», ячейки — «не сверено».
              </p>
            {/if}
          {/if}
        </section>

        <!-- Рельс — колонка рядом с условиями и результатом, а не блок перед
             ними: каким бы длинным ни оказался список корпуса, результат
             остаётся под условиями. Сам рельс ограничен высотой вьюпорта и
             прокручивается своей полосой (см. `.cmp__rail`). -->
        {#if !narrow}
          <aside class="cmp__rail" aria-label="Списки корпуса" use:scrollRegion>
            {@render corporaBlock()}
          </aside>
        {/if}
      </div>

      <!-- Узкий экран: списки корпуса — под результатом, чтобы условия и
           матрица остались в первом вьюпорте. -->
      {#if narrow}
        <div class="acc cmp__corpora">
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

  .cmp__draft {
    flex: 1 1 180px;
    min-width: 160px;
    min-height: 44px;
    border: 0;
    background: var(--surface-sunk);
    font-size: var(--t-small);
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

  /* Быстрые действия под пустым результатом — те же субъекты корпуса, но
     обычными `.btn`: ряд переносится, а одно длинное имя (свободный текст
     субъекта или сырой ключ без перевода) обязано переноситься внутри метки, а
     не вылезать за центральный блок — иначе его резало бы краем `.empty`. */
  .cmp__chips :global(.btn) {
    min-width: 0;
    max-width: 100%;
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

  .cmp__caption {
    caption-side: top;
    text-align: left;
    padding: var(--s3) var(--s4);
    font-size: var(--t-micro);
    color: var(--ink-3);
    background: var(--surface-sunk);
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

  .cmp__limits {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
    margin-top: var(--s3);
  }

  /* Откуда взят предел — подписью при самой метке: распознавание из
     формулировки наблюдения, а не структурированное поле корпуса. */
  .cmp__limit-from {
    color: var(--ink-4);
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
    .cmp__draft {
      flex: 1 1 120px;
      min-width: 0;
    }

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
