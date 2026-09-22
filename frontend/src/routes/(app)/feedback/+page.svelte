<script lang="ts">
  // Проверка решений — верстак: основание отзыва, вердикт с комментарием и
  // разбор предложений эволюции с настоящими числами A/B-прогонов. Решение
  // принимает эксперт; без расширенного доступа экран объясняет, что именно
  // недоступно, а не показывает мёртвую кнопку.
  import { api, ApiError } from '$lib/api';
  import { countOf, num, pct } from '$lib/format';
  import { session } from '$lib/sessionStore.svelte';
  import { PREDICATE_LABELS, SUBJECT_LABELS, STATUS_SHORT, knownTerm, termOf } from '$lib/terms';
  import {
    type ClaimHistory,
    type EvaluationRun,
    type EvolutionExperiment,
    type EvolutionProposal,
    type FindingListItem,
    type ModelMode,
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

  const VERDICTS: { key: 'accept' | 'reject' | 'correct'; label: string }[] = [
    { key: 'accept', label: 'ответ принят' },
    { key: 'reject', label: 'ответ отклонён' },
    { key: 'correct', label: 'нужна правка' },
  ];

  // Журнал предложений порциями: список растёт вместе с корпусом, и 500
  // предложений не должны становиться одним бесконечным листом.
  const PROPOSAL_PAGE_SIZE = 10;
  // Прогони оценки приходят пачкой до нескольких сотен — листуем и их.
  const RUNS_PAGE_SIZE = 8;

  const PROPOSAL_STATUS: Record<EvolutionProposal['status'], string> = {
    proposed: 'ожидает решения',
    accepted: 'принято',
    rejected: 'отклонено',
  };

  // Статус предложения показываем тем же языком статусов, что и утверждения:
  // ждёт решения — гипотеза, принято — консенсус, отклонено — заменённая версия.
  const PROPOSAL_TONE: Record<EvolutionProposal['status'], 'hypothesis' | 'consensus' | 'superseded'> = {
    proposed: 'hypothesis',
    accepted: 'consensus',
    rejected: 'superseded',
  };

  const KIND_RU: Record<string, string> = {
    prompt: 'политика промпта',
    rule: 'правило отбора',
    alias: 'алиас сущности',
    gold_case: 'эталонный кейс',
  };

  const EXPERT_GATE = 'Экспертные действия доступны при расширенном доступе.';

  // Классы отказа различаем, чтобы подобрать верную формулировку и тон плашки,
  // но человеку показываем что не получилось и что нажать — без кодов ответа.
  type Failure = { kind: 'model' | 'backend' | 'denied' | 'conflict' | 'other'; text: string };
  type Target = {
    queryId: string;
    provenance: string;
    createdAt?: string;
    summary?: string;
    confidence?: number;
    modelMode?: ModelMode;
    findings: FindingListItem[];
    evaluation?: EvaluationRun;
  };

  // FeedbackRequest.query_id — UUID без исключения. Свободный комментарий,
  // который не указывает на конкретный ответ, уходит с nil-UUID: сервер его
  // принимает, и в журнале предложений он читается как «не привязан».
  const NIL_QUERY_ID = '00000000-0000-0000-0000-000000000000';

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

  /** Сбой по-человечески: что не вышло и одно действие. */
  function failureText(failure: Failure): string {
    if (failure.kind === 'denied') return EXPERT_GATE;
    if (failure.kind === 'conflict') {
      return 'Что заменять, уже не найдено: версия утверждения изменилась. Перечитайте указатель находок и выберите актуальную версию.';
    }
    if (failure.kind === 'model') {
      return 'Модель не ответила, поэтому предложение не сформулировано. Повторите позже — экспертное решение можно записать и сейчас.';
    }
    if (failure.kind === 'backend') return 'Показания не пришли. Проверьте соединение и повторите отправку.';
    return 'Не получилось. Проверьте соединение и повторите попытку.';
  }

  // Даты форматированием чисел в $lib/format не покрыты, поэтому локально
  // остаётся только человекочитаемый вывод времени.
  function ruDate(iso: string): string {
    const date = new Date(iso);
    return Number.isNaN(date.getTime())
      ? iso
      : date.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
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
  let proposals = $state<EvolutionProposal[]>([]);
  // Сколько предложений листа открыто: порция растёт действием читателя, а не
  // молчаливой прокруткой всего журнала.
  let proposalLimit = $state(PROPOSAL_PAGE_SIZE);
  let runsShown = $state(RUNS_PAGE_SIZE);
  let experiments = $state<EvolutionExperiment[]>([]);
  let corpus = $state<FindingListItem[]>([]);
  let softFailure = $state('');

  let target = $state<Target | null>(null);
  let questionDraft = $state('');
  let asking = $state(false);
  let askFailure = $state<Failure | null>(null);

  let verdict = $state<'accept' | 'reject' | 'correct' | null>(null);
  let comment = $state('');
  let findingId = $state('');
  let findingQuery = $state('');
  let correction = $state('');
  let submitting = $state(false);
  // Attempt, а не «форма невалидна»: претензии показываются после попытки,
  // а не как выговор за ещё не тронутый лист.
  let submitAttempted = $state(false);
  let result = $state<FeedbackResult | null>(null);
  let submitFailure = $state<Failure | null>(null);

  let reviewingId = $state('');
  let reviewFailure = $state('');
  let reviewNotice = $state('');
  let runningId = $state('');
  let experimentFailure = $state('');
  let experimentNotice = $state('');

  // История версий утверждения живёт в отдельной шторке: цепочка версий по id находки.
  let historyOpen = $state(false);
  let historyBusy = $state(false);
  // Утверждение, чья цепочка открыта: шторка обязана называть свой предмет и
  // тогда, когда запрос версий не прошёл — данных истории в этот момент нет.
  let historyClaim = $state('');
  let history = $state<ClaimHistory | null>(null);
  let historyFailure = $state('');
  // Утверждение, правку которого только что записали: по нему и открываем цепочку.
  let lastCorrected = $state('');

  /** Русская связка темы или пустая строка: сырой ключ в текст не подставляем. */
  function topicOf(finding: FindingListItem): string {
    const subject = knownTerm(SUBJECT_LABELS, finding.subject);
    const predicate = knownTerm(PREDICATE_LABELS, finding.predicate);
    return subject && predicate ? `${subject} · ${predicate}` : '';
  }

  const commentReady = $derived(comment.trim().length >= 3);
  const correctionReady = $derived(
    verdict === 'correct' && canRestricted && Boolean(findingId) && correction.trim().length > 0,
  );
  // Новая версия замены утверждения создаётся только в закрытом контуре доступа;
  // остальным вердикт «нужна правка» остаётся комментарием к ответу.
  const correctionRequired = $derived(verdict === 'correct' && canRestricted);

  // Кнопка не гаснет из-за незаполненных полей: иначе у обязательного действия
  // нет пути с клавиатуры, а причина становится видна только после попытки.
  const formOpen = $derived(Boolean(target) && !submitting);
  // Претензия к вердикту появляется после попытки отправки, а не на пустой форме.
  const verdictMissing = $derived(submitAttempted && !verdict);
  const commentError = $derived(
    commentReady || (!submitAttempted && comment.trim().length === 0)
      ? ''
      : comment.trim().length === 0
        ? 'Комментарий — основание отзыва: без него отзыв не принимается.'
        : `Нужен комментарий от 3 символов: сейчас ${comment.trim().length}.`,
  );

  const candidates = $derived.by(() => {
    const pool = target?.findings.length ? target.findings : corpus;
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

  const openCount = $derived(proposals.filter((item) => item.status === 'proposed').length);
  // Журнал предложений растёт вместе с корпусом: лист разбит на порции, чтобы
  // пятьсот предложений не становились одним DOM-полотном.
  const shownProposals = $derived(proposals.slice(0, proposalLimit));

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

  /** Полосы — только измеренные значения; масштаб: метрика или максимум пары. */
  function metricRows(ab: EvolutionExperiment): MetricRow[] {
    const latencyMax = Math.max(ab.baseline.average_latency_ms, ab.candidate.average_latency_ms);
    const rows: MetricSeed[] = [
      {
        key: 'pass_rate',
        label: 'зачёт кейсов',
        base: ab.baseline.pass_rate,
        cand: ab.candidate.pass_rate,
        better: 'больше',
      },
      {
        key: 'source_recall',
        label: 'полнота источников',
        base: ab.baseline.source_recall,
        cand: ab.candidate.source_recall,
        better: 'больше',
      },
      {
        key: 'citation_coverage',
        label: 'полнота цитат',
        base: ab.baseline.citation_coverage,
        cand: ab.candidate.citation_coverage,
        better: 'больше',
      },
      {
        key: 'latency',
        label: 'средняя задержка',
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
      provenance: 'прогон оценки из журнала',
      createdAt: run.created_at,
      findings: [],
      evaluation: run,
    };
    clearForm();
  }

  function chooseFreeForm(): void {
    target = {
      queryId: NIL_QUERY_ID,
      provenance: 'комментарий без привязки к ответу',
      findings: [],
    };
    clearForm();
  }

  function dropTarget(): void {
    target = null;
    clearForm();
  }

  async function askQuestion(): Promise<void> {
    const question = questionDraft.trim();
    if (question.length < 3) return;
    asking = true;
    askFailure = null;
    try {
      const feedbackResponse = await api.query(question);
      target = {
        queryId: feedbackResponse.answer.query_id,
        provenance: `ответ на вопрос «${feedbackResponse.answer.question}»`,
        createdAt: feedbackResponse.evaluation.created_at,
        summary: feedbackResponse.answer.summary,
        confidence: feedbackResponse.answer.confidence,
        modelMode: feedbackResponse.answer.model_mode,
        findings: feedbackResponse.answer.findings,
        evaluation: feedbackResponse.evaluation,
      };
      clearForm();
    } catch (reason) {
      askFailure = classify(reason);
    } finally {
      asking = false;
    }
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
        // Цепочку версий читаем сразу и открываем в шторке: правку без
        // показанной цепочки не проверяешь глазами.
        await loadHistory(correctedFinding, true);
        try {
          corpus = await api.findings();
        } catch {
          softFailure = 'Правка записана, но указатель находок не перечитан. Обновите лист позже.';
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

  async function reloadLedgers(): Promise<void> {
    softFailure = '';
    try {
      const [proposalsResult, experimentsResult] = await Promise.allSettled([
        api.proposals(),
        api.experiments(),
      ]);
      if (proposalsResult.status === 'fulfilled') proposals = proposalsResult.value;
      else softFailure = 'Журнал предложений не перечитан.';
      if (experimentsResult.status === 'fulfilled') experiments = experimentsResult.value;
      else softFailure = 'Результаты A/B-прогонов не перечитаны.';
    } catch {
      softFailure = 'Лист не перечитан: проверьте соединение и повторите попытку.';
    }
  }

  async function decide(proposal: EvolutionProposal, decision: boolean): Promise<void> {
    if (!canReview) {
      reviewFailure = EXPERT_GATE;
      return;
    }
    reviewingId = proposal.id;
    reviewFailure = '';
    reviewNotice = '';
    try {
      const updated = await api.review(proposal.id, decision);
      proposals = proposals.map((item) => (item.id === updated.id ? updated : item));
      reviewNotice = `«${updated.title}» · ${PROPOSAL_STATUS[updated.status]}`;
    } catch (reason) {
      const failureKind = classify(reason);
      reviewFailure =
        failureKind.kind === 'denied'
          ? EXPERT_GATE
          : failureKind.kind === 'conflict'
            ? 'Сначала нужен A/B-прогон этого предложения — без замера решение не записывается.'
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
      experimentNotice = `A/B-прогон «${proposal.title}»: решение ${ab.decision === 'promote' ? 'продвигать' : 'не продвигать'}`;
    } catch {
      experimentFailure = 'A/B-прогон не выполнен. Проверьте соединение и повторите прогон.';
    } finally {
      runningId = '';
    }
  }

  // Цепочка версий утверждения: все версии с тем, кто проверял и чем заменено.
  async function loadHistory(claimId: string, afterCorrection = false): Promise<void> {
    historyClaim = claimId;
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
          ? EXPERT_GATE
          : 'Цепочка версий не открылась. Проверьте соединение и повторите.';
      // Правка уже записана — потеря цепочки не отменяет её, но скрывает замены.
      // Два факта называются отдельно: «записано» и «не открылось» не склеиваются
      // в одну фразу, которая на отказе доступа звучала бы бессмыслицей.
      if (afterCorrection) {
        softFailure =
          failureKind.kind === 'denied'
            ? 'Правка записана. Цепочку версий показать нельзя: утверждение относится к классу данных, который аккаунту не открыт.'
            : 'Правка записана. Цепочка версий не прочитана — откройте её позже кнопкой «Цепочка версий».';
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
    softFailure = '';
    const [proposalsResult, findingsResult, runsResult, experimentsResult] = await Promise.allSettled([
      api.proposals(),
      canRead ? api.findings() : Promise.resolve([] as FindingListItem[]),
      canEvaluate ? api.evaluations() : Promise.resolve([] as EvaluationRun[]),
      api.experiments(),
    ]);
    if (proposalsResult.status === 'rejected') {
      failure = classify(proposalsResult.reason);
      phase = 'failed';
      return;
    }
    proposals = proposalsResult.value;
    experiments = experimentsResult.status === 'fulfilled' ? experimentsResult.value : [];
    corpus = findingsResult.status === 'fulfilled' ? findingsResult.value : [];
    if (runsResult.status === 'fulfilled') runs = runsResult.value;
    else runsFailure = 'Не удалось прочитать журнал прогонов. Обновите лист ещё раз.';
    phase = 'ready';
  }

  // Доступ приходит вместе с подтверждённым входом: лист пересобирается,
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
</script>

<svelte:head>
  <title>Проверка решений — Научный Клубок</title>
  <meta
    name="description"
    content="Верстак проверки: отзыв по конкретному ответу системы, экспертная правка утверждения с новой версией, разбор предложений эволюции и результатов A/B-прогонов."
  />
</svelte:head>

<div class="page work">
  <div class="wrap">
    <SectionHead
      level="1"
      eyebrow="Проверка решений · верстак"
      title="Отзыв, правка и разбор предложений"
      lead="Отзыв привязывается к конкретному ответу системы, правка — к утверждению с доказательством. Модель лишь формулирует предложение эволюции: решение о применении принимает эксперт — сначала A/B-прогон, затем «Принять».">
      {#if phase === 'ready'}
        <p class="micro muted work__counts">
          предложений <span class="num">{proposals.length}</span> · ожидают решения
          <span class="num">{openCount}</span> · прогонов
          <span class="num">{runs.length}</span> · A/B <span class="num">{experiments.length}</span>
        </p>
      {/if}
    </SectionHead>

    {#if !canGive && !canReview}
      <Notice tone="warn" title="Проверка решений недоступна">
        {EXPERT_GATE} Журнал предложений не читается, отзывы и правки не записываются.
        <div class="row work__actions">
          <Button href="/findings" variant="quiet" size="sm">Указатель находок</Button>
          <Button href="/research" variant="ghost" size="sm">Рабочее пространство запроса</Button>
        </div>
      </Notice>
    {:else if phase === 'loading'}
      <div class="work__loading" role="status" aria-label="Считываем показания корпуса">
        <p class="eyebrow"><span class="spinner spinner--quiet"></span> Считываем показания корпуса</p>
        <div class="skeleton" style="height:120px"></div>
        <div class="skeleton" style="height:120px"></div>
        <p class="micro muted">
          Предложения эволюции, результаты A/B-прогонов, журнал прогонов оценки и указатель находок.
        </p>
      </div>
    {:else if phase === 'failed'}
      <Notice tone={failure?.kind === 'denied' ? 'warn' : 'error'} title="Лист не собран">
        {failure ? failureText(failure) : 'Лист не собран. Проверьте соединение и повторите попытку.'}
        <div class="row work__actions">
          {#if failure?.kind === 'denied'}
            <!-- Повтор отказа по праву даёт тот же отказ: здесь только то, что
                 действительно двигает решение. -->
            <Button variant="quiet" size="sm" href="/account">Что открыто моему аккаунту</Button>
            <Button variant="ghost" size="sm" href="/findings">Указатель находок</Button>
          {:else}
            <Button variant="action" size="sm" icon="refresh" onclick={() => void loadAll()}>Пересобрать лист</Button>
          {/if}
        </div>
      </Notice>
    {:else}
      {#if softFailure}
        <Notice tone="error" title="Лист прочитан не полностью">
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
                <p class="eyebrow"><Icon name="target" size={16} /> Основание отзыва</p>
                <h2 class="h3">Ответ, который проверяем</h2>
                <p class="micro muted">
                  Отзыв привязывается к конкретному ответу: возьмите прогон из журнала ниже или
                  спросите корпус прямо здесь.
                </p>
              </div>
            </div>

            {#if askFailure}
              <div class="work__note">
                <Notice tone="error" title="Ответ не получен">
                  {failureText(askFailure)}
                </Notice>
              </div>
            {/if}

            <form class="ask" onsubmit={(event) => { event.preventDefault(); void askQuestion(); }}>
              <Field
                label="Вопрос корпусу — чтобы разобрать свежий ответ"
                name="ask-question"
                placeholder="напр. какие пределы по сухому остатку для шахтной воды"
                bind:value={questionDraft}
                onenter={() => void askQuestion()}
                hint="Тот же запрос, что и в рабочем пространстве."
              />
              <Button type="submit" variant="action" busy={asking} disabled={questionDraft.trim().length < 3}>
                {asking ? 'Прогон запроса…' : 'Спросить'}
              </Button>
            </form>

            {#if target}
              <div class="target">
                <p class="micro target__label">Основание выбрано</p>
                <p class="small target__provenance">
                  {target.provenance}
                  {#if target.createdAt}
                    · <time class="num" datetime={target.createdAt}>{ruDate(target.createdAt)}</time>
                  {/if}
                </p>
                {#if target.summary}<p class="small target__summary">{target.summary}</p>{/if}
                <dl class="kv target__facts">
                  {#if target.confidence != null}
                    <dt>уверенность ответа</dt>
                    <dd class="num">{pct(target.confidence)}</dd>
                  {/if}
                  <dt>утверждений в ответе</dt>
                  <dd class="num">{num(target.findings.length)}</dd>
                  {#if target.evaluation}
                    <dt>полнота цитат</dt>
                    <dd class="num">{pct(target.evaluation.metrics.citation_coverage)}</dd>
                    <dt>доля без поддержки</dt>
                    <dd class="num">{pct(target.evaluation.metrics.unsupported_claim_ratio)}</dd>
                    <dt>зачёт прогона</dt>
                    <dd>{target.evaluation.passed ? 'да' : 'нет'}</dd>
                  {/if}
                </dl>
                {#if target.findings.length > 0}
                  <div class="stack target__claims">
                    <p class="micro muted">Утверждения ответа · версии и локаторы</p>
                    {#each target.findings as claim (claim.id)}
                      <div class="claim-row">
                        <span class="micro num">{claim.version}</span>
                        <span class="small grow">{claim.statement}</span>
                        <StatusPill status={claim.status} label={STATUS_SHORT[claim.status]} />
                        <Button variant="ghost" size="sm" icon="clock" onclick={() => void loadHistory(claim.id)}>
                          версии
                        </Button>
                      </div>
                    {/each}
                  </div>
                {/if}
                <div class="row work__actions">
                  <Button variant="quiet" size="sm" onclick={dropTarget}>Снять основание</Button>
                </div>
              </div>
            {:else}
              <div class="freeform">
                <Button variant="quiet" size="sm" onclick={chooseFreeForm}>
                  Комментарий без привязки к ответу
                </Button>
                <p class="micro muted">
                  Так оставляют замечание, которое касается не одного ответа, а корпуса в целом:
                  в журнале предложений такой отзыв читается не привязанным к прогону.
                </p>
              </div>

              {#if canEvaluate}
                {#if runs.length === 0}
                  <div class="work__note">
                    {#if runsFailure}
                      <Notice tone="error" title="Журнал прогонов не прочитан">{runsFailure}</Notice>
                    {:else}
                      <Empty
                        icon="list"
                        title="Журнал прогонов пуст"
                        body="Прогон оценки появляется сразу после вопроса: задайте его выше или в рабочем пространстве."
                      >
                        {#snippet action()}
                          <div class="row">
                            <Button href="/research" variant="quiet" size="sm">К разделу запроса</Button>
                            <Button href="/dashboard" variant="ghost" size="sm">Состояние корпуса</Button>
                          </div>
                        {/snippet}
                      </Empty>
                    {/if}
                  </div>
                {:else}
                  <div class="stack runs">
                    <p class="micro muted">
                      Прогоны оценки · {countOf(runs.length, 'запись', 'записи', 'записей')}
                    </p>
                    {#each runs.slice(0, runsShown) as run (run.id)}
                      <div class="run">
                        <time class="micro muted" datetime={run.created_at}>{ruDate(run.created_at)}</time>
                        <span class="micro">цитаты <span class="num">{pct(run.metrics.citation_coverage)}</span></span>
                        <span class="micro">без поддержки <span class="num">{pct(run.metrics.unsupported_claim_ratio)}</span></span>
                        <span class="micro">итог <span class="num">{num(run.metrics.overall)}</span></span>
                        <StatusPill
                          status={run.passed ? 'consensus' : 'disputed'}
                          label={run.passed ? 'зачтён' : 'не зачтён'}
                        />
                        <Button variant="quiet" size="sm" onclick={() => chooseRun(run)}>Осмотреть</Button>
                      </div>
                    {/each}
                    {#if runs.length > runsShown}
                      <Button variant="quiet" size="sm" onclick={() => (runsShown += RUNS_PAGE_SIZE)}>
                        Показать ещё {countOf(Math.min(RUNS_PAGE_SIZE, runs.length - runsShown), 'прогон', 'прогона', 'прогонов')}
                      </Button>
                    {/if}
                    {#if runsFailure}
                      <p class="micro muted">{runsFailure}</p>
                    {/if}
                  </div>
                {/if}
              {:else}
                <div class="work__note">
                  <Notice tone="info" title="Журнал прогонов скрыт">
                    Метрики качества ответов доступны при расширенном доступе. Разберите ответ
                    полем запроса выше.
                  </Notice>
                </div>
              {/if}
            {/if}
          </div>
        </Panel>

        <Panel tone="default">
          <div class="stack work__region">
            <div class="panel__head">
              <div class="stack">
                <p class="eyebrow"><Icon name="quote" size={16} /> Вердикт и комментарий</p>
                <h2 class="h3">Что не так с ответом</h2>
                <p class="micro muted">
                  Комментарий — основание предложения: без него отзыв не принимается.
                </p>
              </div>
            </div>

            {#if result?.proposal}
              <div class="work__note">
                <Notice tone="ok" title="Отзыв принят · предложение сформулировано">
                  <span class="notice__line">
                    {KIND_RU[result.proposal.kind] ?? 'предложение модели'} · «{result.proposal.title}» —
                    {result.proposal.change}
                  </span>
                  <span class="micro notice__line">
                    Затрагивает: {result.proposal.impact.join(' · ') || '—'} · статус
                    {PROPOSAL_STATUS[result.proposal.status]} · строка журнала ниже
                  </span>
                </Notice>
              </div>
            {:else if result}
              <div class="work__note">
                <Notice tone="warn" title="Отзыв записан, предложения эволюции нет">
                  {#if result.degradation.length > 0}
                    <span class="micro notice__line">
                      Ответ собран не полностью: {result.degradation.join(' · ')}
                    </span>
                  {/if}
                  <span class="micro notice__line">
                    Формулировку предложения делает модель: без её ответа предложения нет. Экспертное
                    решение при этом сохранено.
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
                    {#if topicOf(result.superseded)} · {topicOf(result.superseded)}{/if}
                  </span>
                  {#if lastCorrected}
                    <span class="notice__line">
                      <Button
                        variant="quiet"
                        size="sm"
                        icon="clock"
                        onclick={() => void loadHistory(lastCorrected, true)}>
                        Цепочка версий
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
                </Notice>
              </div>
            {/if}

            <form class="stack verdict-form" onsubmit={(event) => { event.preventDefault(); void submitReview(); }}>
              <!-- Один вердикт из трёх: radiogroup, а не три кнопы с aria-pressed.
                   Стрелки и пробел работают штатно, доступное имя группы — legend. -->
              <fieldset
                class="stack verdict-form__block"
                aria-describedby={verdictMissing ? 'verdict-err' : undefined}
              >
                <legend class="field__label">Вердикт</legend>
                <div class="row">
                  {#each VERDICTS as item (item.key)}
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
                    <Icon name="alert" size={15} /> Выберите вердикт: без него отзыв не отправится.
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
                    <legend class="field__label">
                      Утверждение для замены ·
                      {target?.findings.length ? 'список из ответа' : 'из указателя корпуса'}
                    </legend>
                    <Field
                      label="Поиск утверждения"
                      name="fb-finding-query"
                      type="search"
                      placeholder="по формулировке или субъекту"
                      bind:value={findingQuery}
                    />
                    {#if candidates.length === 0}
                      <p class="micro muted">Совпадений нет — уточните поиск по формулировке.</p>
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
                              title="Цепочка версий этого утверждения"
                              onclick={() => void loadHistory(finding.id)}>
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
                                  доказательств нет: правка висит без локатора
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
                  <Notice tone="warn" title="Экспертная замена утверждения недоступна">
                    {EXPERT_GATE} Выбрать утверждение и ввести текст правки нельзя, поэтому поля нет —
                    отзыв можно отправить комментарием, и он попадёт в журнал.
                  </Notice>
                {/if}
              {/if}

              <div class="row verdict-form__submit">
                <Button type="submit" variant="action" busy={submitting} disabled={!formOpen}>
                  {submitting ? 'Отправка…' : verdict === 'correct' ? 'Отправить правку' : 'Отправить отзыв'}
                </Button>
                {#if !target}
                  <p class="micro muted">Выберите ответ или оставьте комментарий без привязки к прогону.</p>
                {:else if verdict === 'correct' && canRestricted && !correctionReady}
                  <p class="micro muted">Нужны выбранное утверждение и текст правки.</p>
                {/if}
              </div>
            </form>
          </div>
        </Panel>
      {:else}
        <Notice tone="info" title="Отзыв недоступен, разбор предложений открыт">
          {EXPERT_GATE} Форма отзыва скрыта. Ниже — журнал предложений эволюции: читать его можно.
        </Notice>
      {/if}

      <section class="work__ledger">
        <SectionHead
          level="2"
          eyebrow="Разбор предложений эволюции"
          title="Что модель предложила и чем это мерили"
          lead="Каждая строка — предложение, сформулированное моделью по отзыву, и только измеренные результаты A/B-прогона. Решение о применении принимает эксперт: сначала прогон, затем «Принять»."
        >
          <div class="row work__actions">
            <Button variant="quiet" size="sm" icon="refresh" onclick={() => void reloadLedgers()}>
              Обновить лист
            </Button>
          </div>
        </SectionHead>

        {#if !canReview}
          <div class="work__note">
            <Notice tone="warn" title="Решения по предложениям закрыты">
              {EXPERT_GATE} Журнал предложений и результаты прогонов открыты, кнопок «Принять» и
              «Отклонить» нет.
            </Notice>
          </div>
        {/if}

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
            <Notice tone="ok" title="A/B-прогон записан">
              {experimentNotice}
              <span class="micro notice__line">
                Замер относится к текущему рабочему сеансу: позже прогон можно повторить.
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
                «Принять» открывается после A/B-прогона этого предложения.
              </span>
              <div class="row work__actions">
                <Button variant="quiet" size="sm" onclick={() => (reviewFailure = '')}>Понятно</Button>
              </div>
            </Notice>
          </div>
        {/if}
        {#if experimentFailure}
          <div class="work__note">
            <Notice tone="error" title="A/B-прогон не выполнен">
              {experimentFailure}
              <div class="row work__actions">
                <Button variant="quiet" size="sm" onclick={() => (experimentFailure = '')}>Понятно</Button>
              </div>
            </Notice>
          </div>
        {/if}

        {#if proposals.length === 0}
          <Empty
            icon="sparkles"
            title="Предложений нет"
            body="Они рождаются из отзывов на этом листе и в рабочем пространстве запроса. Пока ни одного отзыва нет — нет и предложений."
          >
            {#snippet action()}
              <Button variant="quiet" size="sm" icon="refresh" onclick={() => void reloadLedgers()}>
                Обновить список
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
                      {KIND_RU[proposal.kind] ?? 'предложение модели'}
                    </p>
                    <StatusPill status={PROPOSAL_TONE[proposal.status]} label={PROPOSAL_STATUS[proposal.status]} />
                    <p class="micro muted row-card__src">
                      {#if proposal.created_at}
                        <time datetime={proposal.created_at}>{ruDate(proposal.created_at)}</time> ·
                      {/if}
                      {#if proposal.source_query_id === NIL_QUERY_ID}
                        без привязки к ответу
                      {:else}
                        привязано к ответу на вопрос
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
                      <p class="micro ab__title">
                        A/B-прогон · {countOf(ab.cases, 'кейс', 'кейса', 'кейсов')} · зачёт
                        {#if ab.delta_pass_rate > 0}
                          вырос на <span class="num">{num(ab.delta_pass_rate * 100)}</span> п.п.
                        {:else if ab.delta_pass_rate < 0}
                          упал на <span class="num">{num(Math.abs(ab.delta_pass_rate) * 100)}</span> п.п.
                        {:else}
                          без изменения
                        {/if}
                        · решение
                        <strong>{ab.decision === 'promote' ? 'продвигать' : 'не продвигать'}</strong>
                      </p>
                      <div class="ab__grid">
                        {#each metricRows(ab) as row (row.key)}
                          <div class="ab__metric">
                            <p class="micro ab__label">{row.label} · чем {row.better}, тем лучше</p>
                            <div class="ab__line">
                              <span class="micro ab__who">база</span>
                              <span class="bar ab__track">
                                {#if row.baseWidth !== null}
                                  <span class="bar__fill" style="width: {row.baseWidth}%"></span>
                                {/if}
                              </span>
                              <span class="micro num ab__val">{row.baseText}</span>
                            </div>
                            <div class="ab__line">
                              <span class="micro ab__who">кандидат</span>
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
                      <dl class="kv ab__extra">
                        <dt>задержка, 95-й процентиль</dt>
                        <dd>
                          база <span class="num">{num(ab.baseline.p95_latency_ms)} мс</span> · кандидат
                          <span class="num">{num(ab.candidate.p95_latency_ms)} мс</span>
                        </dd>
                        <dt>повторов модели на кейс</dt>
                        <dd>
                          база <span class="num">{num(ab.baseline.retries_per_case)}</span> · кандидат
                          <span class="num">{num(ab.candidate.retries_per_case)}</span>
                        </dd>
                        <dt>токенов на прогон</dt>
                        <dd>
                          база <span class="num">{num(ab.baseline.total_tokens)}</span> · кандидат
                          <span class="num">{num(ab.candidate.total_tokens)}</span>
                        </dd>
                        <dt>кейсов стало хуже</dt>
                        <dd class="num {ab.regressions.length > 0 ? 'ab__bad' : ''}">
                          {num(ab.regressions.length)}
                        </dd>
                      </dl>
                      {#if ab.regressions.length > 0}
                        <p class="micro ab__regress">
                          Служебные номера кейсов с ухудшением — по ним прогон сверяют с журналом:
                          <code class="ab__codes">{ab.regressions.join(' · ')}</code>
                        </p>
                      {/if}
                    </div>
                  {:else if measurable}
                    <p class="micro ab__none">
                      Прогона нет: без измеренной пары решение не принимается. Сначала A/B-прогон на
                      эталонных кейсах.
                    </p>
                  {:else}
                    <p class="micro ab__none">
                      A/B-прогон поддержан для политики промпта и правила отбора: для
                      «{KIND_RU[proposal.kind] ?? 'этого предложения'}» пары метрик не будет.
                    </p>
                  {/if}

                  <div class="row row-card__foot">
                    {#if canReview}
                      {#if measurable && canEvaluate}
                        <Button
                          variant="quiet"
                          size="sm"
                          icon="gauge"
                          busy={runningId === proposal.id}
                          disabled={busy}
                          onclick={() => void runAbExperiment(proposal)}>
                          {runningId === proposal.id ? 'Прогон…' : 'A/B-прогон'}
                        </Button>
                      {:else if measurable && !canEvaluate}
                        <p class="micro muted">
                          Прогон доступен при расширенном доступе к оценке качества.
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
                            «Принять» появится после A/B-прогона этого предложения.
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
                          Отмена принятия переводит предложение в «отклонено»: статус «ожидает решения»
                          не возвращается.
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
                          Основание — промоут этого A/B-прогона: решение перезаписывается.
                        </p>
                      {:else}
                        <p class="micro muted row-card__gate">
                          Вернуть «отклонено» в «принято» можно только после A/B-прогона: без замера
                          кнопки нет.
                        </p>
                      {/if}
                    {:else}
                      <p class="micro muted row-card__gate">{EXPERT_GATE} Кнопок решения здесь нет.</p>
                    {/if}
                  </div>
                </div>
              </Panel>
            {/each}
          </div>

          <div class="work__pager">
            <p class="micro">
              показано {countOf(shownProposals.length, 'предложение', 'предложения', 'предложений')} из
              <span class="num">{num(proposals.length)}</span> журнала
            </p>
            <div class="row">
              {#if proposals.length > shownProposals.length}
                <Button variant="quiet" size="sm" onclick={() => (proposalLimit += PROPOSAL_PAGE_SIZE)}>
                  Показать ещё {countOf(Math.min(PROPOSAL_PAGE_SIZE, proposals.length - shownProposals.length), 'предложение', 'предложения', 'предложений')}
                </Button>
              {:else if proposalLimit > PROPOSAL_PAGE_SIZE}
                <Button variant="ghost" size="sm" onclick={() => (proposalLimit = PROPOSAL_PAGE_SIZE)}>
                  Только первые <span class="num">{PROPOSAL_PAGE_SIZE}</span>
                </Button>
              {/if}
            </div>
          </div>

          <div class="row work__actions">
            <Button href="/findings" variant="ghost" size="sm">Указатель находок</Button>
            <Button href="/dashboard" variant="ghost" size="sm">Состояние корпуса</Button>
          </div>
        {/if}
      </section>
    {/if}
  </div>
</div>

{#if historyOpen}
  <Sheet
    title="Цепочка версий утверждения"
    description="Все версии утверждения: чем заменены и кто проверял."
    onclose={closeHistory}>
    <!-- Предмет шторки виден до ответа сервера: по id находки цепочку сверяют
         с «Находками», и при отказе истории называть её нечем. -->
    <p class="micro muted">Утверждение <span class="hist__id">{historyClaim}</span></p>
    {#if historyBusy}
      <div class="row hist__busy"><span class="spinner spinner--quiet"></span> читаем версии…</div>
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
            <p class="micro muted hist__meta">
              {#if version.reviewer_id}{reviewerOf(version.reviewer_id)}{/if}
              {#if version.review_date}
                · <time datetime={version.review_date}>{ruDate(version.review_date)}</time>
              {/if}
              {#if version.review_reason}· {version.review_reason}{/if}
              {#if version.superseded_by}
                · {#if replacement}
                  заменена версией <span class="num">{replacement}</span>
                {:else}
                  заменена — новой версии в этой цепочке нет
                {/if}
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

  .work__counts {
    text-align: right;
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

  /* ── Основание отзыва ────────────────────────────────────────────────── */
  .ask {
    display: grid;
    gap: var(--s4);
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: end;
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
    font-family: var(--font-data);
    color: var(--ink-3);
  }

  .target__provenance {
    text-wrap: pretty;
  }

  .target__summary {
    text-wrap: pretty;
  }

  .target__facts {
    gap: var(--s1) var(--s5);
    margin-top: var(--s2);
  }

  .target__claims {
    --gap: var(--s2);
    margin-top: var(--s4);
  }

  .claim-row {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
    padding: var(--s3) var(--s4);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-md);
    background: var(--surface-raised);
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

  /* Радио остаётся доступным: у поля есть фокус, а подпись рисуетpill-состояние. */
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
    font-family: var(--font-data);
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
    gap: var(--s2);
    flex-wrap: wrap;
    font-family: var(--font-data);
    color: var(--ink-2);
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
    font-family: var(--font-data);
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
    font-family: var(--font-data);
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

  .ab__regress {
    color: var(--ink-3);
  }

  /* Служебные номера кейсов — подчинённая строка, а не заголовок метрики. */
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
    gap: var(--s2);
    flex-wrap: wrap;
  }

  @media (max-width: 820px) {
    .ask {
      grid-template-columns: minmax(0, 1fr);
    }

    .row-card__src {
      margin-left: 0;
    }

    .ab__line {
      grid-template-columns: 58px minmax(0, 1fr) 60px;
    }
  }
</style>
