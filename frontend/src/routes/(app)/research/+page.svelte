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
    type TrailStep,
  } from '$lib/ChatPanel.svelte';
  import { num } from '$lib/format';
  import Notice from '$lib/ui/Notice.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import { page } from '$app/state';
  import { session } from '$lib/sessionStore.svelte';
  import { FAILURE_WORDS, RESEARCH_HEAD, RUN_FAILURES } from '$lib/terms';
  import type { RunFailureKind } from '$lib/terms';
  import type { AgentEvent, AnswerPayload, CorpusStats } from '$lib/types';

  // Поток отдаёт шаг как есть; отсутствие поля — это отсутствие данных, а не
  // «completed», «узел» и ноль миллисекунд.
  interface StreamStep {
    agent?: string | null;
    status?: string | null;
    message?: string | null;
    duration_ms?: number | null;
  }

  interface StreamEvent {
    type: string;
    question?: string;
    step?: StreamStep;
    answer?: AnswerPayload;
    code?: string;
    message?: string;
  }

  const STEP_STATES: readonly AgentEvent['status'][] = ['started', 'completed', 'revised', 'failed'];

  function realStatus(value: string | null | undefined): AgentEvent['status'] | null {
    return STEP_STATES.find((state) => state === value) ?? null;
  }

  function normalizeStep(step: StreamStep): TrailStep {
    const duration = step.duration_ms;
    return {
      agent: step.agent?.trim() || null,
      status: realStatus(step.status),
      message: step.message?.trim() || null,
      duration_ms: typeof duration === 'number' && Number.isFinite(duration) ? duration : null,
    };
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
  let steps = $state<TrailStep[]>([]);
  let elapsedMs = $state(0);
  let failure = $state<RunFailure | null>(null);
  let corpus = $state<CorpusStats | null>(null);
  let corpusState = $state<'loading' | 'ready' | 'error'>('loading');
  let examples = $state<string[]>([]);
  // Отдельное состояние для эталонных вопросов: пустой набор и непрогруженный
  // канал — разные экраны, и «повторить» должно чинить именно канал.
  let examplesState = $state<'loading' | 'ready' | 'error'>('loading');
  let openClaimId = $state<string | null>(null);
  let focusKey = $state<string | null>(null);
  let busy = $state<'correction' | 'feedback' | 'export' | 'import' | null>(null);
  let notice = $state<SheetNotice | null>(null);

  let controller: AbortController | null = null;
  let ticker: ReturnType<typeof setInterval> | null = null;
  let startedAt = 0;

  const canAsk = $derived(session.can('query:ask'));

  // ── Формат ──────────────────────────────────────────────────────────────
  // Числа — через общий `num` из `$lib/format`: локальная копия расходилась с
  // остальными экранами в разрядах и показывала «1,234» там, где другие давали
  // «1,23».

  function seconds(ms: number): string {
    return `${(ms / 1000).toFixed(1)} с`;
  }

  function shortCode(value: string | null | undefined, size = 8): string {
    if (!value) return '—';
    return value.slice(0, size);
  }

  function reasonText(reason: unknown, fallback: string): string {
    if (reason instanceof TypeError) return fallback;
    const message = reason instanceof Error ? reason.message : '';
    return message || fallback;
  }

  // ── Сборка ответа: SSE поверх прокси /backend ────────────────────────────

  function stopTimer(): void {
    if (ticker) clearInterval(ticker);
    ticker = null;
  }

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
          ? `Сейчас сервис собирает ${admission.active} из ${admission.limit} возможных ответов.`
          : text.detail;
      return {
        label: text.label,
        detail: `${busy} Формулировка ни при чём — ничего менять не нужно.`,
        recovery:
          admission?.retryAfter != null
            ? `Повторите примерно через ${admission.retryAfter} с: вопрос останется в поле.`
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
    steps = [];
    failure = null;
    notice = null;
    focusKey = null;
    openClaimId = null;
    elapsedMs = 0;
    startedAt = Date.now();
    stopTimer();
    ticker = setInterval(() => {
      elapsedMs = Date.now() - startedAt;
    }, 100);

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
          } else if (event.type === 'step' && event.step) {
            steps = [...steps, normalizeStep(event.step)];
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
          detail: `Вы остановили сборку ответа на ${seconds(elapsedMs)}: готового текста нет.`,
          question: asked,
        };
      } else {
        failure = failureOf('dropped', asked, reasonText(reason, ''));
      }
    } finally {
      stopTimer();
      elapsedMs = Date.now() - startedAt;
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
    steps = [];
    failure = null;
    notice = null;
    focusKey = null;
    openClaimId = null;
    elapsedMs = 0;
  }

  // ── Правки, отзывы, выгрузка, импорт, версии ────────────────────────────

  async function sendCorrection(input: {
    finding: AnswerPayload['findings'][number];
    comment: string;
    correction: string;
  }): Promise<boolean> {
    if (!answer) return false;
    busy = 'correction';
    try {
      // finding_id обязателен: supersede срабатывает только при
      // verdict='correct' вместе с finding_id и correction.
      const result = await api.feedback({
        query_id: answer.query_id,
        finding_id: input.finding.id,
        verdict: 'correct',
        comment: input.comment,
        correction: input.correction,
      });
      // Предложение генерирует модель: без живого LLM правка всё равно записана,
      // и об этом надо сказать прямо, а не ссылаться на несуществующее предложение.
      const version = result.superseded?.version;
      const proposalLine = result.proposal
        ? 'предложение на проверку поставлено'
        : 'модель предложение не сформировала — записано только решение';
      notice = {
        kind: 'ok',
        title: 'Правка принята',
        detail: `Тезис получил${version != null ? ` версию ${version}` : ' новую версию'}; ${proposalLine}. Решение видно в разделе «Проверка решений» — отменить правку отсюда нельзя.`,
      };
      return true;
    } catch (reason) {
      notice = {
        kind: 'error',
        title: 'Правка не отправлена',
        detail: `${reasonText(reason, 'Не удалось отправить правку.')} Введённый текст сохранён: отправьте правку ещё раз, если сервис не принял её.`,
      };
      return false;
    } finally {
      busy = null;
    }
  }

  async function sendFeedback(input: {
    verdict: 'accept' | 'reject';
    comment: string;
  }): Promise<boolean> {
    if (!answer) return false;
    busy = 'feedback';
    try {
      const result = await api.feedback({
        query_id: answer.query_id,
        finding_id: null,
        verdict: input.verdict,
        comment: input.comment,
      });
      notice = {
        kind: 'ok',
        title: 'Отзыв записан',
        detail: result.proposal
          ? 'Отзыв сохранён, предложение поставлено на проверку.'
          : 'Отзыв сохранён, но предложение не сформировано: решение записано без него.',
      };
      return true;
    } catch (reason) {
      notice = {
        kind: 'error',
        title: 'Отзыв не отправлен',
        detail: `${reasonText(reason, 'Не удалось отправить отзыв.')} Введённый текст сохранён — повторите отправку.`,
      };
      return false;
    } finally {
      busy = null;
    }
  }

  async function exportAnswer(format: 'markdown' | 'json-ld'): Promise<boolean> {
    // В файл уходит серверная копия ответа по query_id: присланный клиентом
    // AnswerPayload сервер не принимает — иначе фильтровались бы клиентские данные.
    if (!answer) return false;
    busy = 'export';
    try {
      const response = await api.export(answer.query_id, format);
      const blob = await response.blob();
      const href = URL.createObjectURL(blob);
      const filename = `klubok-${shortCode(answer.query_id)}.${format === 'markdown' ? 'md' : 'jsonld'}`;
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
        detail: `${filename} — ${num(Math.round(blob.size / 1024))} КиБ, тот же ответ, что на экране.`,
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
      // Показания корпуса опрашиваем отдельно: отказ второго вызова не должен
      // притворяться отказом импорта — документ сервер уже принял.
      try {
        corpus = await api.corpusStats();
        corpusState = 'ready';
      } catch {
        corpusState = 'error';
      }
      // Хвост документа за бюджетом промпта — не «в корпусе такого нет»: число
      // утверждений из неполного разбора обязано быть помечено.
      const tail = receipt.prompt_truncated
        ? ` Разбор дошёл не до конца: без внимания осталось ${receipt.omitted_characters} символов.`
        : '';
      notice = {
        kind: 'ok',
        title: receipt.status === 'duplicate' ? 'Такой документ уже в корпусе' : 'Документ обработан',
        detail: receipt.status === 'duplicate'
          ? 'Второго экземпляра нет: такой файл уже разобран и лежит в корпусе под прежней записью.'
          : `Извлечено утверждений: ${receipt.extracted_claims}.${tail} Вопрос по новому материалу можно задать сразу.`,
      };
      return true;
    } catch (reason) {
      notice = {
        kind: 'error',
        title: 'Документ не загружен',
        detail: `${reasonText(reason, 'Не удалось принять файл.')} Поддерживаются PDF, DOCX, XLSX, JSON и TXT; закрытые документы требуют расширенного доступа.`,
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

  // ── Показания корпуса и готовые вопросы при входе на экран ───────────────

  async function loadExamples(): Promise<void> {
    examplesState = 'loading';
    try {
      const gold = await api.goldCases();
      // Три готовых вопроса — подсказка, а не витрина: весь перечень остаётся
      // в разделе оценки.
      examples = gold.slice(0, 3).map((item) => item.question);
      examplesState = 'ready';
    } catch {
      examples = [];
      examplesState = 'error';
    }
  }

  async function preflight(): Promise<void> {
    const [statsResult] = await Promise.allSettled([api.corpusStats(), loadExamples()]);
    if (statsResult.status === 'fulfilled') {
      corpus = statsResult.value;
      corpusState = 'ready';
    } else {
      corpus = null;
      corpusState = 'error';
    }
  }

  $effect(() => {
    void preflight();
  });

  // Поток переживает переход по маршрутам только пока экран смонтирован.
  $effect(() => {
    return () => {
      stopTimer();
      controller?.abort();
    };
  });
</script>

<svelte:head>
  <title>Запрос — Научный Клубок</title>
</svelte:head>

<div class="page research">
  <div class="wrap">
    <SectionHead
      level="1"
      eyebrow={RESEARCH_HEAD.eyebrow}
      title={RESEARCH_HEAD.title}
      lead={RESEARCH_HEAD.lead}
    />

    {#if !session.signedIn}
      <p class="micro research__session">
        {#if session.state === 'unknown'}
          <span class="spinner spinner--quiet" aria-hidden="true"></span>
          проверяем доступ…
        {:else}
          доступа нет — запрос не запустится.
          <a href={`/login?next=${encodeURIComponent(page.url.pathname)}`}>Войти</a>
        {/if}
      </p>
    {:else if !canAsk}
      <div class="research__blocked">
        <Notice tone="info" title={RUN_FAILURES.forbidden.label}>
          <p>
            Спрашивать корпус может аккаунт с доступом к запросам. Находки корпуса и карта связей
            работают и без него: они собраны по уже извлечённым фактам.
          </p>
        </Notice>
      </div>
    {/if}
  </div>

  <!-- Ответ и композер занимают всю ширину: читается мера колонки, а не поля
       вокруг неё. -->
  <div class="wrap wrap--bleed">
    <ChatPanel
      {answer}
      {question}
      {running}
      {steps}
      {elapsedMs}
      error={failure}
      {corpus}
      {corpusState}
      {examples}
      {examplesState}
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
      oncorrection={sendCorrection}
      onfeedback={sendFeedback}
      onexport={exportAnswer}
      onimport={importDocument}
      onnotice={(value) => (notice = value)}
      onhistory={loadHistory}
      onexamples={loadExamples}
    />
  </div>
</div>

<style>
  .research {
    /* Композер прилип ко дну листа, поэтому нижнее поле не нужно:
       last-child решают отступы самой ленты. */
    padding-bottom: var(--s5);
  }

  .research > .wrap {
    padding-top: var(--s4);
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
