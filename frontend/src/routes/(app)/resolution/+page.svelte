<script lang="ts">
  // Очередь склеек сущностей: модель видит в двух именах одну технологию и
  // предлагает склеить их. Сервер пишет эти предложения с каждого импорта, но
  // экрана у очереди не было, поэтому решение эксперта существовало только в
  // тесте. Направление пары значимо: слева имя из документа, справа принятое
  // название, и принятое склеивает узлы графа ребром алиаса.
  // Маршрут закрыт экспертным правом: без него список не запрашивается вовсе, а
  // человек получает объяснение, кто выдаёт право и где посмотреть свои права.
  // Состояния чтения разведены словами: загружается, хранилище не ответило,
  // очередь пуста подтверждённому нулю, окно прочитано не полностью.
  // Шапку, skip-link и <main id="main"> рендерит +layout.svelte.
  import { api, ApiError } from '$lib/api';
  import { dateTime, pct } from '$lib/format';
  import { navLabel } from '$lib/nav';
  import { session } from '$lib/sessionStore.svelte';
  import {
    journalIncomplete,
    journalLoadMoreOf,
    mergeNotLoadedBody,
    mergeOutcome,
    mergeWaiting,
    MERGE_PAIR_NOUNS,
    MERGE_STATUS_LABELS,
    MERGE_STATUS_TONE,
    RESOLUTION_ACTION,
    RESOLUTION_FAILURE,
    RESOLUTION_GATE,
    RESOLUTION_HINTS,
    RESOLUTION_PAGE,
    RESOLUTION_WORDS,
    shownSentenceOf,
  } from '$lib/terms';
  import type { EntityMergeProposal, MergeReviewAction } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';
  import type { IconName } from '$lib/ui/icons';

  // Очередь растёт на каждом импорте, поэтому читается серверным окном: полное
  // число пар несёт заголовок ответа, а не длина пришедшего массива.
  const PAGE_SIZE = 20;
  // Сколько строк раскрыто по умолчанию: решение принимают по одной паре, а не
  // пролистывая весь список.
  const PREVIEW = 5;

  const canReview = $derived(session.can('proposal:review'));
  const anonymous = $derived(session.state === 'anonymous');

  let items = $state<EntityMergeProposal[]>([]);
  let status = $state<'loading' | 'ready' | 'error'>('loading');
  let failure = $state<{ title: string; body: string; denied: boolean } | null>(null);
  // null означает «показаний нет», а не «пар ноль»: экран обязан различать эти
  // два случая, иначе неполное чтение выглядит как пустая очередь.
  let total = $state<number | null>(null);
  let windowNote = $state<string | null>(null);
  let loadedAt = $state<Date | null>(null);
  let busy = $state(false);
  let shown = $state(PREVIEW);
  let listMessage = $state('');

  // Запись решения идёт по одной строке: блокируются кнопки только той пары, по
  // которой уже ушёл запрос. Остальные строки остаются рабочими, а список не
  // ждёт глобальной разблокировки.
  let pending = $state<Record<string, MergeReviewAction>>({});
  let rowMessage = $state<{
    id: string;
    tone: 'ok' | 'warn' | 'error' | 'info';
    text: string;
  } | null>(null);

  const ACTION_META: Record<
    MergeReviewAction,
    { label: string; icon: IconName; variant: 'action' | 'quiet' | 'ink'; hint: string }
  > = {
    accept: {
      label: RESOLUTION_ACTION.accept,
      icon: 'shield',
      variant: 'action',
      hint: RESOLUTION_HINTS.accept,
    },
    reject: {
      label: RESOLUTION_ACTION.reject,
      icon: 'close',
      variant: 'quiet',
      hint: RESOLUTION_HINTS.reject,
    },
    revert: {
      label: RESOLUTION_ACTION.revert,
      icon: 'refresh',
      variant: 'ink',
      hint: RESOLUTION_HINTS.revert,
    },
  };

  const shownRows = $derived(items.slice(0, shown));
  // Откат — это снятое решение: ребра алиаса в графе нет, строка снова зовёт
  // «Принять склейку». Считать её рассмотренной значит показывать «нерассмотренных
  // нет» там, где эксперту прямо сейчас нужно действие.
  const waitingCount = $derived(
    items.filter(
      (proposal) => proposal.status === 'proposed' || proposal.status === 'reverted',
    ).length,
  );
  const left = $derived(total === null ? 0 : Math.max(total - items.length, 0));
  const hasMore = $derived(items.length > 0 && left > 0);
  // Неполнота называется всегда, когда она есть: не дочитанное окно и нераскрытые
  // строки того же списка читатель должен видеть, а не догадываться по длине.
  const notAll = $derived(left > 0 || shownRows.length < items.length);
  const loadedAtText = $derived(loadedAt ? dateTime(loadedAt, true) : '');
  const shownText = $derived(shownSentenceOf(shownRows.length, total, MERGE_PAIR_NOUNS));
  const anyPending = $derived(Object.keys(pending).length > 0);

  /** Имя пары: пустая строка в данных не превращается в пустое место в заголовке. */
  function nameOr(value: string): string {
    return value.trim().length > 0 ? value.trim() : RESOLUTION_WORDS.nameAbsent;
  }

  function rationaleOr(proposal: EntityMergeProposal): string {
    return proposal.rationale.trim().length > 0
      ? proposal.rationale.trim()
      : RESOLUTION_WORDS.noRationale;
  }

  /** Полоса уверенности рисуется только по названному числу: отсутствие значения
   *  это не ноль процентов. */
  function confStyle(value: number): string | null {
    if (!Number.isFinite(value)) return null;
    return `width: ${Math.min(Math.max(value, 0), 1) * 100}%`;
  }

  /**
   * Набор действий строки. Принятую пару можно только откатить: «отклонить»
   * перевело бы статус, а ребро алиаса осталось бы в графе, и экран показал бы
   * несвязанную пару там, где связь есть. Отклонённую или откатанную пару можно
   * принять снова; повтор уже записанного решения не предлагается.
   */
  function actionsFor(proposal: EntityMergeProposal): MergeReviewAction[] {
    if (proposal.status === 'accepted') return ['revert'];
    if (proposal.status === 'proposed') return ['accept', 'reject'];
    return ['accept'];
  }

  function rowHint(proposal: EntityMergeProposal): string {
    return proposal.status === 'accepted' ? RESOLUTION_HINTS.revert : RESOLUTION_HINTS.accept;
  }

  /** Три разных факта, а не один «сбой»: доступ закрыт, хранилище не ответило,
   *  запрос не прошёл. Коды ответов экран не показывает. */
  function describe(reason: unknown): { title: string; body: string; denied: boolean } {
    if (reason instanceof ApiError && reason.status === 403) {
      return {
        title: RESOLUTION_FAILURE.deniedTitle,
        body: RESOLUTION_FAILURE.deniedBody,
        denied: true,
      };
    }
    if (reason instanceof ApiError && (reason.status === 502 || reason.status === 503)) {
      return {
        title: RESOLUTION_FAILURE.storageTitle,
        body: RESOLUTION_FAILURE.storageBody,
        denied: false,
      };
    }
    return {
      title: RESOLUTION_FAILURE.transportTitle,
      body: RESOLUTION_FAILURE.transportBody,
      denied: false,
    };
  }

  // Тот же 409 сервис отдаёт по двум разным причинам: решение уже записано либо
  // склеиваемых узлов в графе нет. Сводить это к одной фразе значит соврать о
  // состоянии графа, поэтому причина читается из текста сервиса.
  const GRAPH_MISS = /не найдены|нет соединения|граф/i;

  function reviewFailure(
    reason: unknown,
  ): { tone: 'warn' | 'error' | 'info'; text: string; refresh: boolean } {
    if (reason instanceof ApiError) {
      if (reason.status === 403) {
        return { tone: 'warn', text: RESOLUTION_FAILURE.reviewDenied, refresh: false };
      }
      if (reason.status === 404) {
        return { tone: 'warn', text: RESOLUTION_FAILURE.reviewGone, refresh: true };
      }
      if (reason.status === 409) {
        return GRAPH_MISS.test(reason.message)
          ? { tone: 'warn', text: RESOLUTION_FAILURE.reviewNoNodes, refresh: true }
          : { tone: 'info', text: RESOLUTION_FAILURE.reviewDuplicate, refresh: true };
      }
      if (reason.status === 502 || reason.status === 503) {
        return { tone: 'error', text: RESOLUTION_FAILURE.reviewStorage, refresh: false };
      }
    }
    return { tone: 'error', text: RESOLUTION_FAILURE.reviewTransport, refresh: false };
  }

  let seq = 0;
  /** `append` дозагружает следующую часть окна, не стирая показанное при отказе. */
  async function load(append = false): Promise<void> {
    const call = ++seq;
    listMessage = '';
    if (append) {
      busy = true;
    } else {
      status = 'loading';
      failure = null;
      // Показания окна (`total`, замечание) относятся к списку, который на экране:
      // при перечитывании они остаются при старом снимке и заменяются ответом,
      // иначе счётчик на минуту стал бы врать «полное число неизвестно».
    }
    try {
      const page = await api.mergeProposals(PAGE_SIZE, append ? items.length : 0);
      if (call !== seq) return;
      // Окно обязано склеиваться по id пары: очередь пересобирается на каждом
      // импорте, и перекрывшаяся порция удвоила бы строку с теми же кнопками.
      const seen = new Set(items.map((row) => row.id));
      items = append
        ? [...items, ...page.items.filter((row) => !seen.has(row.id))]
        : page.items;
      total = page.total;
      // Пустая строка замечания значит, что его не было: состояние остаётся null,
      // чтобы экран не выводил пустую полосу вместо текста сервиса.
      windowNote = page.windowNote === '' ? null : page.windowNote;
      loadedAt = new Date();
      status = 'ready';
      // Обновление не сворачивает список обратно к первой пятёрке: раскрытое
      // человек уже видел.
      if (!append) shown = Math.max(PREVIEW, shown);
    } catch (reason) {
      if (call !== seq) return;
      if (append) {
        // Показанное осталось: сбой дозагрузки не имеет права превращать
        // прочитанное окно в пустой список.
        listMessage = RESOLUTION_FAILURE.more;
      } else {
        failure = describe(reason);
        status = 'error';
        if (items.length > 0) {
          // Повторного чтения без показаний нет: строки остаются снимком до сбоя,
          // и экран говорит об этом, а не выдаёт их за свежие.
          listMessage = `${failure.body} ${RESOLUTION_FAILURE.stale}`;
        }
      }
    } finally {
      if (call === seq) busy = false;
    }
  }

  /**
   * Решение по паре: строка заменяется тем, что вернул сервис, а не догадкой
   * экрана. Отказ оставляет строку на месте с объяснением в подвале той же
   * карточки: сообщение над списком при длинной очереди терялось бы.
   */
  async function decide(proposal: EntityMergeProposal, action: MergeReviewAction): Promise<void> {
    if (pending[proposal.id] !== undefined) return;
    if (!canReview) {
      rowMessage = { id: proposal.id, tone: 'warn', text: RESOLUTION_FAILURE.reviewDenied };
      return;
    }
    pending = { ...pending, [proposal.id]: action };
    rowMessage = null;
    try {
      const updated = await api.mergeReview(proposal.id, action);
      items = items.map((row) => (row.id === updated.id ? updated : row));
      rowMessage = { id: updated.id, tone: 'ok', text: mergeOutcome(updated.status) };
    } catch (reason) {
      const refused = reviewFailure(reason);
      rowMessage = { id: proposal.id, tone: refused.tone, text: refused.text };
      if (refused.refresh) void load();
    } finally {
      const next = { ...pending };
      delete next[proposal.id];
      pending = next;
    }
  }

  // Очередь читается по подтверждённому входу и только у аккаунта с правом:
  // запрос без права дал бы отказ там, где экран обязан объяснить доступ.
  let started = false;
  $effect(() => {
    if (session.state === 'unknown' || !canReview) return;
    if (!started) {
      started = true;
      void load();
    }
  });
