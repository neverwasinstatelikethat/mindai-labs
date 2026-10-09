<script lang="ts">
  /**
   * Рабочий экран агента: единственный транспорт — SSE поверх прокси
   * `/backend`. Компонент `ChatPanel` показывает данные и собирает формы, все
   * вызовы остаются здесь: ответ сервера не подменяется заглушкой ни в одном
   * состоянии. Права читаются из подтверждённой сессии (`session.can`),
   * заголовков роли клиент не отправляет.
   */
  import { ApiError, api, apiUrl } from '$lib/api';
  import ChatPanel, {
    type HistoryResult,
    type RunFailure,
    type SheetNotice,
  } from '$lib/ChatPanel.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import { page } from '$app/state';
  import { session } from '$lib/sessionStore.svelte';
  import { FAILURE_WORDS, RUN_FAILURES } from '$lib/terms';
  import type { RunFailureKind } from '$lib/terms';
  import type { AnswerHistoryItem, AnswerPayload } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import { stageOfNode } from '$lib/terms/research';

  interface StreamEvent {
    type: string;
    question?: string;
    answer?: AnswerPayload;
    step?: { agent?: string; status?: string };
    code?: string;
    message?: string;
  }

  // Вопрос со входа в раздел (/research?q=…): читаем один раз при открытии
  // экрана; экран подставляет его в поле и запускает ответ сам.
  const seed = new URLSearchParams(page.url.search).get('q')?.trim() ?? '';

  let question = $state('');
  let answer = $state<AnswerPayload | null>(null);
  let turns = $state<AnswerPayload[]>([]);
  let threadId: string | null = null;
  let progressStages = $state<string[]>([]);
  let history = $state<AnswerHistoryItem[]>([]);
  let historyHasMore = $state(false);
  let historyOffset = $state(0);
  let historyLoading = $state(false);
  let historyLoaded = $state(false);
  let historyError = $state('');
  let historyOpen = $state(false);
  let historySelection = $state('');
  let savingConversation = $state<string | null>(null);
  let savedConversation = $state<string | null>(null);
  let historySaveError = $state('');
  let historyRequest = 0;
  let running = $state(false);
  let webSearchEnabled = $state(true);
  // Прежний ответ остаётся доступен в серверной истории после нового вопроса.
  let delivered = false;
  let failure = $state<RunFailure | null>(null);
  // Отдельное состояние для эталонных вопросов: пустой набор и непрогруженный
  // канал — разные экраны, и «повторить» должно чинить именно канал.
  let openClaimId = $state<string | null>(null);
  let focusKey = $state<string | null>(null);
  let busy = $state<'feedback' | 'import' | null>(null);
  let notice = $state<SheetNotice | null>(null);

  let controller: AbortController | null = null;

  const canAsk = $derived(session.can('query:ask'));

  // ── Формат ──────────────────────────────────────────────────────────────
  // Числа и длительности — через общие `num` и `duration` из `$lib/format`:
  // локальная копия расходилась с остальными экранами в разрядах и показывала
  // «1,234» там, где другие давали «1,23».

  function reasonText(reason: unknown, fallback: string): string {
    if (reason instanceof TypeError) return fallback;
    if (reason instanceof ApiError && reason.status === 404) return fallback;
    const message = reason instanceof Error ? reason.message : '';
    return message || fallback;
  }

  // ── Сборка ответа: SSE поверх прокси /backend ────────────────────────────

  function applyAnswer(payload: AnswerPayload, refreshHistory = true): void {
    delivered = true;
    answer = payload;
    threadId = payload.conversation_id ?? threadId;
    if (refreshHistory && !turns.some((turn) => turn.query_id === payload.query_id)) {
      turns = [...turns, payload];
    }
    question = payload.question;
    openClaimId = null;
    focusKey = null;
    historySelection = payload.query_id;
    if (refreshHistory) void loadAnswerHistory(0);
  }

  async function loadAnswerHistory(offset = 0): Promise<void> {
    if (!session.signedIn) return;
    const requestId = ++historyRequest;
    historyLoading = true;
    historyError = '';
    try {
      const page = await api.answerHistory(30, offset);
      if (requestId !== historyRequest) return;
      history = offset ? [...history, ...page.items] : page.items;
      historyOffset = offset + page.items.length;
      historyHasMore = page.has_more;
      if (offset === 0) historyLoaded = true;
    } catch (reason) {
      if (requestId !== historyRequest) return;
      historyError = reasonText(reason, 'История пока недоступна.');
    } finally {
      if (requestId === historyRequest) historyLoading = false;
    }
  }

  async function openSavedAnswer(item: AnswerHistoryItem): Promise<void> {
    historySelection = item.query_id;
    historyOpen = false;
    if (item.query_id === answer?.query_id) return;
    historyLoading = true;
    historyError = '';
    failure = null;
    try {
      const payload = await api.savedAnswer(item.query_id);
      turns = [payload];
      threadId = payload.conversation_id ?? null;
      applyAnswer(payload, false);
    } catch (reason) {
      historyError = reasonText(reason, 'Этот ответ больше недоступен.');
    } finally {
      historyLoading = false;
    }
  }

  function conversationMarkdown(item: AnswerHistoryItem, payload: AnswerPayload): string {
    const sections = [
      '# Диалог в StormIdea',
      `\n${new Intl.DateTimeFormat('ru', { dateStyle: 'long', timeStyle: 'short' }).format(new Date(item.created_at))}`,
      '\n## Вы',
      `\n${payload.question}`,
      '\n## StormIdea',
      `\n${payload.summary}`,
    ];

    if (payload.findings.length) {
      sections.push('\n### Выводы');
      for (const finding of payload.findings) {
        sections.push(`\n#### ${finding.statement}`);
        for (const evidence of finding.evidence) {
          const location = [
            evidence.page != null ? `стр. ${evidence.page}` : '',
            evidence.sheet ? `лист «${evidence.sheet}»` : '',
            evidence.cell_range ? `ячейки ${evidence.cell_range}` : '',
          ].filter(Boolean).join(', ');
          sections.push(`\n- ${evidence.source_title}${location ? `, ${location}` : ''}: «${evidence.quote}»`);
        }
      }
    }

    const append = (title: string, values: string[]) => {
      if (!values.length) return;
      sections.push(`\n### ${title}`);
      sections.push(...values.map((value) => `\n- ${value}`));
    };
    append('Расхождения', payload.conflicts);
    append('Что ещё нужно выяснить', payload.knowledge_gaps);
    append('Следующие шаги', payload.recommendations);
    return `${sections.join('')}\n`;
  }

  async function saveConversation(item: AnswerHistoryItem): Promise<void> {
    if (savingConversation) return;
    savingConversation = item.query_id;
    savedConversation = null;
    historySaveError = '';
    try {
      // Ответ загружается с сервера: владелец записи и текущий доступ к каждому
      // материалу проверяются до сборки файла.
      const payload = await api.savedAnswer(item.query_id);
      const blob = new Blob([conversationMarkdown(item, payload)], {
        type: 'text/markdown;charset=utf-8',
      });
      const href = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = href;
      link.download = `stormidea-dialog-${new Date(item.created_at).toISOString().slice(0, 10)}.md`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(href), 0);
      savedConversation = item.query_id;
    } catch (reason) {
      historySaveError = reasonText(reason, 'Не удалось сохранить этот диалог.');
    } finally {
      savingConversation = null;
    }
  }

  // Причина из ответа сервиса попадает в интерфейс только тогда, когда она
  // про задачу человека. Диагноз контура (ключ модели, эндпоинт, порт, сессия)
  // остаётся в логах: на экране вместо него — состояние и действие.
  const OPS_DETAIL = /llm|gigachat|api[ _-]?key|эндпоинт|endpoint|backend|бэкенд|порт|docker|neo4j|elastic|in-memory|session|cookie|http\s?\d{3}|\b(4|5)\d{2}\b/i;

  function humanDetail(detail: string): string {
    if (OPS_DETAIL.test(detail)) return '';
    // Служебные имена процесса («рабочий процесс», «узел critic») читаются как
    // журнал, а не как причина для аналитика: их меняет словарь показа.
    return FAILURE_WORDS.reduce(
      (value, [pattern, word]) => value.replace(pattern, word),
      detail.trim(),
    );
  }

  // 429 приходит с телом: сколько ответов собирается одновременно, каков предел
  // и через сколько секунд попробовать. Числа показывает только те, что назвал
  // сервис.
  interface Admission {
    active?: number;
    limit?: number;
    retryAfter?: number;
  }

  // Отказ собирает словарь `RUN_FAILURES`: шесть разных причин не должны
  // звучать как одна («не дошёл» / «не отвечает» / «оборвался»). У каждой —
  // своё название и свой следующий шаг, а не общий совет «повторите позже».
  function failureOf(kind: RunFailureKind, asked: string, rawDetail = ''): RunFailure {
    const text = RUN_FAILURES[kind];
    const detail = humanDetail(rawDetail);
    return {
      label: text.label,
      detail: detail ? `${text.detail} ${detail}` : text.detail,
      recovery: text.recovery,
      question: asked,
      login: kind === 'expired',
    };
  }

  // «LLM не настроен» (настройка сервиса), «модель занята» (отказ по лимиту
  // запросов на её стороне) и «модель не ответила» (живой сбой провайдера)
  // приходят одним 503 и одним кодом потока. Развести их нужно потому, что
  // советы расходятся: без ключа повтор бесполезен, при лимите — помогает.
  const MODEL_NOT_CONFIGURED = /не настроен|не задан|ключ|api[ _-]?key|not configured/i;
  const MODEL_BUSY = /rate limit|too many requests|429|лимит запросов/i;

  function modelFailure(rawDetail: string, asked: string): RunFailure {
    const kind: RunFailureKind = MODEL_BUSY.test(rawDetail)
      ? 'modelBusy'
      : MODEL_NOT_CONFIGURED.test(rawDetail)
        ? 'noModel'
        : 'modelFailed';
    return failureOf(kind, asked);
  }

  function failureFromResponse(
    status: number,
    rawDetail: string,
    unreachable: boolean,
    asked: string,
    admission: Admission | null = null,
  ): RunFailure {
    if (unreachable) return failureOf('unreachable', asked, rawDetail);
    if (status === 429) {
      const text = RUN_FAILURES.busy;
      const busy =
        admission?.active != null && admission.limit != null
          ? `Сейчас сервис собирает ${admission.active} из ${admission.limit} ответов.`
          : text.detail;
      return {
        label: text.label,
        detail: busy,
        recovery:
          admission?.retryAfter != null
            ? `Спросите примерно через ${admission.retryAfter} с: набранный вопрос сохранён.`
            : text.recovery,
        question: asked,
      };
    }
    if (status === 401) return failureOf('expired', asked, rawDetail);
    if (status === 403) return failureOf('forbidden', asked, rawDetail);
    // 503 на запросе — это ModelUnavailableError: модель не настроена либо она
    // не ответила. Ни то, ни другое не значит «сервис вообще не отвечает».
    if (status === 503) return modelFailure(rawDetail, asked);
    if (status === 422) return failureOf('malformed', asked, rawDetail);
    if (status >= 500) return failureOf('server', asked, rawDetail);
    return failureOf('rejected', asked, rawDetail);
  }

  function failureFromStream(code: string | undefined, raw: string, asked: string): RunFailure {
    if (code === 'model_unavailable') return modelFailure(raw, asked);
    if (code === 'no_answer') return failureOf('noAnswer', asked, raw);
    return failureOf('server', asked, raw);
  }

  async function run(asked: string): Promise<void> {
    if (running) return;
    question = asked;
    running = true;
    delivered = false;
    failure = null;
    notice = null;
    focusKey = null;
    openClaimId = null;

    controller = new AbortController();
    threadId ??= crypto.randomUUID();
    progressStages = [];
    const signal = controller.signal;

    try {
      const response = await fetch(apiUrl('/api/v1/query/stream'), {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ thread_id: threadId, question: asked, language: 'ru', mode: 'hybrid',
                               web_search_enabled: webSearchEnabled }),
        signal,
      });

      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as {
          detail?: string;
          proxy_unreachable?: boolean;
          active?: number;
          limit?: number;
          retry_after?: number;
        };
        failure = failureFromResponse(
          response.status,
          typeof body.detail === 'string' ? body.detail : '',
          body.proxy_unreachable === true,
          asked,
          { active: body.active, limit: body.limit, retryAfter: body.retry_after },
        );
        return;
      }
      if (!response.body) {
        failure = failureOf('noBody', asked);
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          let event: StreamEvent;
          try {
            event = JSON.parse(line.slice(6)) as StreamEvent;
          } catch {
            continue;
          }
          if (event.type === 'start' && event.question) {
            question = event.question;
          } else if (event.type === 'answer' && event.answer) {
            applyAnswer(event.answer);
          } else if (event.type === 'error') {
            failure = failureFromStream(event.code, event.message ?? '', asked);
          } else if (event.type === 'step' && event.step?.agent) {
            const stage = stageOfNode(event.step.agent)?.label;
            if (stage && progressStages.at(-1) !== stage) progressStages = [...progressStages, stage];
          }
        }
      }

      // Проверка по этому запросу: в `answer` мог лежать прежний ответ,
      // который намеренно не стёрт.
      if (!delivered && !failure) {
        failure = failureOf('stepsNoAnswer', asked);
      }
    } catch (reason) {
      // Остановка по кнопке и оборванное соединение — разные причины: одна
      // говорит про действие человека, другая про канал, и совет у них свой.
      if (signal.aborted) {
        failure = {
          ...RUN_FAILURES.stopped,
          detail: 'Ответ остановлен. Готовый текст не получен.',
          question: asked,
        };
      } else {
        failure = failureOf('dropped', asked, reasonText(reason, ''));
      }
    } finally {
      running = false;
      controller = null;
    }
  }

  function stop(): void {
    controller?.abort();
  }

  // Прежний ответ уходит с экрана только по явному действию человека: сервер не
  // отдаёт список прошлых ответов, и second try чужой работы не вернёт.
  function clearAnswer(): void {
    turns = [];
    threadId = null;
    progressStages = [];
    answer = null;
    question = '';
    failure = null;
    notice = null;
    focusKey = null;
    openClaimId = null;
    historySelection = '';
    historyOpen = false;
  }

  // ── Отзывы, выгрузка, импорт, версии ────────────────────────────────────

  async function importDocument(file: File): Promise<boolean> {
    busy = 'import';
    try {
      const receipt = await api.upload(file);
      // Хвост документа за бюджетом разбора обязано быть помечено: число находок
      // из неполного разбора не выдаётся за полное.
      const tail = receipt.prompt_truncated
        ? ` Разбор дошёл не до конца: без внимания осталось ${receipt.omitted_characters} символов.`
        : '';
      notice = {
        kind: 'ok',
        title: receipt.status === 'duplicate' ? 'Такой документ уже в корпусе' : 'Документ обработан',
        detail: receipt.status === 'duplicate'
          ? 'Второго экземпляра нет: такой файл уже разобран и лежит в корпусе под прежней записью.'
          : `Находок извлечено: ${receipt.extracted_claims}.${tail} Вопрос по новому материалу можно задать сразу.`,
      };
      return true;
    } catch (reason) {
      notice = {
        kind: 'error',
        title: 'Документ не загружен',
        detail: `${reasonText(reason, 'Не удалось принять файл.')} Поддерживаются PDF, DOCX, XLSX, JSON и TXT; закрытые документы загружает аккаунт с экспертным правом, право выдаёт администратор сервиса.`,
      };
      return false;
    } finally {
      busy = null;
    }
  }

  async function loadHistory(claimId: string): Promise<HistoryResult> {
    try {
      return { history: await api.claimHistory(claimId) };
    } catch (reason) {
      return { history: null, error: reasonText(reason, 'История версий не получена.') };
    }
  }

  // Поток переживает переход по маршрутам только пока экран смонтирован.
  $effect(() => {
    return () => {
      controller?.abort();
    };
  });

  $effect(() => {
    if (session.state === 'authenticated' && canAsk && !historyLoaded && !historyLoading && !historyError) {
      void loadAnswerHistory(0);
    }
  });
