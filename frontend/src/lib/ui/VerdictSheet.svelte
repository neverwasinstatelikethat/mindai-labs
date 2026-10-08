<script lang="ts">
  import { api, ApiError } from '../api';
  import type { FeedbackResult, FindingListItem } from '../types';
  import { FEEDBACK_VERDICTS, STATUS_SHORT } from '../terms';
  import Sheet from './Sheet.svelte';
  import Button from './Button.svelte';
  import Field from './Field.svelte';
  import Notice from './Notice.svelte';
  import StatusPill from './StatusPill.svelte';

  /**
   * Вердикт открывается там, где возникло сомнение: из ответа, из темы
   * расхождения, из результата сравнения, из очереди на проверку. Отдельного
   * раздела для отзыва нет — идти за действием на другой экран значит потерять
   * контекст, в котором решение принято.
   *
   * `query_id` обязателен контракту, но у расхождения или сравнения своего
   * ответа нет. Тогда уходит нулевой идентификатор, а человек его не видит:
   * служебное значение не становится текстом интерфейса.
   */
  let {
    subject,
    subjectNote = '',
    queryId = null,
    findingId = null,
    correction = '',
    candidates = [],
    gate = '',
    onclose,
    onsent,
  }: {
    subject: string;
    subjectNote?: string;
    queryId?: string | null;
    findingId?: string | null;
    correction?: string;
    candidates?: FindingListItem[];
    /** Одна фраза об отсутствии права. Пустая строка — право есть. */
    gate?: string;
    onclose: () => void;
    onsent?: (result: FeedbackResult) => void;
  } = $props();

  const NIL_QUERY_ID = '00000000-0000-0000-0000-000000000000';

  // Начальные значения намеренно снимаются один раз: шторка открывается на
  // готовом основании, и смена `findingId` снаружи не должна переписывать
  // уже введённый комментарий.
  // svelte-ignore state_referenced_locally
  let verdict = $state<'accept' | 'reject' | 'correct'>(findingId ? 'correct' : 'accept');
  let comment = $state('');
  // svelte-ignore state_referenced_locally
  let fixedCorrection = $state(correction);
  // svelte-ignore state_referenced_locally
  let chosen = $state<string | null>(findingId);
  let query = $state('');
  let busy = $state(false);
  let failure = $state('');
  let sent = $state<FeedbackResult | null>(null);

  const filtered = $derived(
    query.trim().length < 2
      ? candidates.slice(0, 12)
      : candidates
          .filter((item) => item.statement.toLowerCase().includes(query.trim().toLowerCase()))
          .slice(0, 12),
  );

  const chosenFinding = $derived(candidates.find((item) => item.id === chosen) ?? null);
  const missingFinding = $derived(verdict === 'correct' && !chosen && candidates.length > 0);

  function validate(): string {
    if (comment.trim().length < 3) return 'Напишите, что именно вы решили: комментарий обязателен.';
    if (missingFinding) return 'Правка указывается для конкретного утверждения. Выберите его из списка.';
    return '';
  }

  async function submit(): Promise<void> {
    const problem = validate();
    if (problem) {
      failure = problem;
      return;
    }
    busy = true;
    failure = '';
    try {
      const result = await api.feedback({
        query_id: queryId ?? NIL_QUERY_ID,
        finding_id: verdict === 'correct' ? chosen : null,
        verdict,
        comment: comment.trim(),
        ...(verdict === 'correct' && fixedCorrection.trim()
          ? { correction: fixedCorrection.trim() }
          : {}),
      });
      sent = result;
      onsent?.(result);
    } catch (cause) {
      failure = phraseFor(cause);
    } finally {
      busy = false;
    }
  }

  function phraseFor(cause: unknown): string {
    if (cause instanceof ApiError) {
      switch (cause.status) {
        case 401:
          return 'Вход больше не подтверждён. Войдите заново и повторите вердикт.';
        case 403:
          return 'Вердикт не записан: этому аккаунту действие не открыто.';
        case 404:
          return 'Объект отзыва не найден: его могли заменить или закрыть вашим доступом.';
        case 409:
          return 'Это утверждение уже заменено более поздней версией. Откройте историю и решите по ней.';
        case 429:
          return 'Слишком много попыток подряд. Повторите через минуту.';
        default:
          return cause.status >= 500
            ? 'Сервис не принял вердикт. Повторите через минуту.'
            : 'Вердикт не записан. Проверьте комментарий и повторите.';
      }
    }
    return 'Сервис не ответил. Проверьте соединение и повторите.';
  }
</script>