</script>

<svelte:head>
  <title>Сущности: Научный Клубок</title>
  <meta
    name="description"
    content="Очередь предложений склейки сущностей: два имени одной технологии, уверенность модели и пояснение. Принять, отклонить или откатить склейку может эксперт."
  />
</svelte:head>

<div class="page res">
  <div class="wrap">
    <SectionHead
      level="1"
      eyebrow={RESOLUTION_PAGE.eyebrow}
      title={RESOLUTION_PAGE.title}
      lead={RESOLUTION_PAGE.lead}>
      <div class="row res__aside">
        {#if canReview && loadedAt}
          <!-- Время относится к последнему удачному чтению: оно остаётся на месте
               и при отказе перечитать, показывая, каким снимкам висит список. -->
          <p class="micro muted">
            {RESOLUTION_PAGE.loadedAt}
            <time datetime={loadedAt.toISOString()}>{loadedAtText}</time>
          </p>
        {/if}
        {#if canReview && windowNote}
          <!-- Замечание сервиса о неполном чтении повторяется отдельной строкой как
               есть: экран не переписывает чужие показания своими словами. -->
          <p class="micro muted res__window-note">{windowNote}</p>
        {/if}
        {#if canReview}
          <Button
            variant="quiet"
            size="sm"
            icon="refresh"
            busy={status === 'loading' && items.length === 0}
            disabled={busy || anyPending}
            onclick={() => void load()}>
            {RESOLUTION_ACTION.readAgain}
          </Button>
        {/if}
      </div>
    </SectionHead>

    {#if session.state === 'unknown' && items.length === 0}
      <div class="res__skeleton" role="status" aria-label={RESOLUTION_PAGE.loadingAria}>
        <span class="skeleton res__sk-title"></span>
        <p class="micro muted">{RESOLUTION_PAGE.loading}</p>
      </div>
    {:else if !canReview}
      <!-- Ни одной пары не запрошено: вместо пустого списка и вместо сырого отказа
           объяснение, кто выдаёт право и где посмотреть свои права. -->
      <Notice
        tone="warn"
        title={anonymous ? RESOLUTION_GATE.anonymousTitle : RESOLUTION_GATE.title}>
        {anonymous ? RESOLUTION_GATE.anonymousBody : RESOLUTION_GATE.body}
        <div class="row res__aside">
          {#if anonymous}
            <Button href={`/login?next=${encodeURIComponent('/resolution')}`} variant="action" size="sm">
              {RESOLUTION_GATE.anonymousAction}
            </Button>
          {:else}
            <Button href="/account" variant="quiet" size="sm">{RESOLUTION_GATE.checkAction}</Button>
            <Button href="/feedback" variant="ghost" size="sm">
              Раздел «{navLabel('/feedback')}»
            </Button>
          {/if}
        </div>
      </Notice>
    {:else if status === 'loading' && items.length === 0}
      <div class="res__skeleton" role="status" aria-label={RESOLUTION_PAGE.loadingAria}>
        <span class="skeleton res__sk-title"></span>
        <span class="skeleton res__sk-panel"></span>
        <span class="skeleton res__sk-panel"></span>
        <p class="micro muted">{RESOLUTION_PAGE.loading}</p>
      </div>
    {:else if status === 'error' && items.length === 0}
      <Notice
        tone={failure?.denied ? 'warn' : 'error'}
        title={failure?.title ?? RESOLUTION_FAILURE.transportTitle}>
        {failure?.body ?? RESOLUTION_FAILURE.transportBody}
        <div class="row res__aside">
          {#if failure?.denied}
            <!-- Повтор отказа по праву даёт тот же отказ: здесь только то, что
                 действительно двигает дело. -->
            <Button href="/account" variant="quiet" size="sm">{RESOLUTION_GATE.checkAction}</Button>
          {:else}
            <Button variant="quiet" size="sm" icon="refresh" onclick={() => void load()}>
              {RESOLUTION_ACTION.readAgain}
            </Button>
          {/if}
          <Button href="/graph" variant="ghost" size="sm">Раздел «{navLabel('/graph')}»</Button>
        </div>
      </Notice>
    {:else if items.length === 0}
      {#if total !== null && total > 0}
        <!-- Сервис называет число больше нуля, а страница пустая: это сбой чтения
             окна, а не «пар на склейку нет». -->
        <Notice tone="error" title={RESOLUTION_PAGE.notLoadedTitle}>
          {mergeNotLoadedBody(total)}
          <div class="row res__aside">
            <Button variant="quiet" size="sm" icon="refresh" onclick={() => void load()}>
              {RESOLUTION_ACTION.readAgain}
            </Button>
          </div>
        </Notice>
      {:else if total === null}
        <!-- Подтверждённого нуля нет: экран не пишет ни «очередь пуста», ни
             «пары есть», потому что полного числа он не знает. -->
        <Notice tone="warn" title={RESOLUTION_PAGE.notLoadedUnknownTitle}>
          {RESOLUTION_PAGE.notLoadedUnknownBody}
          <div class="row res__aside">
            <Button variant="quiet" size="sm" icon="refresh" onclick={() => void load()}>
              {RESOLUTION_ACTION.readAgain}
            </Button>
          </div>
        </Notice>
      {:else}
        <Empty
          icon="checkCircle"
          title={RESOLUTION_PAGE.emptyTitle}
          body={RESOLUTION_PAGE.emptyBody}>
          {#snippet action()}
            <div class="row">
              <Button href="/graph" variant="quiet" size="sm">Открыть «{navLabel('/graph')}»</Button>
              <Button href="/findings" variant="ghost" size="sm">
                Раздел «{navLabel('/findings')}»
              </Button>
            </div>
          {/snippet}
        </Empty>
      {/if}
    {:else}
      <!-- Каждый счётчик подписан своим элементом: сколько пар на экране из
           полного числа и сколько из них ещё не решены. -->
      <div class="row res__counter">
        <span class="micro">{shownText}</span>
        <span class="micro">{mergeWaiting(waitingCount)}</span>
        {#if total === null}
          <span class="micro">{RESOLUTION_PAGE.unknownTotal}</span>
        {:else if notAll}
          <span class="micro">{journalIncomplete(MERGE_PAIR_NOUNS)}</span>
        {/if}
      </div>

      {#if listMessage}
        <Notice tone="error">{listMessage}</Notice>
      {/if}

      <div class="res__list">
        {#each shownRows as proposal (proposal.id)}
          {@const rowAction = pending[proposal.id]}
          {@const rowBusy = rowAction !== undefined}
          {@const decided = proposal.status !== 'proposed'}
          {@const conf = confStyle(proposal.confidence)}
          {@const mine = proposal.reviewer_id !== null && proposal.reviewer_id === session.account?.id}
          <Panel tag="article" tone="default" flush={true}>
            <div class="res-pair">
              <div class="res-pair__head">
                <h3 class="h4 res-pair__title">
                  <span class="res-pair__name">{nameOr(proposal.source)}</span>
                  <span class="res-pair__arrow">
                    <Icon name="arrowRight" size={18} />
                  </span>
                  <span class="res-pair__name">{nameOr(proposal.target)}</span>
                </h3>
                <span class="row res-pair__marks">
                  <StatusPill
                    status={MERGE_STATUS_TONE[proposal.status]}
                    label={MERGE_STATUS_LABELS[proposal.status]}
                  />
                </span>
              </div>

              <!-- Направление склейки читается словами, а не только стрелкой:
                   на узком экране стрелка без подписи двусмысленна. -->
              <p class="micro res-pair__sides">
                <span>{RESOLUTION_WORDS.aliasMark}</span>
                <span aria-hidden="true">·</span>
                <span>{RESOLUTION_WORDS.canonicalMark}</span>
                <span class="res-pair__effect">{RESOLUTION_WORDS.directionNote}</span>
              </p>

              <div class="res-pair__conf">
                <p class="micro res-pair__conf-title">
                  {RESOLUTION_WORDS.confidenceTitle}
                  <span class="num">{pct(proposal.confidence)}</span>
                </p>
                {#if conf}
                  <span class="bar res-pair__bar"><span class="bar__fill" style={conf}></span></span>
                {:else}
                  <p class="micro muted">{RESOLUTION_WORDS.confidenceAbsent}</p>
                {/if}
                <p class="micro muted">{RESOLUTION_WORDS.confidenceHint}</p>
              </div>

              <p class="micro res-pair__why">{RESOLUTION_WORDS.rationaleTitle}</p>
              <p class="small res-pair__reason">{rationaleOr(proposal)}</p>

              <p class="micro res-pair__decided">
                {#if decided}
                  {#if mine}
                    <span>{RESOLUTION_WORDS.decidedSelf}</span>
                  {:else if proposal.reviewer_id}
                    <span>{RESOLUTION_WORDS.decidedBy}</span>
                  {:else}
                    <span>{RESOLUTION_WORDS.decidedUnknown}</span>
                  {/if}
                  {#if proposal.reviewed_at}
                    <span>
                      {RESOLUTION_WORDS.decided}
                      <time datetime={proposal.reviewed_at}>{dateTime(proposal.reviewed_at)}</time>
                    </span>
                  {:else}
                    <!-- Статус в записи есть, момента решения в ней нет: экран
                         говорит об этом прямо вместо пустой даты. -->
                    <span>{RESOLUTION_WORDS.noDecidedAt}</span>
                  {/if}
                {:else}
                  <span>{RESOLUTION_WORDS.waiting}</span>
                {/if}
                <span>
                  {RESOLUTION_WORDS.registered}
                  <time datetime={proposal.created_at}>{dateTime(proposal.created_at, true)}</time>
                </span>
              </p>

              <details class="res-svc">
                <summary class="micro">{RESOLUTION_WORDS.svcHead}</summary>
                <p class="micro res-svc__row">
                  {RESOLUTION_WORDS.svcPair} <code class="code">{proposal.id}</code>
                </p>
                {#if proposal.source_id || proposal.target_id}
                  {#if proposal.source_id}
                    <p class="micro res-svc__row">
                      {RESOLUTION_WORDS.svcSourceId}
                      <code class="code">{proposal.source_id}</code>
                    </p>
                  {/if}
                  {#if proposal.target_id}
                    <p class="micro res-svc__row">
                      {RESOLUTION_WORDS.svcTargetId}
                      <code class="code">{proposal.target_id}</code>
                    </p>
                  {/if}
                {:else}
                  <!-- Пустые id это не прочерк и не ноль: узлы пары ещё не найдены
                       в графе, склейка не выполнена. -->
                  <p class="micro res-svc__row">{RESOLUTION_WORDS.idsAbsent}</p>
                {/if}
                {#if proposal.reviewer_id}
                  <p class="micro res-svc__row">
                    {RESOLUTION_WORDS.svcReviewer}
                    <code class="code">{proposal.reviewer_id}</code>
                  </p>
                {/if}
              </details>

              {#if rowMessage && rowMessage.id === proposal.id}
                <Notice tone={rowMessage.tone}>{rowMessage.text}</Notice>
              {/if}

              <div class="row res-pair__actions">
                {#each actionsFor(proposal) as action (action)}
                  <Button
                    variant={ACTION_META[action].variant}
                    size="sm"
                    icon={ACTION_META[action].icon}
                    title={ACTION_META[action].hint}
                    busy={rowBusy && rowAction === action}
                    disabled={rowBusy && rowAction !== action}
                    onclick={() => void decide(proposal, action)}>
                    {rowBusy && rowAction === action
                      ? RESOLUTION_ACTION.pending
                      : ACTION_META[action].label}
                  </Button>
                {/each}
                <!-- Последствие того действия, которое строке действительно
                     предложено, видно текстом, а не только подсказкой на наведении. -->
                <p class="micro muted res-pair__hint">{rowHint(proposal)}</p>
              </div>
            </div>
          </Panel>
        {/each}
      </div>

      <div class="res__pager">
        <p class="micro">{shownText}</p>
        {#if shownRows.length < items.length}
          <Button variant="quiet" size="sm" onclick={() => (shown += PREVIEW)}>
            {RESOLUTION_ACTION.showMore}
          </Button>
        {:else if hasMore}
          <!-- Одна кнопка «Показать ещё»: сначала раскрывает прочитанное окно,
               потом дозагружает следующее по серверному смещению. -->
          <Button
            variant="quiet"
            size="sm"
            busy={busy}
            disabled={busy}
            onclick={() => void load(true)}>
            {busy ? RESOLUTION_PAGE.loadingMore : journalLoadMoreOf(left, PAGE_SIZE, MERGE_PAIR_NOUNS)}
          </Button>
        {/if}
      </div>
    {/if}
  </div>
</div>

<style>
  .res .wrap {
    display: flex;
    flex-direction: column;
    gap: var(--s5);
  }

  .res__aside {
    justify-content: flex-end;
  }

  /* Замечание сервиса о неполном чтении занимает в шапке свою строку: предложение
     длиннее кнопок и не имеет права их сжимать. */
  .res__window-note {
    flex: 1 0 100%;
    text-align: right;
  }

  .res__skeleton {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    padding: var(--s6);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-xl);
    background: var(--surface-raised);
  }

  /* Геометрия скелетона в классе, а не в inline-стилях разметки. */
  .res__sk-title {
    display: block;
    height: var(--s4);
    width: 38%;
  }

  .res__sk-panel {
    display: block;
    height: var(--s9);
    border-radius: var(--r-lg);
  }

  .res__counter {
    gap: var(--s4);
    color: var(--ink-3);
  }

  .res__list {
    display: flex;
    flex-direction: column;
    gap: var(--s5);
  }

  .res__pager {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--s4);
    flex-wrap: wrap;
    color: var(--ink-3);
  }

  .res-pair {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    padding: clamp(var(--s4), 2.4vw, var(--s6));
  }

  .res-pair__head {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: start;
    gap: var(--s3) var(--s4);
    padding-bottom: var(--s3);
    border-bottom: 1px solid var(--line-soft);
  }

  .res-pair__title {
    display: flex;
    align-items: center;
    gap: var(--s3);
    flex-wrap: wrap;
    min-width: 0;
    text-wrap: pretty;
  }

  .res-pair__name {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  /* Стрелка направления: служебный знак, поэтому она тише имён пары. */
  .res-pair__arrow {
    display: flex;
    color: var(--ink-4);
  }

  .res-pair__marks {
    gap: var(--s3);
    justify-content: flex-end;
  }

  .res-pair__sides {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
    color: var(--ink-3);
  }

  .res-pair__effect {
    color: var(--ink-4);
  }

  .res-pair__conf {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    max-width: var(--maxw-measure);
  }

  .res-pair__conf-title {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    color: var(--ink-3);
  }

  /* Полоса уверенности устойчивой ширины: две величины рядом не пляшут от длины
     подписи. */
  .res-pair__bar {
    width: min(100%, 320px);
  }

  .res-pair__why {
    color: var(--ink-3);
  }

  .res-pair__reason {
    max-width: var(--maxw-measure);
    color: var(--ink-2);
    text-wrap: pretty;
  }

  .res-pair__decided {
    display: flex;
    align-items: baseline;
    gap: var(--s2);
    flex-wrap: wrap;
    color: var(--ink-3);
  }

  .res-pair__actions {
    gap: var(--s3);
    align-items: flex-start;
  }

  .res-pair__hint {
    flex: 1 1 220px;
    min-width: 0;
    color: var(--ink-4);
    text-wrap: pretty;
  }

  /* Служебные коды пары под раскрытием: на виду они читателю не помогают. */
  .res-svc {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
    padding: var(--s3) var(--s4);
    border: 1px dashed var(--line);
    border-radius: var(--r-md);
    background: var(--surface-sunk);
  }

  .res-svc summary {
    color: var(--ink-3);
    font-weight: 500;
    cursor: pointer;
  }

  .res-svc .code {
    color: var(--ink-4);
    overflow-wrap: anywhere;
  }

  .res-svc__row {
    color: var(--ink-3);
  }

  @media (max-width: 900px) {
    .res-pair__head {
      grid-template-columns: minmax(0, 1fr);
    }

    .res-pair__marks {
      justify-content: flex-start;
    }
  }

  @media (max-width: 640px) {
    /* Действия и пояснение встают друг под друга: имена пары и полоса
       уверенности остаются читаемыми на узком экране. */
    .res-pair__actions {
      flex-direction: column;
      align-items: flex-start;
    }

    .res__pager {
      flex-direction: column;
      align-items: flex-start;
    }

    .res-pair__bar {
      width: 100%;
    }
  }
</style>
