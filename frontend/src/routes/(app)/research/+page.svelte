<script lang="ts">
  /**
   * Рабочий экран агента: единственный транспорт — SSE поверх прокси
   * `/backend`. Компонент `ChatPanel` показывает данные и собирает формы, все
   * вызовы остаются здесь: ответ сервера не подменяется заглушкой ни в одном
   * состоянии. Права читаются из подтверждённой сессии (`session.can`),
   * заголовков роли клиент не отправляет.
   */
  import { api, apiUrl } from '$lib/api';
  import ChatPanel, {
    type HistoryResult,
    type RunFailure,
    type SheetNotice,
  } from '$lib/ChatPanel.svelte';
  import { num } from '$lib/format';
  import Notice from '$lib/ui/Notice.svelte';
  import { page } from '$app/state';
  import { session } from '$lib/sessionStore.svelte';
  import { FAILURE_WORDS, RUN_FAILURES } from '$lib/terms';
  import type { RunFailureKind } from '$lib/terms';
  import type { AnswerPayload } from '$lib/types';

  interface StreamEvent {
    type: string;
    question?: string;
    answer?: AnswerPayload;
    code?: string;
    message?: string;
  }

  // Вопрос со входа в раздел (/research?q=…): читаем один раз при открытии
  // экрана; экран подставляет его в поле и запускает ответ сам.
  const seed = new URLSearchParams(page.url.search).get('q')?.trim() ?? '';

  let question = $state('');
  let answer = $state<AnswerPayload | null>(null);
  let running = $state(false);
  // Новый вопрос не стирает прежний ответ: серверного перечня прошлых ответов
  // нет — api.ts знает только query/stream, export по query_id и claimHistory.
  // Молча потерять единственный собранный ответ значит потерять работу, поэтому
  // прежний ответ сходит с экрана, только когда пришёл новый или когда человек
  // убрал его сам кнопкой.
  let delivered = false;
  let failure = $state<RunFailure | null>(null);
  // Отдельное состояние для эталонных вопросов: пустой набор и непрогруженный
  // канал — разные экраны, и «повторить» должно чинить именно канал.
  let openClaimId = $state<string | null>(null);
  let focusKey = $state<string | null>(null);
  let busy = $state<'feedback' | 'export' | 'import' | null>(null);
  let notice = $state<SheetNotice | null>(null);

  let controller: AbortController | null = null;

  const canAsk = $derived(session.can('query:ask'));

  // ── Формат ──────────────────────────────────────────────────────────────
  // Числа и длительности — через общие `num` и `duration` из `$lib/format`:
  // локальная копия расходилась с остальными экранами в разрядах и показывала
  // «1,234» там, где другие давали «1,23».

  function reasonText(reason: unknown, fallback: string): string {
    if (reason instanceof TypeError) return fallback;
    const message = reason instanceof Error ? reason.message : '';
    return message || fallback;
  }

  // ── Сборка ответа: SSE поверх прокси /backend ────────────────────────────

  function applyAnswer(payload: AnswerPayload): void {
    delivered = true;
    answer = payload;
    question = payload.question;
    openClaimId = payload.findings[0]?.id ?? null;
    focusKey = null;
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
    const signal = controller.signal;

    try {
      const response = await fetch(apiUrl('/api/v1/query/stream'), {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: asked, language: 'ru', mode: 'hybrid' }),
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
    answer = null;
    question = '';
    failure = null;
    notice = null;
    focusKey = null;
    openClaimId = null;
  }

  // ── Отзывы, выгрузка, импорт, версии ────────────────────────────────────

  async function exportAnswer(format: 'markdown' | 'json-ld'): Promise<boolean> {
    // В файл уходит серверная копия ответа по query_id: присланный клиентом
    // AnswerPayload сервер не принимает — иначе фильтровались бы клиентские данные.
    if (!answer) return false;
    busy = 'export';
    try {
      const response = await api.export(answer.query_id, format);
      const blob = await response.blob();
      const href = URL.createObjectURL(blob);
      // Идентификатор ответа живёт в имени файла, а не в строке на экране:
      // Пользователь видит сообщение о результате, а не внутренние поля ответа.
      const filename = `klubok-${answer.query_id || 'otvet'}.${format === 'markdown' ? 'md' : 'jsonld'}`;
      const link = document.createElement('a');
      link.href = href;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(href);
      notice = {
        kind: 'ok',
        title: 'Файл выгружен',
        detail: `${format === 'markdown' ? 'Markdown' : 'JSON-LD'}, ${num(Math.round(blob.size / 1024))} КиБ: тот же ответ, что на экране.`,
      };
      return true;
    } catch (reason) {
      notice = {
        kind: 'error',
        title: 'Выгрузка не получилась',
        detail: `${reasonText(reason, 'Не удалось получить файл.')} Попробуйте отправить выгрузку ещё раз.`,
      };
      return false;
    } finally {
      busy = null;
    }
  }

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
</script>

<svelte:head>
  <title>Чат — StormIdea</title>
</svelte:head>

<div class="page research">
  <div class="wrap research__head">
    <h1 class="h2">Чат</h1>

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

  <!-- Поток и композер начинаются у левого края одной рабочей колонки. -->
  <div class="wrap wrap--bleed">
    <ChatPanel
      {answer}
      {question}
      {running}
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
      onexport={exportAnswer}
      onimport={importDocument}
      onnotice={(value) => (notice = value)}
      onhistory={loadHistory}
    />
  </div>
</div>

<style>
  .research {
    padding-bottom: var(--s5);
  }

  .research__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s3);
    padding-top: var(--s4);
    padding-bottom: var(--s3);
  }

  .research > .wrap--bleed :global(.ask) {
    max-width: 58rem;
    margin-inline: 0;
  }

  .research > .wrap--bleed :global(.ask__flow),
  .research > .wrap--bleed :global(.ask__composer) {
    width: 100%;
    max-width: 58rem;
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