<Sheet title="Вердикт" description={subject} width="620px" {onclose}>
  {#if gate}
    <Notice tone="warn" title="Вердикт не записывается">{gate}</Notice>
  {:else if sent}
    <Notice tone="ok" title="Вердикт записан">
      {#if sent.superseded}
        <p class="verdict__line">
          Создана новая версия утверждения, {sent.superseded.version}-я. Прежняя осталась в истории.
        </p>
        <p class="verdict__statement">{sent.superseded.statement}</p>
      {:else if verdict === 'accept'}
        <p class="verdict__line">Ответ принят. Запись осталась в текущей версии.</p>
      {:else if verdict === 'reject'}
        <p class="verdict__line">Ответ отклонён. Запись помечена вашим решением.</p>
      {:else}
        <p class="verdict__line">Правка отправлена. Новая версия появится после подтверждения.</p>
      {/if}
    </Notice>
  {:else}
    <div class="verdict__subject">
      <p class="verdict__label">Что вы решаете</p>
      <p class="verdict__text">{subject}</p>
      {#if subjectNote}<p class="micro muted">{subjectNote}</p>{/if}
    </div>

    {#if failure}<Notice tone="error">{failure}</Notice>{/if}

    <fieldset class="verdict__set">
      <legend class="verdict__label">Ваш вердикт</legend>
      <div class="verdict__options">
        {#each FEEDBACK_VERDICTS as option (option.key)}
          <label class="verdict__option" class:verdict__option--on={verdict === option.key}>
            <input
              type="radio"
              name="verdict"
              value={option.key}
              checked={verdict === option.key}
              onchange={() => (verdict = option.key)}
            />
            <span>{option.label}</span>
          </label>
        {/each}
      </div>
    </fieldset>

    {#if verdict === 'correct'}
      {#if chosenFinding}
        <div class="verdict__picked">
          <StatusPill status={chosenFinding.status} label={STATUS_SHORT[chosenFinding.status]} />
          <span class="verdict__statement">{chosenFinding.statement}</span>
        </div>
      {:else if candidates.length > 0}
        <Field
          label="Какое утверждение правим"
          name="verdict-finding"
          bind:value={query}
          placeholder="Начните писать формулировку"
          hint="Правка записывается для одного утверждения, а не для ответа целиком."
        />
        <ul class="verdict__list">
          {#each filtered as item (item.id)}
            <li>
              <button class="verdict__pick" type="button" onclick={() => (chosen = item.id)}>
                <StatusPill status={item.status} label={STATUS_SHORT[item.status]} />
                <span class="verdict__statement">{item.statement}</span>
                <span class="micro muted">версия {item.version}</span>
              </button>
            </li>
          {:else}
            <li class="micro muted">Под этот текст утверждений нет.</li>
          {/each}
        </ul>
      {/if}

      <Field
        label="Новая формулировка"
        name="verdict-correction"
        type="textarea"
        rows={3}
        bind:value={fixedCorrection}
        placeholder="Как должно быть по источнику"
        hint="Необязательно: без неё правка уходит как просьба пересмотреть."
      />
    {/if}

    <Field
      label="Комментарий"
      name="verdict-comment"
      type="textarea"
      rows={3}
      bind:value={comment}
      required
      placeholder="Что вы проверили и почему решили так"
      error={comment.length > 0 && comment.trim().length < 3 ? 'Слишком коротко: напишите по существу.' : ''}
    />
  {/if}

  {#snippet footer()}
    {#if gate}
      <Button variant="quiet" onclick={onclose}>Закрыть</Button>
    {:else if sent}
      <Button variant="action" onclick={onclose}>Готово</Button>
    {:else}
      <Button variant="quiet" onclick={onclose} disabled={busy}>Отменить</Button>
      <Button variant="action" onclick={() => void submit()} busy={busy}>Отправить вердикт</Button>
    {/if}
  {/snippet}
</Sheet>

<style>
  .verdict__subject {
    padding: var(--s4);
    border-radius: var(--r-sm);
    background: var(--surface-sunk);
  }

  .verdict__label {
    margin: 0 0 var(--s2);
    padding: 0;
    font-size: var(--t-micro);
    color: var(--ink-3);
  }

  .verdict__text {
    margin: 0;
    font-size: var(--t-body);
    line-height: var(--lh-dense);
    font-weight: 600;
    color: var(--ink);
    text-wrap: pretty;
  }

  .verdict__line {
    margin: 0;
  }

  .verdict__set {
    border: 0;
    margin: 0;
    padding: 0;
  }

  .verdict__options {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
  }

  .verdict__option {
    display: inline-flex;
    align-items: center;
    gap: var(--s2);
    padding: var(--s2) var(--s4);
    border: 1px solid var(--line);
    border-radius: var(--r-pill);
    background: var(--surface);
    font-size: var(--t-small);
    cursor: pointer;
    transition:
      border-color var(--dur-fast) var(--ease-soft),
      background-color var(--dur-fast) var(--ease-soft);
  }

  .verdict__option:hover {
    border-color: var(--line-strong);
  }

  .verdict__option--on {
    border-color: var(--ink);
    background: var(--surface-raised);
    font-weight: 600;
  }

  .verdict__option input {
    accent-color: var(--action-ink);
  }

  .verdict__option input:focus-visible {
    outline: 2px solid var(--action-ink);
    outline-offset: 2px;
  }

  .verdict__picked {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    padding: var(--s3) var(--s4);
    border-radius: var(--r-sm);
    background: var(--surface-sunk);
  }

  .verdict__statement {
    margin: 0;
    font-size: var(--t-small);
    line-height: var(--lh-dense);
    color: var(--ink-2);
    text-wrap: pretty;
  }

  .verdict__list {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    margin: 0;
    padding: 0;
    max-block-size: 16rem;
    overflow-y: auto;
    list-style: none;
  }

  .verdict__pick {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    inline-size: 100%;
    padding: var(--s3);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-sm);
    background: var(--surface);
    text-align: start;
    cursor: pointer;
    transition: border-color var(--dur-fast) var(--ease-soft);
  }

  .verdict__pick:hover {
    border-color: var(--line-strong);
  }
</style>
