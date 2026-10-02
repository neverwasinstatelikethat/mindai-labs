<script lang="ts">
  // Отзыв на ответ: по ответу системы решают, принять его, отклонить или попросить
  // правку, и поясняют решение комментарием. Разбор предложений модели с замером
  // на одних и тех же проверочных вопросах и правка самого утверждения доступны
  // эксперту: без права экран объясняет, что именно не открыто, а не показывает
  // мёртвую кнопку, и в обычной прокрутке экспертных строк не стоит.
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { api, ApiError } from '$lib/api';
  import { navLabel } from '$lib/nav';
  import { dateTime, num, pct } from '$lib/format';
  import { session } from '$lib/sessionStore.svelte';
  import {
    AB_METRIC_LABELS,
    AB_STRINGS,
    FEEDBACK_FEED,
    FEEDBACK_GATE,
    FEEDBACK_HEAD,
    FEEDBACK_VERDICTS,
    PREDICATE_LABELS,
    PROPOSAL_KIND_FALLBACK,
    PROPOSAL_KIND_LABELS,
    PROPOSAL_STATUS_LABELS,
    RUN_METRIC_LABELS,
    STATUS_SHORT,
    feedShownOf,
    knownTerm,
    proposalsCappedNote,
    SUBJECT_LABELS,
    termOf,
  } from '$lib/terms';
  import {
    type ClaimHistory,
    type EvaluationRun,
    type EvolutionExperiment,
    type EvolutionProposal,
    type FindingListItem,
  } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Field from '$lib/ui/Field.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import Sheet from '$lib/ui/Sheet.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';

  // Журнал предложений порциями: список растёт вместе с корпусом, и 500
  // предложений не должны становиться одним бесконечным списком.
  const PROPOSAL_PAGE_SIZE = 10;
  // Прогоны оценки приходят пачкой до нескольких сотен — разбиваем и их.
  const RUNS_PAGE_SIZE = 8;
  // Durable-ленты читаются серверным окном (`limit` + `offset`), полное число
  // подходит в X-Total-Count: размер страницы и полный объём журнала экран
  // называет раздельно, молчаливого среза нет.
  const RUNS_FEED_SIZE = 200;
  const EXPERIMENTS_FEED_SIZE = 50;
  // Журнал предложений приходит массивом без полного числа, а потолок одной
  // загрузки — 200: экран называет его сам, иначе список на 500 предложений
  // обрывается на 200 молча. Знаменатель даст оркестратор, когда переведёт
  // маршрут на `requestWithTotal` (см. комментарий в `api.ts`).
  const PROPOSAL_READ_SIZE = 200;

  // Существительное счётчик берёт с экрана: раздел называет строки списка
  // проверок «проверками ответа», а журнал предложений «предложениями модели».
  // Служебное имя ленты из словаря звало бы те же строки иначе, чем весь
  // остальной экран, и человек искал бы в списке то, чего в списке нет.
  const RUN_NOUNS = { one: 'проверка ответа', few: 'проверки ответа', many: 'проверок ответа' };
  const PROPOSAL_NOUNS = {
    one: 'предложение модели',
    few: 'предложения модели',
    many: 'предложений модели',
  };

  // Статус предложения показываем тем же языком статусов, что и утверждения:
  // ждёт решения — гипотеза, принято — консенсус, отклонено — заменённая версия.
  const PROPOSAL_TONE: Record<EvolutionProposal['status'], 'hypothesis' | 'consensus' | 'superseded'> = {
    proposed: 'hypothesis',
    accepted: 'consensus',
    rejected: 'superseded',
  };

  // Классы отказа различаем, чтобы подобрать верную формулировку и тон плашки,
  // но человеку показываем что не получилось и что нажать — без кодов ответа.
  type Failure = { kind: 'model' | 'backend' | 'denied' | 'conflict' | 'other'; text: string };
  // FeedbackRequest.query_id — UUID без исключения. Свободный комментарий,
  // который не указывает на конкретный ответ, уходит с nil-UUID: сервер его
  // принимает, и в журнале предложений он читается как «не привязан».
  const NIL_QUERY_ID = '00000000-0000-0000-0000-000000000000';

  // Ответ, на который пишут отзыв: проверка ответа из журнала либо вывод,
  // принесённый из «Сравнения» (свободный комментарий). Вопрос корпусу задают в
  // разделе «Вопрос», а не здесь.
  type Target = {
    queryId: string;
    provenance: string;
    createdAt?: string;
    evaluation?: EvaluationRun;
  };

  // Ответ на отзыв раскладывается по полочкам: решение эксперта (superseded)
  // и предложение от модели — разные по надёжности части, плюс явный канал
  // неполноты ответа.
  interface FeedbackResult {
    proposal: EvolutionProposal | null;
    superseded: FindingListItem | null;
    degradation: string[];
  }

  function looksLikeProposal(value: unknown): value is EvolutionProposal {
    const row = value as Record<string, unknown> | null;
    return typeof row === 'object' && row !== null && typeof row.id === 'string' && typeof row.title === 'string';
  }

  function looksLikeFinding(value: unknown): value is FindingListItem {
    const row = value as Record<string, unknown> | null;
    return (
      typeof row === 'object' &&
      row !== null &&
      typeof row.statement === 'string' &&
      Array.isArray(row.observations)
    );
  }

  function readFeedbackResult(raw: unknown): FeedbackResult {
    const body = (raw ?? {}) as Record<string, unknown>;
    const proposal = looksLikeProposal(body.proposal)
      ? body.proposal
      : looksLikeProposal(raw)
        ? (raw as EvolutionProposal)
        : null;
    return {
      proposal,
      superseded: looksLikeFinding(body.superseded) ? body.superseded : null,
      degradation: Array.isArray(body.degradation_reasons)
        ? body.degradation_reasons.filter((line): line is string => typeof line === 'string')
        : [],
    };
  }

  function classify(reason: unknown): Failure {
    const text = reason instanceof Error ? reason.message : String(reason);
    if (reason instanceof ApiError && reason.status === 403) return { kind: 'denied', text };
    if (reason instanceof ApiError && reason.status === 409) return { kind: 'conflict', text };
    if (/LLM не настроен|GigaChat/i.test(text)) return { kind: 'model', text };
    if (/Бэкенд недоступен|Бэкенд не ответил|HTTP 50[23]/i.test(text)) return { kind: 'backend', text };
    return { kind: 'other', text };
  }

  /** Сбой по-человечески: что не вышло и одно действие. Служебный текст отказа
   *  остаётся в «Служебных данных»: кодов ответов и имён сервисов здесь нет.
   *  Фразы нейтральны к тому, читали мы данные или записывали: функция общая. */
  function failureText(failure: Failure): string {
    if (failure.kind === 'denied') return FEEDBACK_GATE;
    if (failure.kind === 'conflict') {
      return 'То самое утверждение уже заменили новой версией. Обновите находки и выберите актуальную версию.';
    }
    if (failure.kind === 'model') {
      return 'Модель не ответила, и запись не состоялась. Повторите попытку позже.';
    }
    if (failure.kind === 'backend') return 'Данные не пришли. Проверьте соединение и повторите попытку.';
    return 'Сервис не ответил. Проверьте соединение и повторите попытку.';
  }

  const canGive = $derived(session.can('feedback:give'));
  const canRead = $derived(session.can('knowledge:read'));
  const canEvaluate = $derived(session.can('evaluation:view'));
  const canReview = $derived(session.can('proposal:review'));
  const canRestricted = $derived(session.can('restricted:read'));

  let phase = $state<'loading' | 'ready' | 'failed'>('loading');
  let failure = $state<Failure | null>(null);

  let runs = $state<EvaluationRun[]>([]);
  let runsFailure = $state('');
  // Сколько прогонов показано и сколько их всего по сведениям сервера.
  let runsTotal = $state<number | null>(null);
  let runsOffset = $state(0);
  let runsMoreLoading = $state(false);
  let runsMoreError = $state('');
  // Пустая страница при положительном остатке: смещение не сдвинулось, показывать
  // больше нечего, поэтому кнопка осталась бы пустым действием.
  let runsStalled = $state(false);
  let runsSeq = 0;
  let proposals = $state<EvolutionProposal[]>([]);
  // Сколько предложений открыто: список растёт действием читателя, а не
  // молчаливой прокруткой всего журнала.
  let proposalLimit = $state(PROPOSAL_PAGE_SIZE);
  let runsShown = $state(RUNS_PAGE_SIZE);
  let experiments = $state<EvolutionExperiment[]>([]);
  let experimentsTotal = $state<number | null>(null);
  let experimentsOffset = $state(0);
  let experimentsMoreLoading = $state(false);
  let experimentsMoreError = $state('');
  // Тот же тупик, что у списка прогонов: пустая страница при остатке не двигает
  // смещение, и кнопка осталась бы пустым действием.
  let experimentsStalled = $state(false);
  let experimentsSeq = 0;
  // Сбой чтения журнала не маскируем пустотой: у каждого своя строка.
  let experimentsFailure = $state('');
  let corpus = $state<FindingListItem[]>([]);
  let corpusFailure = $state('');
  // Частичный сбой чтения: часть журналов прочитана, часть нет. Это отдельная
  // строка, а не молчаливая пустота в секции.
  let softFailure = $state('');

  let target = $state<Target | null>(null);

  let verdict = $state<'accept' | 'reject' | 'correct' | null>(null);
  let comment = $state('');
  let findingId = $state('');
  let findingQuery = $state('');
  let correction = $state('');
  let submitting = $state(false);
  // Attempt, а не «форма невалидна»: претензии показываются после попытки,
  // а не как выговор за ещё не тронутую форму.
  let submitAttempted = $state(false);
  let result = $state<FeedbackResult | null>(null);
  let submitFailure = $state<Failure | null>(null);

  let reviewingId = $state('');
  let reviewFailure = $state('');
  let reviewNotice = $state('');
  let runningId = $state('');
  let experimentFailure = $state('');
  let experimentNotice = $state('');

  // Список прогонов под раскрытием: форма отзыва остаётся в первом экране, а
  // журнал подключается по одному нажатию.
  let runsOpen = $state(false);

  // История версий утверждения живёт в отдельной шторке: цепочка версий по id находки.
  let historyOpen = $state(false);
  let historyBusy = $state(false);
  // Утверждение, чья цепочка открыта: шторка обязана называть свой предмет и
  // тогда, когда запрос версий не прошёл: данных истории в этот момент нет.
  let historyClaim = $state('');
  // Человекочитаемая формулировка того же утверждения: UUID остаётся подписью,
  // а не именем предмета.
  let historyClaimText = $state('');
  let history = $state<ClaimHistory | null>(null);
  let historyFailure = $state('');
  // Утверждение, правку которого только что записали: по нему и открываем цепочку.
  let lastCorrected = $state('');
  // Его формулировка: шторка версий называет предмет по-русски, id остаётся подписью.
  let lastCorrectedStatement = $state('');

  /** Русская связка темы или пустая строка: сырой ключ в текст не подставляем. */
  function topicOf(finding: FindingListItem): string {
    const subject = knownTerm(SUBJECT_LABELS, finding.subject);
    const predicate = knownTerm(PREDICATE_LABELS, finding.predicate);
    return subject && predicate ? `${subject}, ${predicate}` : '';
  }

  const commentReady = $derived(comment.trim().length >= 3);
  const correctionReady = $derived(
    verdict === 'correct' && canRestricted && Boolean(findingId) && correction.trim().length > 0,
  );
  // Новая версия утверждения вместо старой создаётся только там, где у аккаунта
  // открыты закрытые данные; остальным «нужна правка» остаётся комментарием.
  const correctionRequired = $derived(verdict === 'correct' && canRestricted);

  // Кнопка не гаснет из-за незаполненных полей: иначе у обязательного действия
  // нет пути с клавиатуры, а причина становится видна только после попытки.
  const formOpen = $derived(Boolean(target) && !submitting);
  // Претензия к решению появляется после попытки отправки, а не на пустой форме.
  const verdictMissing = $derived(submitAttempted && !verdict);
  const commentError = $derived(
    commentReady || (!submitAttempted && comment.trim().length === 0)
      ? ''
      : comment.trim().length === 0
        ? 'Опишите, что в ответе проверяемо по источнику. Без комментария отзыв не принимается.'
        : `Нужен комментарий от 3 символов: сейчас ${comment.trim().length}.`,
  );

  // Пул для правки это находки корпуса: своего ответа на вопрос раздел не держит
  // (его задают в «Вопросе»), сужать выбор до утверждений ответа нечему.
  const candidates = $derived.by(() => {
    const pool = corpus;
    const query = findingQuery.trim().toLowerCase();
    const list = query
      ? pool.filter(
          (finding) =>
            finding.statement.toLowerCase().includes(query) ||
            (finding.subject ?? '').toLowerCase().includes(query) ||
            termOf(SUBJECT_LABELS, finding.subject ?? '').toLowerCase().includes(query),
        )
      : pool;
    return list.slice(0, 12);
  });

  // Пул для правки пуст и без поиска: «уточните поиск» про другое, про фильтр,
  // который ничего не отобрал из существующего пула.
  const poolEmpty = $derived(corpus.length === 0);

  // Журнал предложений растёт вместе с корпусом: список разбит на страницы, чтобы
  // пятьсот предложений не становились одним DOM-полотном.
  const shownProposals = $derived(proposals.slice(0, proposalLimit));
  // Показанная часть дошла до потолка загрузки, а не до конца журнала: без
  // полного числа от сервиса это отдельное состояние, а не «предложений больше
  // нет».
  const proposalsAtCap = $derived(proposals.length >= PROPOSAL_READ_SIZE);

  // Сколько осталось за показанной частью лент: без полного числа от сервера это
  // неизвестность (догружать не предлагаем), а не ноль.
  const runsLeft = $derived(
    runsTotal === null ? null : Math.max(0, runsTotal - runs.length),
  );
  const experimentsLeft = $derived(
    experimentsTotal === null ? null : Math.max(0, experimentsTotal - experiments.length),
  );
  // Прогон ищется по показанной части ленты: если за ней осталось непоказанное,
  // строка «прогона нет» может значить «его просто не показали».
  const experimentsCaveat = $derived(
    canReview && experimentsTotal !== null && experimentsTotal > experiments.length
      ? FEEDBACK_FEED.lookupNote
      : '',
  );

  /** Пустой список при положительном или не названном полном числе это
   *  незагруженный журнал, а не «записей нет». null подтверждённая пустота. */
  function unloadedState(total: number | null): { title: string; body: string } | null {
    if (total === 0) return null;
    return total === null
      ? { title: FEEDBACK_FEED.noTotalTitle, body: FEEDBACK_FEED.noTotal }
      : { title: FEEDBACK_FEED.emptyTotalTitle, body: FEEDBACK_FEED.emptyTotal };
  }

  // Дочитывания идут по серверному offset; счётчики отсекают запоздавшие
  // ответы после перечитывания, а перекрывшиеся порции склеиваются по id.
  async function loadRunsMore(): Promise<void> {
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
      // Смещение сдвигается по строкам ответа: дубликаты тупиком не считаются,
      // там следующий клик действительно читает дальше.
      runsStalled = page.items.length === 0;
      if (page.total !== null) runsTotal = page.total;
    } catch {
      if (call === runsSeq) runsMoreError = FEEDBACK_FEED.moreFailed;
    } finally {
      if (call === runsSeq) runsMoreLoading = false;
    }
  }

  /**
   * Одна кнопка «Показать ещё» на два действия: сначала раскрывает уже
   * загруженный буфер строк, а когда буфер кончился молча читает следующую
   * страницу журнала. Двух кнопок с одной подписью на экране не бывает.
   */
  function showMoreRuns(): void {
    if (runs.length > runsShown) {
      runsShown += RUNS_PAGE_SIZE;
      return;
    }
    void loadRunsMore();
  }

  async function loadExperimentsMore(): Promise<void> {
    const call = ++experimentsSeq;
    const offset = experimentsOffset;
    experimentsMoreLoading = true;
    experimentsMoreError = '';
    try {
      const page = await api.experiments(EXPERIMENTS_FEED_SIZE, offset);
      if (call !== experimentsSeq) return;
      experimentsOffset = offset + page.items.length;
      const seen = new Set(experiments.map((item) => item.id));
      experiments = [...experiments, ...page.items.filter((item) => !seen.has(item.id))];
      experimentsStalled = page.items.length === 0;
      if (page.total !== null) experimentsTotal = page.total;
    } catch {
      if (call === experimentsSeq) experimentsMoreError = FEEDBACK_FEED.moreFailed;
    } finally {
      if (call === experimentsSeq) experimentsMoreLoading = false;
    }
  }

  function experimentOf(proposalId: string): EvolutionExperiment | null {
    return experiments.find((item) => item.proposal_id === proposalId) ?? null;
  }

  function promoteOf(proposalId: string): EvolutionExperiment | null {
    const found = experimentOf(proposalId);
    return found && found.decision === 'promote' ? found : null;
  }

  interface MetricSeed {
    key: string;
    label: string;
    base: number;
    cand: number;
    better: 'больше' | 'меньше';
  }

  interface MetricRow extends MetricSeed {
    baseText: string;
    candText: string;
    /** null — реальной шкалы у пары нет: полосы не рисуется. */
    baseWidth: number | null;
    candWidth: number | null;
  }

  /** Полосы только по измеренным значениям; масштаб: своя шкала доли или
   *  максимум пары для задержки. */
  function metricRows(ab: EvolutionExperiment): MetricRow[] {
    const latencyMax = Math.max(ab.baseline.average_latency_ms, ab.candidate.average_latency_ms);
    const rows: MetricSeed[] = [
      {
        key: 'pass_rate',
        label: AB_METRIC_LABELS.pass_rate,
        base: ab.baseline.pass_rate,
        cand: ab.candidate.pass_rate,
        better: 'больше',
      },
      {
        key: 'source_recall',
        label: AB_METRIC_LABELS.source_recall,
        base: ab.baseline.source_recall,
        cand: ab.candidate.source_recall,
        better: 'больше',
      },
      {
        key: 'citation_coverage',
        label: AB_METRIC_LABELS.citation_coverage,
        base: ab.baseline.citation_coverage,
        cand: ab.candidate.citation_coverage,
        better: 'больше',
      },
      {
        key: 'latency',
        label: AB_METRIC_LABELS.latency,
        base: ab.baseline.average_latency_ms,
        cand: ab.candidate.average_latency_ms,
        better: 'меньше',
      },
    ];
    return rows.map((row) => {
      // Доля измеряется в собственной шкале 0…1; задержку ведём по максимуму
      // пары. Оба нуля — реальной шкалы нет, и полосы не будет.
      const scale = row.key === 'latency' ? latencyMax : 1;
      const width = (value: number): number | null =>
        scale > 0 ? Math.max(0, Math.min(100, (value / scale) * 100)) : null;
      return {
        ...row,
        baseText: row.key === 'latency' ? `${num(row.base)} мс` : pct(row.base),
        candText: row.key === 'latency' ? `${num(row.cand)} мс` : pct(row.cand),
        baseWidth: width(row.base),
        candWidth: width(row.cand),
      };
    });
  }

  function clearForm(): void {
    findingId = '';
    correction = '';
    lastCorrected = '';
    result = null;
    submitFailure = null;
    submitAttempted = false;
    softFailure = '';
  }

  function chooseRun(run: EvaluationRun): void {
    target = {
      queryId: run.query_id,
      provenance: 'проверка ответа из журнала',
      createdAt: run.created_at,
      evaluation: run,
    };
    clearForm();
    runsOpen = false;
  }

  /**
   * Комментарий без привязки к ответу: так оставляют замечание о корпусе в целом
   * или вывод, принесённый из «Сравнения» (ссылка ведёт сюда с `?about=…`).
   */
  function chooseFreeForm(about = ''): void {
    target = {
      queryId: NIL_QUERY_ID,
      provenance: about ? `вывод по сравнению «${about}»` : 'комментарий без привязки к ответу',
    };
    clearForm();
  }

  function dropTarget(): void {
    target = null;
    clearForm();
  }

  async function submitReview(): Promise<void> {
    submitAttempted = true;
    const queryId = target?.queryId;
    if (!queryId || submitting) return;
    // Три отдельных пропуска — три отдельных объяснения у своего поля;
    // молча не отправлять и не показывать одну общую претензию.
    if (!verdict || !commentReady) return;
    if (correctionRequired && !correctionReady) return;
    submitting = true;
    result = null;
    submitFailure = null;
    softFailure = '';
    const correctedFinding = verdict === 'correct' ? findingId : '';
    try {
      const raw = await api.feedback({
        query_id: queryId,
        finding_id: verdict === 'correct' && findingId ? findingId : null,
        verdict,
        comment: comment.trim(),
        correction: verdict === 'correct' && correction.trim() ? correction.trim() : undefined,
      });
      const parsed = readFeedbackResult(raw);
      result = parsed;
      if (parsed.proposal) proposals = [parsed.proposal, ...proposals];
      if (correctedFinding) {
        lastCorrected = correctedFinding;
        // Формулировку берём из того же пула, из которого выбирали: шторка
        // называет утверждение по-русски, id остаётся подписью.
        lastCorrectedStatement =
          corpus.find((item) => item.id === correctedFinding)?.statement ?? '';
        // Цепочку версий читаем сразу и открываем в шторке: правку без
        // показанной цепочки не проверяешь глазами.
        await loadHistory(correctedFinding, lastCorrectedStatement, true);
        try {
          corpus = (await api.findings()).items;
          corpusFailure = '';
        } catch {
          softFailure = 'Правка записана, но находки не обновились. Обновите раздел позже.';
        }
      }
      correction = '';
      findingId = '';
    } catch (reason) {
      submitFailure = classify(reason);
    } finally {
      submitting = false;
    }
  }

  // Находки для правки перечитывают отдельно: пустой пул лечится пополнением
  // корпуса, а не обновлением журналов предложений.
  async function reloadCorpus(): Promise<void> {
    try {
      corpus = (await api.findings()).items;
      corpusFailure = '';
    } catch {
      corpusFailure = 'Не удалось загрузить находки. Попробуйте ещё раз.';
    }
  }

  async function reloadLedgers(): Promise<void> {
    softFailure = '';
    experimentsSeq += 1;
    try {
      const [proposalsResult, experimentsResult] = await Promise.allSettled([
        api.proposals(PROPOSAL_READ_SIZE, 0),
        api.experiments(EXPERIMENTS_FEED_SIZE, 0),
      ]);
      if (proposalsResult.status === 'fulfilled') proposals = proposalsResult.value;
      else softFailure = 'Журнал предложений не обновился.';
      if (experimentsResult.status === 'fulfilled') {
        experiments = experimentsResult.value.items;
        experimentsTotal = experimentsResult.value.total;
        experimentsOffset = experimentsResult.value.items.length;
        experimentsMoreError = '';
        // Список загружен заново: с начала, поэтому прежнее «показаны не все»
        // к нему больше не относится.
        experimentsStalled = false;
        experimentsFailure = '';
      } else {
        experimentsStalled = false;
        softFailure = 'Результаты сравнения не обновились.';
      }
    } catch {
      softFailure = 'Журналы не обновились: проверьте соединение и повторите попытку.';
    }
  }

  async function decide(proposal: EvolutionProposal, decision: boolean): Promise<void> {
    if (!canReview) {
      reviewFailure = FEEDBACK_GATE;
      return;
    }
    reviewingId = proposal.id;
    reviewFailure = '';
    reviewNotice = '';
    try {
      const updated = await api.review(proposal.id, decision);
      proposals = proposals.map((item) => (item.id === updated.id ? updated : item));
      reviewNotice = `Предложение «${updated.title}» получило статус «${PROPOSAL_STATUS_LABELS[updated.status]}».`;
    } catch (reason) {
      const failureKind = classify(reason);
      reviewFailure =
        failureKind.kind === 'denied'
          ? FEEDBACK_GATE
          : failureKind.kind === 'conflict'
            ? 'Сначала нужен замер этого предложения на проверочных вопросах: без него решение не записывается.'
            : 'Решение не записано. Проверьте соединение и повторите.';
    } finally {
      reviewingId = '';
    }
  }

  async function runAbExperiment(proposal: EvolutionProposal): Promise<void> {
    runningId = proposal.id;
    experimentFailure = '';
    try {
      const ab = await api.runExperiment(proposal.id);
      experiments = [ab, ...experiments];
      experimentNotice = `Сравнение «${proposal.title}»: ${
        ab.decision === 'promote' ? AB_STRINGS.promote : AB_STRINGS.hold
      }.`;
    } catch (reason) {
      const failureKind = classify(reason);
      experimentFailure =
        failureKind.kind === 'denied'
          ? FEEDBACK_GATE
          : failureKind.kind === 'conflict'
            ? 'Сравнение не выполнено: предложение изменилось. Обновите журнал.'
            : 'Сравнение не выполнено. Проверьте соединение и повторите.';
    } finally {
      runningId = '';
    }
  }

  // Цепочка версий утверждения: все версии с тем, кто проверял и чем заменено.
  // Формулировка утверждения идёт рядом с id: шторка называет предмет по-русски.
  async function loadHistory(claimId: string, claimText = '', afterCorrection = false): Promise<void> {
    historyClaim = claimId;
    historyClaimText = claimText;
    history = null;
    historyFailure = '';
    historyBusy = true;
    historyOpen = true;
    try {
      history = await api.claimHistory(claimId);
    } catch (reason) {
      const failureKind = classify(reason);
      historyFailure =
        failureKind.kind === 'denied'
          ? FEEDBACK_GATE
          : 'Цепочка версий не открылась. Проверьте соединение и повторите.';
      // Правка уже записана: потеря цепочки её не отменяет, но скрывает замены.
      // Два факта называются отдельно: «записано» и «не открылось» не склеивают в
      // одну фразу, которая на отказе по праву звучала бы бессмыслицей.
      if (afterCorrection) {
        softFailure =
          failureKind.kind === 'denied'
            ? 'Правка записана. Версии этого утверждения аккаунту не открыты.'
            : 'Правка записана. Версии утверждения не загрузились: откройте их позже кнопкой «Версии утверждения».';
      }
    } finally {
      historyBusy = false;
    }
  }

  function closeHistory(): void {
    historyOpen = false;
  }

  /**
   * Замену показываем номером версии, а не UUID: идентификатор цепочки читателю
   * не нужен, а номер рядом со следующей строкой списка — нужен.
   */
  function versionNumberById(claimId: string | null): string | null {
    if (!claimId || !history) return null;
    const found = history.versions.find((row) => row.finding_id === claimId);
    return found ? String(found.version) : null;
  }

  /** Кто проверял: имя аккаунта журнал не восстанавливает, поэтому — про своего. */
  function reviewerOf(reviewerId: string): string {
    return reviewerId === session.account?.id ? 'правку внесли вы' : 'правку внёс другой эксперт';
  }

  async function loadAll(): Promise<void> {
    phase = 'loading';
    failure = null;
    runsFailure = '';
    experimentsFailure = '';
    corpusFailure = '';
    softFailure = '';
    runsSeq += 1;
    experimentsSeq += 1;
    runsMoreError = '';
    experimentsMoreError = '';
    // Перечитывание лент с нулевого offset владеет и тупиком дочитывания: окно
    // заново читается с начала, прежнее «дальше читать нечего» больше не верно.
    runsStalled = false;
    experimentsStalled = false;
    const [proposalsResult, findingsResult, runsResult, experimentsResult] = await Promise.allSettled([
      // Журнал предложений и прогоны читает только эксперт: без права не просим —
      // сервер ответит 403, а отказ по доступу не должен выглядеть сбоем раздела.
      canReview ? api.proposals(PROPOSAL_READ_SIZE, 0) : Promise.resolve([] as EvolutionProposal[]),
      canRead
        ? api.findings().then((window) => window.items)
        : Promise.resolve([] as FindingListItem[]),
      canEvaluate ? api.evaluations(RUNS_FEED_SIZE, 0) : Promise.resolve(null),
      canReview ? api.experiments(EXPERIMENTS_FEED_SIZE, 0) : Promise.resolve(null),
    ]);
    if (proposalsResult.status === 'rejected') {
      failure = classify(proposalsResult.reason);
      phase = 'failed';
      return;
    }
    proposals = proposalsResult.value;
    // Сбой чтения не равен пустому журналу: пустота у каждого своя, а сбой
    // называется отдельной строкой у своей секции.
    if (experimentsResult.status === 'fulfilled') {
      const window = experimentsResult.value;
      experiments = window === null ? [] : window.items;
      experimentsTotal = window?.total ?? null;
      experimentsOffset = window?.items.length ?? 0;
      experimentsFailure = '';
    } else {
      experiments = [];
      experimentsTotal = null;
      experimentsOffset = 0;
      experimentsFailure = 'Не удалось загрузить результаты сравнения. Обновите раздел.';
    }
    if (findingsResult.status === 'fulfilled') {
      corpus = findingsResult.value;
      corpusFailure = '';
    } else {
      corpus = [];
      corpusFailure = 'Не удалось загрузить находки. Обновите раздел.';
    }
    if (runsResult.status === 'fulfilled') {
      const window = runsResult.value;
      runs = window === null ? [] : window.items;
      runsTotal = window?.total ?? null;
      runsOffset = window?.items.length ?? 0;
    } else {
      runsFailure = 'Не удалось загрузить журнал проверок. Обновите раздел.';
    }
    phase = 'ready';
  }

  // Доступ приходит вместе с подтверждённым входом: раздел загружается заново,
  // когда он появился.
  let started = false;
  $effect(() => {
    if (session.state === 'unknown') return;
    if (!canGive && !canReview) {
      phase = 'ready';
      return;
    }
    if (started) return;
    started = true;
    void loadAll();
  });

  // «Сравнение» присылает сюда действием «Записать отзыв»: ответ свободный
  // комментарий, а строка `about` говорит, о чём именно вывод.
  // «Расхождения» присылают конкретное утверждение (`?claim=`): выбор решения и
  // утверждения проделывается за человека ровно в том объёме, в каком он его
  // назвал, и объявляется строкой. Молча подставленное утверждение читалось бы
  // как своя правка.
  let pendingClaim = '';
  let claimNote = $state('');
  // Принесённое утверждение не нашлось: строка замечания остаётся, и к ней
  // добавляется ручное действие. Молчать переход не имеет права ни на шаге.
  let claimLost = $state(false);

  onMount(() => {
    const about = page.url.searchParams.get('about')?.trim().slice(0, 160) ?? '';
    const claim = page.url.searchParams.get('claim')?.trim() ?? '';
    if (claim) pendingClaim = claim;
    if (about) chooseFreeForm(about);
    else if (claim) {
      chooseFreeForm('разбор расхождения из раздела «Расхождения»');
      // Переход объявляет себя сразу: до ответа сервера человек видит, что именно
      // сюда принесли, а не пустую форму в ожидании.
      claimNote = 'Из «Расхождений» принесли утверждение для правки. Подставим его, когда найдём в находках.';
    }
  });

  $effect(() => {
    // Утверждение ищется, только когда раздел дочитан: до ответа сервера выбирать
    // нечего, а обещание подстановки не должно висеть над несостоявшимся поиском.
    if (!pendingClaim || phase !== 'ready') return;
    if (!canRead) {
      pendingClaim = '';
      claimLost = true;
      claimNote =
        'Правка утверждения этому аккаунту не открыта: в форму подставить нечего. Право выдаёт администратор сервиса.';
      return;
    }
    if (corpusFailure) {
      pendingClaim = '';
      claimLost = true;
      claimNote =
        'Находки не пришли, поэтому принесённое утверждение не подставлено. Обновите раздел и найдите его поиском по формулировке.';
      return;
    }
    if (corpus.length === 0) {
      pendingClaim = '';
      claimLost = true;
      claimNote =
        'В находках корпуса пока нет ни одного утверждения: подставлять нечего. Найдите утверждение поиском, когда корпус пополнен.';
      return;
    }
    const found = corpus.find((item) => item.id === pendingClaim);
    pendingClaim = '';
    if (!found) {
      claimLost = true;
      claimNote =
        'Принесённое из «Расхождений» утверждение в находках не нашлось: возможно, оно аккаунту не открыто. Найдите его поиском по формулировке.';
      return;
    }
    claimLost = false;
    verdict = 'correct';
    findingId = found.id;
    findingQuery = '';
    claimNote = `Утверждение для правки из «Расхождений»: ${found.statement}`;
  });
