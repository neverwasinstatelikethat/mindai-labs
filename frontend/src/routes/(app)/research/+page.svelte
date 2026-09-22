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
  // экрана; лист подставляет его в поле и запускает проход сам.
  const seed = new URLSearchParams(page.url.search).get('q')?.trim() ?? '';

  let question = $state('');
  let answer = $state<AnswerPayload | null>(null);
  let running = $state(false);
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

  // ── Проход: SSE поверх прокси /backend ───────────────────────────────────

  function stopTimer(): void {
    if (ticker) clearInterval(ticker);
    ticker = null;
  }

  function applyAnswer(payload: AnswerPayload): void {
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
    return OPS_DETAIL.test(detail) ? '' : detail.trim();
  }

  // 429 приходит с телом: сколько прогонов идёт, каков предел и через сколько
  // секунд попробовать. Числа показывает только те, что назвал сервис.
  interface Admission {
    active?: number;
    limit?: number;
    retryAfter?: number;
  }

  function failureFromResponse(
    status: number,
    rawDetail: string,
    unreachable: boolean,
    asked: string,
    admission: Admission | null = null,
  ): RunFailure {
    const detail = humanDetail(rawDetail);
    if (unreachable) {
      return {
        label: 'Запрос не дошёл до сервиса',
        detail: detail || 'Не удалось связаться с сервисом знаний.',
        recovery: 'Проверьте соединение и повторите вопрос — набранный текст остаётся в поле.',
        question: asked,
      };
    }
    if (status === 429) {
      const busy =
        admission?.active != null && admission.limit != null
          ? `Сейчас сервис ведёт ${admission.active} из ${admission.limit} прогонов.`
          : 'Все прогоны заняты, и новый пока некуда поставить.';
      return {
        label: 'Сервис занят: вопрос не начал обрабатываться',
        detail: `${busy} Формулировка ни при чём — ничего менять не нужно.`,
        recovery:
          admission?.retryAfter != null
            ? `Повторите примерно через ${admission.retryAfter} с: вопрос остаётся в поле.`
            : 'Повторите через минуту: вопрос остаётся в поле.',
        question: asked,
      };
    }
    if (status === 401) {
      return {
        label: 'Доступ истёк',
        detail: detail || 'Вход был завершён или истёк, и запрос не запустился.',
        recovery: 'Войдите заново и повторите вопрос.',
        question: asked,
        login: true,
      };
    }
    if (status === 403) {
      return {
        label: 'Запрос недоступен',
        detail: detail || 'Для этого аккаунта запрос к корпусу не разрешён.',
        recovery:
          'Находки корпуса и карта связей работают и без запроса: они собраны по уже извлечённым фактам.',
        question: asked,
      };
    }
    if (status === 503) {
      return {
        label: 'Сервис сейчас не отвечает',
        detail: detail || 'Собирать ответ по этому запросу пока нечем.',
        recovery: 'Повторите вопрос чуть позже или уточните формулировку.',
        question: asked,
      };
    }
    if (status === 422) {
      return {
        label: 'Вопрос не принят',
        detail: detail || 'Формулировка не подошла: вопрос обязан быть не короче трёх символов.',
        recovery: 'Уточните формулировку и повторите запрос.',
        question: asked,
      };
    }
    if (status >= 500) {
      return {
        label: 'Проход не завершился',
        detail: detail || 'Рабочий процесс не дошёл до ответа.',
        recovery: 'Повторите вопрос; если повтор снова даёт сбой — упростите формулировку.',
        question: asked,
      };
    }
    return {
      label: 'Запрос отклонён',
      detail: detail || 'Причину сервис не назвал.',
      recovery: 'Проверьте формулировку вопроса и повторите запрос.',
      question: asked,
    };
  }

  function failureFromStream(code: string | undefined, raw: string, asked: string): RunFailure {
    const detail = humanDetail(raw);
    if (code === 'model_unavailable') return failureFromResponse(503, raw, false, asked);
    if (code === 'no_answer') {
      return {
        label: 'Ответ не пришёл',
        detail: detail || 'Проход завершился без ответа.',
        recovery: 'Уточните формулировку или повторите запрос: след прохода ниже показывает, где всё оборвалось.',
        question: asked,
      };
    }
    return {
      label: 'Проход оборвался',
      detail: detail || 'Рабочий процесс остановился на середине.',
      recovery: 'Повторите запрос или упростите вопрос — лимиты видно в причине неполного ответа.',
      question: asked,
    };
  }

  async function run(asked: string): Promise<void> {
    if (running) return;
    question = asked;
    running = true;
    answer = null;
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
        failure = {
          label: 'Ответ не пришёл',
          detail: 'Соединение открылось, но данных ответа не дало.',
          recovery: 'Повторите запрос — набранный вопрос остаётся в поле.',
          question: asked,
        };
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

      if (!answer && !failure) {
        failure = {
          label: 'Ответ не пришёл',
          detail: 'Шаги прохода были, а готового ответа — нет.',
          recovery: 'Повторите запрос: возможно, исчерпан лимит одного вопроса. Тогда снимите часть условий.',
          question: asked,
        };
      }
    } catch (reason) {
      if (signal.aborted) {
        failure = {
          label: 'Проход остановлен',
          detail: `Вы остановили проход на ${seconds(elapsedMs)}: ответа в листе нет.`,
          recovery: 'Повторите вопрос кнопкой ниже — незавершённый проход не сохраняется.',
          question: asked,
        };
      } else {
        failure = {
          label: 'Сервис не отвечает',
          detail: reasonText(reason, 'Не удалось связаться с сервисом знаний.'),
          recovery: 'Проверьте соединение и повторите запрос.',
          question: asked,
        };
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
        detail: `Тезис получил${version != null ? ` версию ${version}` : ' новую версию'}; ${proposalLine}. Решение видно в разделе «Проверка решений» — правку не отменить отсюда.`,
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
        detail: `${filename} — ${num(Math.round(blob.size / 1024))} КиБ, тот же ответ, что на листе.`,
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

  // ── Показания корпуса и эталонные вопросы при входе на экран ─────────────

  async function loadExamples(): Promise<void> {
    examplesState = 'loading';
    try {
      const gold = await api.goldCases();
      // Три эталонных вопроса — подсказка, а не витрина: весь перечень остаётся
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
      eyebrow="Рабочее пространство · запрос"
      title="Проход по графу доказательств"
      lead="Вопрос собирается в намерение, план, обход графа и ответ. Каждый тезис ответа остаётся привязан к цитате и локатору: страница, лист, диапазон ячеек, смещение символов."
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
        <Notice tone="info" title="Запрос корпуса вам недоступен">
          <p>
            Спрашивать граф доказательств может аккаунт с доступом к запросам. Находки корпуса и
            карта связей работают и без него: они собраны по уже извлечённым фактам.
          </p>
        </Notice>
      </div>
    {/if}
  </div>

  <!-- Рабочий лист и композер занимают всю ширину: читается мера колонки,
       а не поля вокруг неё. -->
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