</script>

<svelte:head>
  <title>Чат — StormIdea</title>
</svelte:head>

<div class="page research">
  <div class="wrap research__head">
    <h1 class="h2">Чат</h1>

    {#if session.signedIn}
      <Button
        class="research__history-toggle"
        variant="quiet"
        size="sm"
        icon="clock"
        expanded={historyOpen}
        controls="chat-history"
        onclick={() => (historyOpen = !historyOpen)}
      >
        {historyOpen ? 'Скрыть историю' : 'Показать историю'}
      </Button>
    {/if}

    {#if !session.signedIn}
      <p class="small research__session">
        {#if session.state === 'unknown'}
          <span class="spinner spinner--quiet" aria-hidden="true"></span>
          Загружаем чат…
        {:else}
          Войдите, чтобы начать разговор.
          <a href={`/login?next=${encodeURIComponent(page.url.pathname)}`}>Войти</a>
        {/if}
      </p>
    {:else if !canAsk}
      <!-- Единственное сообщение об отказе в праве на вопрос: композер ниже
           только показывает заблокированное поле. -->
      <div class="research__blocked">
        <Notice tone="info" title={RUN_FAILURES.forbidden.label}>
          <p>{RUN_FAILURES.forbidden.recovery}</p>
        </Notice>
      </div>
    {/if}
  </div>

  <div class={`wrap wrap--bleed research__workspace ${historyOpen ? 'research__workspace--history-open' : ''}`}>
    {#if session.signedIn}
      <aside
        id="chat-history"
        class="research__history"
        aria-label="История чатов"
        hidden={!historyOpen}
      >
        <div class="research__history-head">
          <div>
            <h2 class="h4">История чатов</h2>
            <p class="micro muted">Вопросы за последние 30 дней</p>
          </div>
          <Button variant="action" size="sm" icon="plus" onclick={clearAnswer}>Новый чат</Button>
        </div>
        {#if historyError}
          <div class="research__history-error" role="status">
            <p class="small">{historyError}</p>
            <Button variant="quiet" size="sm" icon="refresh" onclick={() => void loadAnswerHistory(0)}>
              Повторить
            </Button>
          </div>
        {:else if history.length === 0 && historyLoading}
          <p class="small muted" role="status">Загружаем историю…</p>
        {:else if history.length === 0}
          <Empty title="Здесь будут ваши вопросы" body="Новый вопрос появится в истории после ответа." />
        {:else}
          {#if historySaveError}
            <p class="small research__history-save-error" role="status">{historySaveError}</p>
          {/if}
          <ul class="research__history-list">
            {#each history as item (item.query_id)}
              <li class="research__history-row">
                <button
                  type="button"
                  class={`research__history-item ${historySelection === item.query_id ? 'research__history-item--active' : ''}`}
                  aria-current={historySelection === item.query_id ? 'true' : undefined}
                  disabled={historyLoading}
                  onclick={() => void openSavedAnswer(item)}
                >
                  <span class="research__history-question">{item.question}</span>
                  <time datetime={item.created_at}>
                    {new Intl.DateTimeFormat('ru', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(item.created_at))}
                  </time>
                </button>
                <Button
                  variant="ghost"
                  size="sm"
                  icon={savedConversation === item.query_id ? 'checkCircle' : 'download'}
                  title={savedConversation === item.query_id ? 'Диалог сохранён' : 'Сохранить диалог'}
                  busy={savingConversation === item.query_id}
                  disabled={savingConversation !== null}
                  onclick={() => void saveConversation(item)}
                >
                  <span class="sr-only">
                    {savedConversation === item.query_id ? 'Диалог сохранён' : 'Сохранить диалог'}
                  </span>
                </Button>
              </li>
            {/each}
          </ul>
          {#if historyHasMore}
            <Button
              variant="ghost"
              size="sm"
              busy={historyLoading}
              onclick={() => void loadAnswerHistory(historyOffset)}
            >
              Показать ещё
            </Button>
          {/if}
        {/if}
      </aside>
    {/if}

    <div class="research__conversation">
    <ChatPanel
      {answer}
      {question}
      {running}
      {webSearchEnabled}
      onwebsearchchange={(enabled) => (webSearchEnabled = enabled)}
      {turns}
      {progressStages}
      error={failure}
      {openClaimId}
      {focusKey}
      {busy}
      {notice}
      {seed}
      onask={(value) => void run(value)}
      onclear={clearAnswer}
      onstop={stop}
      onselect={(id) => (openClaimId = id)}
      onfocus={(key) => (focusKey = key)}
      onimport={importDocument}
      onnotice={(value) => (notice = value)}
      onhistory={loadHistory}
    />
    </div>
  </div>
</div>

<style>
  .research {
    display: flex;
    flex-direction: column;
    min-height: calc(100dvh - var(--topbar-h) - var(--s4));
    padding-bottom: var(--s4);
  }

  .research__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s3);
    padding-top: var(--s4);
    padding-bottom: var(--s3);
  }

  .research__history-toggle {
    display: inline-flex;
  }

  .research__workspace {
    display: grid;
    flex: 1;
    min-height: max(26rem, calc(100dvh - var(--topbar-h) - var(--s10) - var(--s6)));
    grid-template-columns: minmax(0, 1fr);
    gap: var(--s5);
    align-items: stretch;
  }

  .research__workspace--history-open {
    grid-template-columns: minmax(15rem, 18rem) minmax(0, 1fr);
  }

  .research__history {
    min-width: 0;
    padding: var(--s4);
    border-radius: var(--r-md);
    background: var(--sage);
  }

  .research__history-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--s3);
    margin-bottom: var(--s4);
  }

  .research__history-head h2,
  .research__history-head p {
    margin: 0;
  }

  .research__history-head p {
    margin-top: var(--s1);
  }

  .research__history-list {
    display: grid;
    gap: var(--s1);
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .research__history-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--s1);
    border-top: 1px solid var(--line-soft);
  }

  .research__history-item {
    display: grid;
    width: 100%;
    gap: var(--s1);
    padding: var(--s3);
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--ink);
    font: inherit;
    text-align: start;
    cursor: pointer;
  }

  .research__history-question {
    display: -webkit-box;
    overflow: hidden;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }

  .research__history-item:hover,
  .research__history-item--active {
    background: var(--surface-raised);
  }

  .research__history-item:focus-visible {
    outline: 2px solid var(--action-ink);
    outline-offset: 2px;
  }

  .research__history-item span {
    min-width: 0;
  }

  .research__history-item time {
    color: var(--ink-3);
    font-size: var(--t-micro);
  }

  .research__conversation {
    flex: 1;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  .research__conversation :global(.ask__flow) {
    align-items: center;
  }

  .research__conversation :global(.paper),
  .research__conversation :global(.chat__pending) {
    width: min(100%, var(--maxw-narrow));
    align-self: center;
  }

  .research__history-save-error {
    color: var(--disputed);
  }

  .research__conversation :global(.ask) {
    flex: 1;
    min-height: 100%;
  }

  .research__conversation :global(.prompt__lead) {
    display: grid;
  }

  @media (max-width: 720px) {
    .research__workspace {
      display: flex;
      flex-direction: column;
      gap: var(--s3);
    }

    .research__history {
      width: 100%;
      max-height: min(50dvh, 38rem);
      overflow: auto;
    }

    .research__history-row {
      grid-template-columns: minmax(0, 1fr);
      padding-block: var(--s1);
    }

    .research__history-row :global(.btn) {
      justify-self: start;
    }

    .research__head {
      justify-content: space-between;
    }
  }

  .research__session {
    display: flex;
    align-items: center;
    gap: var(--s2);
    margin-bottom: var(--s3);
  }

  .research__blocked {
    max-width: var(--maxw-measure);
    margin-bottom: var(--s5);
  }
</style>