</script>

<svelte:head>
  <title>Отзыв на ответ: Научный Клубок</title>
  <meta
    name="description"
    content="Примите ответ системы, отклоните его или попросите правку. В комментарии поясните, что в ответе проверяемо по источнику. Правка утверждения и разбор предложений модели доступны эксперту."
  />
</svelte:head>

{#snippet serviceNote(text: string)}
  <!-- Служебный текст отказа под раскрытием: он нужен, чтобы сверить запись с
       журналом, но в пользовательской строке ему не место. -->
  {#if text}
    <details class="work__svc">
      <summary class="micro">Служебные данные</summary>
      <p class="tech">{text}</p>
    </details>
  {/if}
{/snippet}

<div class="page work">
  <div class="wrap">
    <SectionHead
      level="1"
      eyebrow="Корпус, отзывы"
      title={FEEDBACK_HEAD.title}
      lead={FEEDBACK_HEAD.lead}
    />

    {#if !canGive && !canReview}
      <Notice tone="warn" title="Отзыв не записывается">
        {FEEDBACK_GATE} Отзывы и правки не записываются, предложения модели не открываются.
        <div class="row work__actions">
          <Button href="/findings" variant="quiet" size="sm">{navLabel('/findings')}</Button>
          <Button href="/research" variant="ghost" size="sm">{navLabel('/research')}</Button>
        </div>
      </Notice>
    {:else if phase === 'loading'}
      <div class="work__loading" role="status" aria-label="Загружаем данные для отзыва">
        <p class="eyebrow"><span class="spinner spinner--quiet"></span> Загружаем данные для отзыва</p>
        <div class="skeleton" style="height:120px"></div>
        <div class="skeleton" style="height:120px"></div>
        <p class="micro muted">Загружаем журнал проверок и находки.</p>
        <!-- Выход из подвешенной загрузки: если сессия не ответила, чтение может
             не начаться вовсе. Выход здесь, а не вечное «загружаем». -->
        <div class="row work__actions">
          <p class="micro muted">Если данные не появляются, проверьте соединение.</p>
          <Button variant="quiet" size="sm" onclick={() => void session.refresh()}>
            Проверить соединение
          </Button>
        </div>
      </div>
    {:else if phase === 'failed'}
      <Notice tone={failure?.kind === 'denied' ? 'warn' : 'error'} title="Данные не прочитаны">
        {failure ? failureText(failure) : 'Данные не прочитаны. Проверьте соединение и повторите попытку.'}
        <!-- Служебный текст отказа под тем же раскрытием, что и у сбоя отправки:
             экран называет состояние по-человечески, детали нужны для сверки. -->
        {@render serviceNote(failure?.text ?? '')}
        <div class="row work__actions">
          {#if failure?.kind === 'denied'}
            <!-- Повтор отказа по праву даёт тот же отказ: здесь только то, что
                 действительно двигает решение. -->
            <Button variant="quiet" size="sm" href="/account">Что открыто моему аккаунту</Button>
            <Button variant="ghost" size="sm" href="/findings">{navLabel('/findings')}</Button>
          {:else}
            <Button variant="action" size="sm" icon="refresh" onclick={() => void loadAll()}>Прочитать заново</Button>
          {/if}
        </div>
      </Notice>
    {:else}
      {#if softFailure}
        <Notice tone="error" title="Данные прочитаны не полностью">
          {softFailure}
          <div class="row work__actions">
            <Button variant="quiet" size="sm" onclick={() => (softFailure = '')}>Понятно</Button>
          </div>
        </Notice>
      {/if}

      {#if canGive}
        <Panel tone="default">
          <div class="stack work__region">
            <div class="panel__head">
              <div class="stack">
                <p class="eyebrow"><Icon name="target" size={16} /> Что проверяем</p>
                <h2 class="h3">Ответ, на который пишете отзыв</h2>
                <p class="micro muted">
                  Это проверка ответа из журнала либо вывод, принесённый из «Сравнения».
                </p>
              </div>
            </div>

            {#if target}
              <div class="target">
                <p class="micro target__label">Ответ выбран</p>
                <p class="small target__provenance">
                  {target.provenance}
                  {#if target.createdAt}
                    <time class="num" datetime={target.createdAt}>{dateTime(target.createdAt)}</time>
                  {/if}
                </p>
                <dl class="kv target__facts">
                  {#if target.evaluation}
                    <dt>{RUN_METRIC_LABELS.citationCoverage}</dt>
                    <dd class="num">{pct(target.evaluation.metrics.citation_coverage)}</dd>
                    <dt>{RUN_METRIC_LABELS.unsupportedClaims}</dt>
                    <dd class="num">{pct(target.evaluation.metrics.unsupported_claim_ratio)}</dd>
                    <dt>{RUN_METRIC_LABELS.overall}</dt>
                    <dd class="num">
                      {num(target.evaluation.metrics.overall)}
                      <span class="micro muted">{RUN_METRIC_LABELS.overallScale}</span>
                    </dd>
                    <dt>проверка ответа</dt>
                    <dd>{target.evaluation.passed ? RUN_METRIC_LABELS.passed : RUN_METRIC_LABELS.failed}</dd>
                  {/if}
                </dl>
                <div class="row work__actions">
                  <Button variant="quiet" size="sm" onclick={dropTarget}>Снять ответ</Button>
                </div>
              </div>
            {:else}
              <div class="work__ask">
                <p class="micro muted">
                  Свежий ответ получают вопросом в разделе «Вопрос»: проверка ответа
                  появляется в журнале сразу после него.
                </p>
                <Button href="/research" variant="link" size="sm">Задать вопрос</Button>
              </div>

              <!-- Список ответов под раскрытием: форма отзыва остаётся в первом
                   экране, журнал подключается одним нажатием. -->
              <div class="acc">
                <button
                  type="button"
                  class="acc__head"
                  aria-expanded={runsOpen}
                  aria-controls="fb-runs-body"
                  onclick={() => (runsOpen = !runsOpen)}
                >
                  <span>Выбрать, на что отвечать</span>
                  <Icon name="plus" size={16} class="acc__icon" />
                </button>
                <div class="acc__body" id="fb-runs-body" hidden={!runsOpen}>
                  <div class="freeform">
                    <Button variant="quiet" size="sm" onclick={() => chooseFreeForm()}>
                      Комментарий без привязки к ответу
                    </Button>
                    <p class="micro muted">
                      Так оставляют замечание не об одном ответе, а о корпусе в целом.
                    </p>
                  </div>

                  {#if canEvaluate}
                    {#if runs.length === 0}
                      <div class="work__note">
                        {#if runsFailure}
                          <Notice tone="error" title="Журнал проверок не загружен">{runsFailure}</Notice>
                        {:else if unloadedState(runsTotal)}
                          <!-- Пустой список при ненулевом или не названном полном
                               числе: журнал не загружен, а не пуст. -->
                          <Notice tone="error" title={unloadedState(runsTotal)?.title}>
                            {unloadedState(runsTotal)?.body}
                            <div class="row work__actions">
                              <Button variant="action" size="sm" icon="refresh" onclick={() => void loadAll()}>
                                Обновить журнал
                              </Button>
                            </div>
                          </Notice>
                        {:else}
                          <Empty
                            icon="list"
                            title="Журнал проверок пуст"
                            body="Проверка ответа появляется сразу после вопроса к корпусу. Задайте его в разделе «Вопрос»."
                          >
                            {#snippet action()}
                              <div class="row">
                                <Button href="/research" variant="quiet" size="sm">{navLabel('/research')}</Button>
                                <Button href="/dashboard" variant="ghost" size="sm">{navLabel('/dashboard')}</Button>
                              </div>
                            {/snippet}
                          </Empty>
                        {/if}
                      </div>
                    {:else}
                      <div class="stack runs">
                        <!-- Счётчик считает строки, которые человек действительно
                             видит, а не загруженный буфер: знаменатель относится к
                             этому списку, а не к ответу сервера. -->
                        <p class="micro muted">
                          {feedShownOf(Math.min(runsShown, runs.length), runsTotal, RUN_NOUNS)}
                        </p>
                        <!-- Шкала итога названа один раз над списком: число без неё
                             читают то за процент, то за балл из десяти. -->
                        <p class="micro muted">
                          {RUN_METRIC_LABELS.overall}: {RUN_METRIC_LABELS.overallScale}.
                        </p>
                        {#each runs.slice(0, runsShown) as run (run.id)}
                          <div class="run">
                            <time class="micro muted" datetime={run.created_at}>{dateTime(run.created_at)}</time>
                            <span class="micro">
                              {RUN_METRIC_LABELS.citationCoverage}
                              <span class="num">{pct(run.metrics.citation_coverage)}</span>
                            </span>
                            <span class="micro">
                              {RUN_METRIC_LABELS.unsupportedClaims}
                              <span class="num">{pct(run.metrics.unsupported_claim_ratio)}</span>
                            </span>
                            <span class="micro">
                              {RUN_METRIC_LABELS.overall}
                              <span class="num">{num(run.metrics.overall)}</span>
                            </span>
                            <StatusPill
                              status={run.passed ? 'consensus' : 'disputed'}
                              label={run.passed ? RUN_METRIC_LABELS.passed : RUN_METRIC_LABELS.failed}
                            />
                            <Button variant="quiet" size="sm" onclick={() => chooseRun(run)}>
                              Оценить этот ответ
                            </Button>
                          </div>
                        {/each}
                        <!-- Одна кнопка «Показать ещё» на два шага: раскрывает
                             загруженный буфер, а когда он кончился читает
                             следующую страницу журнала. Кнопок с одной подписью
                             рядом не стоит. -->
                        {#if runs.length > runsShown || (!runsStalled && (runsLeft ?? 0) > 0)}
                          <Button
                            variant="quiet"
                            size="sm"
                            busy={runsMoreLoading}
                            disabled={runsMoreLoading}
                            onclick={showMoreRuns}
                          >
                            {runsMoreLoading ? FEEDBACK_FEED.loadingMore : FEEDBACK_FEED.loadMore}
                          </Button>
                        {/if}
                        {#if runsStalled}
                          <!-- Остаток назван, а страница пустая: догружание убрано,
                               чтобы не предлагать пустое действие. -->
                          <Notice tone="warn" title={FEEDBACK_FEED.stalledTitle}>
                            {FEEDBACK_FEED.stalled}
                          </Notice>
                        {/if}
                        {#if runsMoreError}
                          <Notice tone="error" title={FEEDBACK_FEED.moreFailedTitle}>
                            {runsMoreError}
                          </Notice>
                        {/if}
                        {#if runsFailure}
                          <p class="micro muted">{runsFailure}</p>
                        {/if}
                      </div>
                    {/if}
                  {:else}
                    <div class="work__note">
                      <Notice tone="info" title="Журнал проверок не открыт">
                        Показания качества ответа этому аккаунту не показывают. Ответ всё
                        равно можно оценить: оставьте комментарий без привязки к проверке.
                      </Notice>
                    </div>
                  {/if}
                </div>
              </div>
            {/if}

            <hr class="rule" />

            <div class="stack">
              <h2 class="h3">Ваш отзыв на ответ</h2>
              <!-- Переход из «Расхождений» объявляет себя здесь, а не только под
                   отмеченным решением: на первых секундах и при закрытом пуле
                   правки человек видит, что именно сюда принесли и что с этим
                   делать. Решение он ещё не выбирал. -->
              {#if claimNote}
                <div class="work__note">
                  <p class="micro muted">{claimNote}</p>
                  {#if claimLost}
                    <div class="row work__actions">
                      {#if canRestricted}
                        <Button
                          variant="quiet"
                          size="sm"
                          onclick={() => (verdict = 'correct')}>
                          Искать утверждение вручную
                        </Button>
                      {/if}
                      <Button href="/findings" variant="ghost" size="sm">{navLabel('/findings')}</Button>
                    </div>
                  {/if}
                </div>
              {/if}
              <!-- Три решения видны подписями на метках, поэтому здесь только
                   требование к комментарию: «оценка ответа» остаётся именем
                   метрики качества и действием не называется. -->
              <p class="micro muted">Комментарий обязателен: без него отзыв не принимается.</p>

            {#if result?.proposal}
              <div class="work__note">
                <Notice tone="ok" title="Отзыв записан, предложение составлено">
                  <span class="notice__line">
                    {knownTerm(PROPOSAL_KIND_LABELS, result.proposal.kind) ?? PROPOSAL_KIND_FALLBACK}:
                    «{result.proposal.title}»
                  </span>
                  <span class="small notice__line">{result.proposal.change}</span>
                  <span class="micro notice__line">
                    {#if result.proposal.impact.length}
                      Затрагивает: {result.proposal.impact.join(', ')}.
                    {/if}
                    Статус: {PROPOSAL_STATUS_LABELS[result.proposal.status]}.
                    {#if canReview}Строка журнала ниже.{/if}
                  </span>
                </Notice>
              </div>
            {:else if result}
              <div class="work__note">
                <Notice tone="warn" title="Отзыв записан, предложения модели нет">
                  {#if result.degradation.length > 0}
                    <span class="micro notice__line">
                      Ответ собран не полностью: {result.degradation.join(', ')}
                    </span>
                  {/if}
                  <span class="micro notice__line">
                    Формулировку предложения делает модель: без её ответа предложения нет.
                    Ваше решение при этом сохранено.
                  </span>
                </Notice>
              </div>
            {/if}

            {#if result?.superseded}
              <div class="work__note">
                <Notice tone="ok" title="Создана новая версия утверждения">
                  <span class="small notice__line">{result.superseded.statement}</span>
                  <span class="micro notice__line">
                    версия <span class="num">{result.superseded.version}</span>
                    {#if topicOf(result.superseded)}({topicOf(result.superseded)}){/if}
                  </span>
                  {#if lastCorrected}
                    <span class="notice__line">
                      <Button
                        variant="quiet"
                        size="sm"
                        icon="clock"
                        onclick={() => void loadHistory(lastCorrected, lastCorrectedStatement, true)}>
                        Версии утверждения
                      </Button>
                    </span>
                  {/if}
                </Notice>
              </div>
            {/if}

            {#if submitFailure}
              <div class="work__note">
                <Notice tone={submitFailure.kind === 'conflict' ? 'warn' : 'error'} title="Отзыв не отправлен">
                  {failureText(submitFailure)}
                  {@render serviceNote(submitFailure.text)}
                </Notice>
              </div>
            {/if}

            <form class="stack verdict-form" onsubmit={(event) => { event.preventDefault(); void submitReview(); }}>
              <!-- Одно решение из трёх: radiogroup, а не три кнопки с aria-pressed.
                   Стрелки и пробел работают штатно, доступное имя группы legend. -->
              <fieldset
                class="stack verdict-form__block"
                aria-describedby={verdictMissing ? 'verdict-err' : undefined}
              >
                <legend class="field__label">Решение по ответу</legend>
                <div class="row">
                  {#each FEEDBACK_VERDICTS as item (item.key)}
                    <label class="verdict">
                      <input
                        class="verdict__input"
                        type="radio"
                        name="verdict"
                        value={item.key}
                        bind:group={verdict}
                      />
                      <span class="chip verdict__chip">{item.label}</span>
                    </label>
                  {/each}
                </div>
                {#if verdictMissing}
                  <p class="field__error" id="verdict-err">
                    <Icon name="alert" size={15} /> Выберите решение: без него отзыв не отправится.
                  </p>
                {/if}
              </fieldset>

              <Field
                label="Комментарий"
                name="fb-comment"
                type="textarea"
                rows={4}
                placeholder="Что в ответе проверяемо по источнику, а что держится на пересказе?"
                bind:value={comment}
                error={commentError}
              />

              {#if verdict === 'correct'}
                {#if canRestricted}
                  <fieldset class="stack picks">
                    <legend class="field__label">Какое утверждение заменить</legend>
                    <p class="micro muted">Список берётся из находок корпуса.</p>
                    <Field
                      label="Поиск утверждения"
                      name="fb-finding-query"
                      type="search"
                      placeholder="по формулировке или названию технологии"
                      bind:value={findingQuery}
                    />
                    {#if corpusFailure}
                      <Notice tone="error" title="Находки не загружены">{corpusFailure}</Notice>
                    {:else if poolEmpty}
                      <!-- Пустой пул и пустой поиск разные состояния: первый
                           лечится обновлением находок и пополнением корпуса. -->
                      <Empty
                        icon="list"
                        title="Утверждений для замены пока нет"
                        body="Здесь появятся утверждения: обновите находки, когда корпус пополнен, или загрузите документ в разделе «Находки»."
                      >
                        {#snippet action()}
                          <div class="row">
                            <Button variant="quiet" size="sm" icon="refresh" onclick={() => void reloadCorpus()}>
                              Обновить находки
                            </Button>
                            <Button href="/findings" variant="ghost" size="sm">Пополнить корпус</Button>
                          </div>
                        {/snippet}
                      </Empty>
                    {:else if candidates.length === 0}
                      <p class="micro muted">Совпадений нет. Уточните поиск по формулировке.</p>
                    {:else}
                      <div class="picks__list">
                        {#each candidates as finding (finding.id)}
                          {@const picked = findingId === finding.id}
                          <div class="pick" data-on={picked}>
                            <!-- Выбор одного утверждения — радио: один вариант из
                                 списка, клавиатура и скринридер ведут себя штатно. -->
                            <label class="pick__main">
                              <input
                                class="pick__input"
                                type="radio"
                                name="fb-finding"
                                value={finding.id}
                                bind:group={findingId}
                              />
                              <span class="small grow">{finding.statement}</span>
                              <span class="micro muted">
                                версия <span class="num">{finding.version}</span>
                              </span>
                            </label>
                            <StatusPill status={finding.status} label={STATUS_SHORT[finding.status]} />
                            <Button
                              variant="quiet"
                              size="sm"
                              icon="clock"
                              title="Все версии этого утверждения"
                              onclick={() => void loadHistory(finding.id, finding.statement)}>
                              версии
                            </Button>
                            <p class="pick__locs">
                              {#if finding.evidence[0]}
                                <span class="locator">{finding.evidence[0].source_title || 'источник без названия'}</span>
                                {#if finding.evidence[0].page != null}
                                  <span class="locator">стр. <span class="num">{finding.evidence[0].page}</span></span>
                                {/if}
                                {#if finding.evidence[0].sheet}
                                  <span class="locator">лист <span class="num">{finding.evidence[0].sheet}</span></span>
                                {/if}
                                {#if finding.evidence[0].cell_range}
                                  <span class="locator">
                                    ячейки <span class="num">{finding.evidence[0].cell_range}</span>
                                  </span>
                                {/if}
                              {:else}
                                <span class="locator">
                                  доказательств нет: источник не указан
                                </span>
                              {/if}
                            </p>
                          </div>
                        {/each}
                      </div>
                    {/if}
                  </fieldset>

                  <Field
                    label="Формулировка исправленного утверждения"
                    name="fb-correction"
                    type="textarea"
                    rows={3}
                    placeholder="Новая редакция с сохранённым условием применения"
                    bind:value={correction}
                    error={findingId && correction.trim().length === 0
                      ? 'Без текста правки новая версия утверждения не появится.'
                      : ''}
                    hint="Правка создаёт новую версию утверждения; старая помечается заменённой."
                  />
                {:else}
                  <!-- «Нужна правка» остаётся отзывом: поля замены утверждения
                       просто нет, чтобы не вести читателя в отказ по праву. -->
                  <Notice tone="info" title="Новую формулировку утверждения выбирает эксперт">
                    Здесь вы описываете, что именно поправить. Выбирать утверждение и
                    писать новую формулировку доступно эксперту. Отзыв с комментарием
                    записывается и так.
                  </Notice>
                {/if}
              {/if}

              <div class="row verdict-form__submit">
                <Button type="submit" variant="action" busy={submitting} disabled={!formOpen}>
                  {submitting ? 'Отправка…' : verdict === 'correct' ? 'Отправить правку' : 'Отправить отзыв'}
                </Button>
                {#if !target}
                  <p class="micro muted">
                    Выберите ответ из журнала или оставьте комментарий без привязки к нему.
                  </p>
                {:else if verdict === 'correct' && canRestricted && !correctionReady}
                  <p class="micro muted">Нужны выбранное утверждение и текст правки.</p>
                {/if}
              </div>
            </form>
            </div>
          </div>
        </Panel>
      {:else}
        <Notice tone="info" title="Отзыв не записывается">
          {FEEDBACK_GATE} Форму отзыва скрыта, а ответы корпуса читаются в разделах
          «Вопрос» и «Находки».
        </Notice>
      {/if}

      {#if canReview}
        <section class="work__ledger">
          <SectionHead
            level="2"
            eyebrow="Предложения модели"
            title="Что модель предложила и чем это мерили"
            lead="В строках предложения, которые модель сформулировала по отзывам, и результаты замера на одних и тех же проверочных вопросах. Решение о применении принимает эксперт: сначала замер, затем «Принять»."
          >
            <div class="row work__actions">
              <Button variant="quiet" size="sm" icon="refresh" onclick={() => void reloadLedgers()}>
                Обновить журнал
              </Button>
            </div>
          </SectionHead>

          {#if reviewNotice}
            <div class="work__note">
              <Notice tone="ok" title="Решение записано">
                {reviewNotice}
                <div class="row work__actions">
                  <Button variant="quiet" size="sm" onclick={() => (reviewNotice = '')}>Понятно</Button>
                </div>
              </Notice>
            </div>
          {/if}
          {#if experimentNotice}
            <div class="work__note">
              <Notice tone="ok" title="Замер записан">
                {experimentNotice}
                <span class="micro notice__line">
                  Замер относится к текущему сеансу работы: позже его можно повторить.
                </span>
                <div class="row work__actions">
                  <Button variant="quiet" size="sm" onclick={() => (experimentNotice = '')}>Понятно</Button>
                </div>
              </Notice>
            </div>
          {/if}
          {#if reviewFailure}
            <div class="work__note">
              <Notice tone="error" title="Решение не принято">
                {reviewFailure}
                <span class="micro notice__line">
                  Кнопка «Принять» появляется после замера этого предложения.
                </span>
                <div class="row work__actions">
                  <Button variant="quiet" size="sm" onclick={() => (reviewFailure = '')}>Понятно</Button>
                </div>
              </Notice>
            </div>
          {/if}
          {#if experimentFailure}
            <div class="work__note">
              <Notice tone="error" title="Замер не выполнен">
                {experimentFailure}
                <div class="row work__actions">
                  <Button variant="quiet" size="sm" onclick={() => (experimentFailure = '')}>Понятно</Button>
                </div>
              </Notice>
            </div>
          {/if}
          {#if experimentsFailure}
            <div class="work__note">
              <Notice tone="error" title="Результаты замера не загружены">{experimentsFailure}</Notice>
            </div>
          {/if}
          {#if experimentsCaveat}
            <!-- Замер подставляется к предложению из показанной части списка: пока
                 за ней осталось непоказанное, «замера нет» может значить «его просто
                 не показали». -->
            <div class="work__note">
              <Notice tone="warn" title={FEEDBACK_FEED.stalledTitle}>
                {experimentsCaveat}
                {#if experimentsStalled}
                  <!-- Остаток назван, а страница пустая: смещение не сдвинулось, и
                     кнопка осталась бы пустым действием. Оговорка встаёт в строку
                     того же предупреждения, без нового стиля. -->
                  <span class="notice__line">{FEEDBACK_FEED.stalled}</span>
                {:else if experimentsLeft !== null && experimentsLeft > 0}
                  <div class="row work__actions">
                    <Button
                      variant="quiet"
                      size="sm"
                      busy={experimentsMoreLoading}
                      disabled={experimentsMoreLoading}
                      onclick={() => void loadExperimentsMore()}
                    >
                      {experimentsMoreLoading ? FEEDBACK_FEED.loadingMore : FEEDBACK_FEED.loadMore}
                    </Button>
                  </div>
                {/if}
                {#if experimentsMoreError}
                  <span class="micro notice__line">{experimentsMoreError}</span>
                {/if}
              </Notice>
            </div>
          {/if}

          {#if proposals.length === 0}
            <Empty
              icon="list"
              title="Предложений пока нет"
              body="Они появляются после отзывов здесь и в разделе «Вопрос»."
            >
              {#snippet action()}
                <Button variant="quiet" size="sm" icon="refresh" onclick={() => void reloadLedgers()}>
                  Обновить журнал
                </Button>
              {/snippet}
            </Empty>
          {:else}
            <div class="stack rows">
              {#each shownProposals as proposal (proposal.id)}
                {@const ab = experimentOf(proposal.id)}
                {@const promotable = promoteOf(proposal.id)}
                {@const measurable = proposal.kind === 'prompt' || proposal.kind === 'rule'}
                {@const busy = reviewingId === proposal.id || runningId === proposal.id}
                <Panel tone="sunk" tag="article">
                  <div class="row-card">
                    <div class="row-card__head">
                      <p class="row-card__kind">
                        {knownTerm(PROPOSAL_KIND_LABELS, proposal.kind) ?? PROPOSAL_KIND_FALLBACK}
                      </p>
                      <StatusPill status={PROPOSAL_TONE[proposal.status]} label={PROPOSAL_STATUS_LABELS[proposal.status]} />
                      <p class="micro muted row-card__src">
                        {#if proposal.created_at}
                          <time datetime={proposal.created_at}>{dateTime(proposal.created_at)}</time>
                        {/if}
                        {#if proposal.source_query_id === NIL_QUERY_ID}
                          без привязки к ответу
                        {:else}
                          по ответу на вопрос
                        {/if}
                      </p>
                    </div>

                    <h3 class="h4">{proposal.title}</h3>
                    <p class="quote row-card__change">{proposal.change}</p>
                    {#if proposal.impact.length > 0}
                      <p class="row row-card__impact">
                        <span class="micro muted">затрагивает</span>
                        {#each proposal.impact as area (area)}<span class="tag">{area}</span>{/each}
                      </p>
                    {/if}

                    {#if ab}
                      <div class="stack ab">
                        <!-- Три подписанных факта замера отдельными элементами:
                             число вопросов, доля принятых ответов своими глазами
                             (с какой и на какую) и вывод замера. «Зачёт» и
                             процентные пункты здесь не работают: величина видна
                             по двум числам. -->
                        <p class="micro ab__title">
                          <span class="ab__fact">
                            <span class="ab__key">{AB_STRINGS.casesTitle}</span>
                            <span class="num">{ab.cases}</span>
                          </span>
                          <span class="ab__fact">
                            <span class="ab__key">{AB_METRIC_LABELS.pass_rate}</span>
                            {#if ab.delta_pass_rate > 0}
                              <span>
                                {AB_STRINGS.passUp} с <span class="num">{pct(ab.baseline.pass_rate)}</span>
                                до <span class="num">{pct(ab.candidate.pass_rate)}</span>
                              </span>
                            {:else if ab.delta_pass_rate < 0}
                              <span>
                                {AB_STRINGS.passDown} с <span class="num">{pct(ab.baseline.pass_rate)}</span>
                                до <span class="num">{pct(ab.candidate.pass_rate)}</span>
                              </span>
                            {:else}
                              <span>{AB_STRINGS.passSame}: <span class="num">{pct(ab.baseline.pass_rate)}</span></span>
                            {/if}
                          </span>
                          <span class="ab__fact">
                            <span class="ab__key">{AB_STRINGS.decisionTitle}</span>
                            <strong>{ab.decision === 'promote' ? AB_STRINGS.promote : AB_STRINGS.hold}</strong>
                          </span>
                        </p>
                        <div class="ab__grid">
                          {#each metricRows(ab) as row (row.key)}
                            <div class="ab__metric">
                              <p class="micro ab__label">{row.label}, чем {row.better}, тем лучше</p>
                              <div class="ab__line">
                                <span class="micro ab__who">{AB_STRINGS.base}</span>
                                <span class="bar ab__track">
                                  {#if row.baseWidth !== null}
                                    <span class="bar__fill" style="width: {row.baseWidth}%"></span>
                                  {/if}
                                </span>
                                <span class="micro num ab__val">{row.baseText}</span>
                              </div>
                              <div class="ab__line">
                                <span class="micro ab__who">{AB_STRINGS.candidate}</span>
                                <span class="bar ab__track">
                                  {#if row.candWidth !== null}
                                    <span
                                      class="bar__fill {ab.decision === 'promote' ? 'bar__fill--consensus' : 'bar__fill--disputed'}"
                                      style="width: {row.candWidth}%"></span>
                                  {/if}
                                </span>
                                <span class="micro num ab__val">{row.candText}</span>
                              </div>
                            </div>
                          {/each}
                        </div>
                        <!-- Стоимость замера (токены, повторы модели, процентиль
                             задержки) к решению о предложении не относится: это
                             показатели контура, их место в разделе «Качество». Здесь
                             то, что меняет решение: ухудшения на вопросах. -->
                        <dl class="kv ab__extra">
                          <dt>{AB_STRINGS.worseCases}</dt>
                          <dd class="num {ab.regressions.length > 0 ? 'ab__bad' : ''}">
                            {num(ab.regressions.length)}
                          </dd>
                        </dl>
                        {#if ab.regressions.length > 0}
                          <details class="ab__svc">
                            <summary class="micro">Служебные данные</summary>
                            <p class="micro ab__regress">
                              Номера вопросов с ухудшением, по ним замер сверяют с журналом:
                              <code class="ab__codes">{ab.regressions.join(', ')}</code>
                            </p>
                          </details>
                        {/if}
                      </div>
                    {:else if measurable && !experimentsFailure}
                      <p class="micro ab__none">{AB_STRINGS.notMeasured}</p>
                    {:else}
                      <p class="micro ab__none">
                        Замер на одних и тех же вопросах поддержан для настройки вопросов
                        модели и правила отбора: для
                        «{knownTerm(PROPOSAL_KIND_LABELS, proposal.kind) ?? PROPOSAL_KIND_FALLBACK}»
                        сравнения чисел не будет.
                      </p>
                    {/if}

                    <div class="row row-card__foot">
                      <!-- Раздел предложений рендерится только там, где право проверки
                           есть: отдельной ветки «кнопок нет» для базового пользователя
                           здесь больше не нужно. -->
                      {#if measurable && canEvaluate}
                        <Button
                          variant="quiet"
                          size="sm"
                          icon="gauge"
                          busy={runningId === proposal.id}
                          disabled={busy}
                          onclick={() => void runAbExperiment(proposal)}>
                          {runningId === proposal.id ? 'Измеряем…' : 'Измерить на вопросах'}
                        </Button>
                      {:else if measurable && !canEvaluate}
                        <p class="micro muted">
                          Это действие доступно эксперту. Право выдаёт администратор сервиса.
                        </p>
                      {/if}

                      {#if proposal.status === 'proposed'}
                        {#if promotable}
                          <Button
                            variant="action"
                            size="sm"
                            icon="check"
                            busy={reviewingId === proposal.id}
                            disabled={busy}
                            onclick={() => void decide(proposal, true)}>
                            {reviewingId === proposal.id ? 'Записываем…' : 'Принять'}
                          </Button>
                        {:else}
                          <p class="micro muted row-card__gate">
                            «Принять» появится после замера этого предложения.
                          </p>
                        {/if}
                        <Button
                          variant="ink"
                          size="sm"
                          busy={reviewingId === proposal.id}
                          disabled={busy}
                          onclick={() => void decide(proposal, false)}>
                          {reviewingId === proposal.id ? 'Записываем…' : 'Отклонить'}
                        </Button>
                      {:else if proposal.status === 'accepted'}
                        <Button
                          variant="quiet"
                          size="sm"
                          icon="refresh"
                          busy={reviewingId === proposal.id}
                          disabled={busy}
                          onclick={() => void decide(proposal, false)}>
                          {reviewingId === proposal.id ? 'Записываем…' : 'Отменить принятие'}
                        </Button>
                        <p class="micro muted row-card__gate">
                          Отмена принятия переводит предложение в «отклонено»: статус «ожидает
                          решения» не возвращается.
                        </p>
                      {:else if promotable}
                        <Button
                          variant="action"
                          size="sm"
                          icon="check"
                          busy={reviewingId === proposal.id}
                          disabled={busy}
                          onclick={() => void decide(proposal, true)}>
                          {reviewingId === proposal.id ? 'Записываем…' : 'Вернуть в принято'}
                        </Button>
                        <p class="micro muted row-card__gate">
                          Решение опирается на замер этого предложения на одних и тех же
                          вопросах. Новое решение перезаписывает прежнее.
                        </p>
                      {:else}
                        <p class="micro muted row-card__gate">
                          Вернуть «отклонено» в «принято» можно только после замера: без него
                          кнопки нет.
                        </p>
                      {/if}
                    </div>
                  </div>
                </Panel>
              {/each}
            </div>

            <div class="work__pager">
              <!-- Знаменателя у этого списка нет: журнал предложений приходит
                 массивом, полного числа экран не знает и «все» не обещает. -->
              <p class="micro">{feedShownOf(shownProposals.length, null, PROPOSAL_NOUNS)}</p>
              <div class="row">
                {#if proposals.length > shownProposals.length}
                  <Button variant="quiet" size="sm" onclick={() => (proposalLimit += PROPOSAL_PAGE_SIZE)}>
                    {FEEDBACK_FEED.loadMore}
                  </Button>
                {:else if proposalsAtCap}
                  <Button variant="quiet" size="sm" icon="refresh" onclick={() => void reloadLedgers()}>
                    Обновить журнал
                  </Button>
                {/if}
              </div>
            </div>
            <!-- Буфер кончился на потолке загрузки, а не на конце журнала: обрыв
                 называется строкой, чтобы «Показано 200» не читалось как «всё». -->
            {#if proposalsAtCap}
              <p class="micro muted">{proposalsCappedNote(PROPOSAL_READ_SIZE)}</p>
            {/if}

            <div class="row work__actions">
              <Button href="/findings" variant="ghost" size="sm">{navLabel('/findings')}</Button>
              <Button href="/dashboard" variant="ghost" size="sm">{navLabel('/dashboard')}</Button>
            </div>
          {/if}
        </section>
      {/if}
    {/if}
  </div>
</div>

{#if historyOpen}
  <Sheet
    title="Цепочка версий утверждения"
    description="Все версии утверждения: чем заменена каждая и кто внёс правку."
    onclose={closeHistory}>
    <!-- Предмет шторки виден до ответа сервера: человек читает формулировку
         утверждения, а служебный номер уходит под «Служебные данные» им нужна
         сверка с «Находками», а не чтение. -->
    <p class="micro muted">
      {historyClaimText || 'Формулировку утверждения сервис не прислал: сверяйте цепочку по служебному номеру.'}
    </p>
    {@render serviceNote(historyClaim)}
    {#if historyBusy}
      <div class="row hist__busy"><span class="spinner spinner--quiet"></span> Читаем версии…</div>
    {:else if historyFailure}
      <Notice tone="error" title="История не открылась">{historyFailure}</Notice>
    {:else if history}
      <div class="stack hist">
        <p class="micro muted">
          Версий в цепочке: <span class="num">{num(history.versions.length)}</span>
        </p>
        {#each history.versions as version (version.finding_id)}
          {@const replacement = version.superseded_by ? versionNumberById(version.superseded_by) : null}
          <div class="hist__row" data-flag={version.superseded_by ? 'out' : undefined}>
            <p class="hist__head">
              <span class="micro">версия</span>
              <strong class="num">{version.version}</strong>
              <StatusPill status={version.status} label={STATUS_SHORT[version.status]} />
            </p>
            <p class="small">{version.statement}</p>
            <!-- Факты решения отдельными подписанными элементами: никто не
                 собирает их в одну строку через разделитель. -->
            <p class="micro muted hist__meta">
              {#if version.reviewer_id}
                <span class="hist__fact">
                  <span class="hist__key">кто внёс правку</span> {reviewerOf(version.reviewer_id)}
                </span>
              {/if}
              {#if version.review_date}
                <span class="hist__fact">
                  <span class="hist__key">дата решения</span>
                  <time datetime={version.review_date}>{dateTime(version.review_date)}</time>
                </span>
              {/if}
              {#if version.review_reason}
                <span class="hist__fact">
                  <span class="hist__key">почему так решили</span> {version.review_reason}
                </span>
              {/if}
              {#if version.superseded_by}
                <span class="hist__fact">
                  <span class="hist__key">чем заменена</span>
                  {#if replacement}
                    версией <span class="num">{replacement}</span>
                  {:else}
                    новой версии в этой цепочке нет
                  {/if}
                </span>
              {/if}
            </p>
          </div>
        {/each}
      </div>
    {:else}
      <p class="micro muted">Версий нет.</p>
    {/if}
    {#snippet footer()}
      <Button variant="quiet" onclick={closeHistory}>Закрыть</Button>
    {/snippet}
  </Sheet>
{/if}

<style>
  .work .wrap {
    display: flex;
    flex-direction: column;
    gap: var(--s6);
  }

  .work__actions {
    justify-content: flex-start;
    margin-top: var(--s3);
  }

  .work__note {
    margin-block: var(--s4);
  }

  /* Notice рендерит детей внутри span: строки держим блочными span'ами. */
  .notice__line {
    display: block;
  }

  .notice__line + .notice__line {
    margin-top: var(--s2);
  }

  .work__loading {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    padding: var(--s6);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-xl);
    background: var(--surface-raised);
  }

  .work__region {
    --gap: var(--s4);
  }

  .work__region .panel__head {
    margin-bottom: var(--s4);
  }

  /* ── Ответ, на который пишут отзыв ────────────────────────────────────── */
  /* Вопрос корпусу задают в разделе «Вопрос»: здесь остаётся одна строка-
     подсказка с переходом, а не второй композер. */
  .work__ask {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s3) var(--s5);
    flex-wrap: wrap;
    padding: var(--s3) var(--s4);
    border: 1px dashed var(--line-strong);
    border-radius: var(--r-lg);
  }

  .work__ask p {
    flex: 1 1 42ch;
    margin: 0;
    text-wrap: pretty;
  }

  .freeform {
    display: flex;
    align-items: center;
    gap: var(--s4);
    flex-wrap: wrap;
    padding-top: var(--s4);
    border-top: 1px dashed var(--line-strong);
  }

  .freeform p {
    flex: 1 1 42ch;
  }

  .target {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    margin-top: var(--s4);
    padding: var(--s5);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    background: var(--sage);
  }

  .target__label {
    color: var(--ink-3);
  }

  .target__provenance {
    text-wrap: pretty;
  }

  .target__facts {
    gap: var(--s1) var(--s5);
    margin-top: var(--s2);
  }

  .runs {
    --gap: var(--s2);
    margin-top: var(--s4);
  }

  .run {
    display: flex;
    align-items: center;
    gap: var(--s3) var(--s4);
    flex-wrap: wrap;
    padding: var(--s3) var(--s4);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-pill);
    background: var(--surface-sunk);
  }

  /* ── Вердикт и комментарий ───────────────────────────────────────────── */
  .verdict-form {
    --gap: var(--s5);
  }

  /* Fieldset без рамки: группа держит близость и legend, а не коробку. */
  .verdict-form__block,
  .picks {
    margin: 0;
    padding: 0;
    border: 0;
    min-width: 0;
  }

  .verdict-form__block {
    --gap: var(--s2);
  }

  /* Радио остаётся доступным: у поля есть фокус, а подпись рисует pill-состояние. */
  .verdict {
    display: inline-flex;
  }

  .verdict__input,
  .pick__input {
    appearance: none;
    width: var(--s4);
    height: var(--s4);
    margin: 0;
    flex: none;
    border: 1px solid var(--line-strong);
    border-radius: var(--r-pill);
    background: var(--surface);
    cursor: pointer;
    transition: background var(--dur-fast) var(--ease-soft), border-color var(--dur-fast) var(--ease-soft);
  }

  .verdict__input:checked,
  .pick__input:checked {
    border-color: var(--action-deep);
    background: var(--action-deep);
    box-shadow: inset 0 0 0 3px var(--surface);
  }

  .verdict__input:focus-visible,
  .pick__input:focus-visible {
    outline: 2px solid var(--action-ink);
    outline-offset: 2px;
  }

  .verdict__chip {
    cursor: pointer;
  }

  .verdict-form__submit {
    justify-content: flex-start;
    gap: var(--s4);
  }

  .picks {
    --gap: var(--s2);
  }

  .picks__list {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  .pick {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
    padding: var(--s2) var(--s4);
    border: 1px solid var(--line);
    border-radius: var(--r-md);
    background: var(--surface);
    transition: background var(--dur-fast) var(--ease-soft), border-color var(--dur-fast) var(--ease-soft);
  }

  .pick:hover {
    border-color: var(--line-strong);
    background: var(--surface-raised);
  }

  .pick[data-on='true'] {
    border-color: var(--action-deep);
    background: var(--peach-wash);
    box-shadow: var(--shadow-soft);
  }

  .pick__main {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex: 1 1 26ch;
    min-width: 0;
    padding: var(--s2) 0;
    text-align: left;
    cursor: pointer;
  }

  .pick__locs {
    display: flex;
    align-items: baseline;
    gap: var(--s2) var(--s4);
    flex: 1 0 100%;
    flex-wrap: wrap;
    padding-bottom: var(--s2);
    border-top: 1px solid var(--line-soft);
  }

  /* ── Строки разбора предложений ──────────────────────────────────────── */
  .work__ledger {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    margin-top: var(--s7);
    padding-top: var(--s7);
    border-top: 1px solid var(--line);
  }

  .rows {
    --gap: var(--s4);
  }

  /* Итог списка предложений: счётчик и единственная кнопка «Показать ещё» в
     одной строке, на узком экране кнопка переносится под счётчик. */
  .work__pager {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s3) var(--s5);
    flex-wrap: wrap;
    margin-top: var(--s4);
    padding-top: var(--s4);
    border-top: 1px solid var(--line-soft);
  }

  .work__pager p {
    margin: 0;
  }

  .row-card {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    border-radius: var(--r-lg);
  }

  .row-card__head {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .row-card__kind {
    font-size: var(--t-small);
    font-weight: 600;
    color: var(--ink);
  }

  .row-card__src {
    margin-left: auto;
  }

  .row-card__change {
    text-wrap: pretty;
  }

  .row-card__impact {
    justify-content: flex-start;
    gap: var(--s2);
  }

  .row-card__foot {
    justify-content: flex-start;
    gap: var(--s4);
    padding-top: var(--s3);
    border-top: 1px solid var(--line);
  }

  .row-card__gate {
    max-width: var(--maxw-measure);
  }

  /* ── A/B: только измеренные значения ─────────────────────────────────── */
  .ab {
    --gap: var(--s3);
    padding: var(--s4);
    border: 1px solid var(--line);
    border-radius: var(--r-md);
    background: var(--surface);
  }

  .ab__title {
    display: flex;
    gap: var(--s2) var(--s5);
    flex-wrap: wrap;
    color: var(--ink-2);
  }

  /* Каждый счётчик замера со своей подписью: числа не склеиваются в строку. */
  .ab__fact {
    display: inline-flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
    min-width: 0;
  }

  .ab__key {
    color: var(--ink-3);
  }

  .ab__title strong {
    color: var(--ink);
    font-weight: 600;
  }

  .ab__grid {
    display: grid;
    gap: var(--s4);
    grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr));
  }

  .ab__metric {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
  }

  .ab__label {
    color: var(--ink-3);
  }

  /* Столбцы по содержимому: моно-число вида «12 345,6 мс» не должно резаться
     фиксированной дорожкой, сжимается только полоса. */
  .ab__line {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--s2);
  }

  .ab__who {
    color: var(--ink-3);
  }

  .ab__track {
    height: 8px;
  }

  .ab__val {
    font-size: var(--t-micro);
    text-align: right;
    color: var(--ink-2);
  }

  .ab__extra {
    gap: var(--s1) var(--s5);
  }

  /* Служебные величина и номера под раскрытием: они нужны, чтобы сверить запись
     с журналом, а не для чтения решения. Один блок на оба раскрытия экрана. */
  .ab__svc,
  .work__svc {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
  }

  .ab__svc summary,
  .work__svc summary {
    cursor: pointer;
    color: var(--ink-3);
  }

  .ab__regress {
    margin: 0;
    color: var(--ink-3);
  }

  /* Моно-номера кейсов — подчинённая строка, а не заголовок метрики. */
  .ab__codes {
    font-family: var(--font-data);
    color: var(--ink-3);
    word-break: break-word;
  }

  .ab__bad {
    color: var(--disputed);
  }

  .ab__none {
    color: var(--ink-3);
  }

  /* ── Шторка истории версий ───────────────────────────────────────────── */
  .hist {
    --gap: var(--s3);
  }

  .hist__busy {
    gap: var(--s3);
  }

  .hist__row {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
    padding: var(--s4);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-md);
    background: var(--surface-sunk);
  }

  .hist__row[data-flag='out'] {
    background: var(--superseded-wash);
  }

  .hist__head {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
  }

  .hist__meta {
    display: flex;
    gap: var(--s2) var(--s5);
    flex-wrap: wrap;
  }

  /* Факт решения своим подписанным элементом: ключ словами и значение рядом,
     без разделителей между фактами в одной строке. */
  .hist__fact {
    display: inline-flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
    min-width: 0;
  }

  .hist__key {
    color: var(--ink-3);
  }

  @media (max-width: 900px) {
    .work__ask {
      align-items: flex-start;
    }

    .row-card__src {
      margin-left: 0;
    }

    .ab__line {
      grid-template-columns: auto minmax(0, 1fr) auto;
    }
  }
</style>
