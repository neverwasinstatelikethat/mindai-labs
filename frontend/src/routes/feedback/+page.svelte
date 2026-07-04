<script lang="ts">
  import { onMount } from 'svelte';
  import {
    AlertTriangle, CheckCircle2, LoaderCircle, Lock, MessageSquare,
    ThumbsUp, ThumbsDown, Send, X,
  } from '@lucide/svelte';
  import AppHeader from '$lib/AppHeader.svelte';
  import { api } from '$lib/api';
  import { getRole } from '$lib/roleStore.svelte';
  import { hasPermission, type EvolutionProposal, type Notification } from '$lib/types';

  const currentRole = $derived(getRole());
  const canAccess = $derived(hasPermission(currentRole, 'feedback:give'));
  const canReview = $derived(hasPermission(currentRole, 'proposal:review'));

  let proposals = $state<EvolutionProposal[]>([]);
  let notifications = $state<Notification[]>([]);
  let loading = $state(true);
  let error = $state('');

  // Форма обратной связи
  let feedbackVerdict = $state<'accept' | 'reject' | null>(null);
  let feedbackComment = $state('');
  let feedbackQueryId = $state('');
  let submitting = $state(false);
  let submitMsg = $state('');
  let submitError = $state('');

  async function submitFeedback() {
    if (feedbackVerdict === null) return;
    submitting = true;
    submitMsg = '';
    submitError = '';
    try {
      const comment = feedbackComment.trim().length >= 3
        ? feedbackComment.trim()
        : (feedbackVerdict === 'accept' ? 'Ответ был полезен' : 'Ответ не был полезен');
      await api.feedback(feedbackQueryId || 'general', null, feedbackVerdict, comment);
      submitMsg = 'Спасибо! Ваш отзыв отправлен.';
      feedbackVerdict = null;
      feedbackComment = '';
      feedbackQueryId = '';
      // Перезагружаем предложения
      proposals = await api.proposals();
    } catch (reason) {
      submitError = reason instanceof Error ? reason.message : 'Не удалось отправить отзыв';
    } finally {
      submitting = false;
    }
  }

  async function reviewProposal(proposal: EvolutionProposal, accepted: boolean) {
    try {
      const updated = await api.review(proposal.id, accepted);
      proposals = proposals.map((item) => item.id === updated.id ? updated : item);
    } catch (reason) {
      error = reason instanceof Error ? reason.message : 'Не удалось проверить предложение';
    }
  }

  function formatTime(ts: string): string {
    try {
      return new Date(ts).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
    } catch {
      return ts;
    }
  }

  onMount(async () => {
    try {
      const [propResult, notifResult] = await Promise.allSettled([
        api.proposals(),
        api.notifications(),
      ]);
      if (propResult.status === 'fulfilled') proposals = propResult.value;
      if (notifResult.status === 'fulfilled') notifications = notifResult.value;
    } catch (reason) {
      error = reason instanceof Error ? reason.message : 'Ошибка загрузки данных';
    } finally {
      loading = false;
    }
  });
</script>

<svelte:head><title>Обратная связь — Научный Клубок</title></svelte:head>

<AppHeader compact />
<main class="workspace" id="main">
  <section class="page-heading">
    <div>
      <p class="section-label">Взаимодействие</p>
      <h1>Обратная связь</h1>
    </div>
  </section>

  {#if !canAccess}
    <div class="permission-denied workspace-panel">
      <Lock size={36} />
      <h2>Доступ запрещён</h2>
      <p>Для отправки обратной связи требуется роль «Исследователь» или выше.</p>
    </div>
  {:else if loading}
    <div class="workspace-panel loading-state">
      <LoaderCircle class="spin" size={28} />
      <p>Загрузка…</p>
    </div>
  {:else}
    {#if error}
      <div class="notice notice--error">
        <AlertTriangle size={19} />
        <div><strong>Ошибка</strong><p>{error}</p></div>
      </div>
    {/if}

    <!-- Форма обратной связи -->
    <section class="workspace-panel feedback-form-page">
      <div class="panel-intro">
        <div>
          <p class="section-label">Новый отзыв</p>
          <h2>Поделитесь мнением о качестве ответов</h2>
        </div>
        <p>Ваш отзыв помогает системе улучшаться. Укажите, был ли ответ полезен, и при желании добавьте комментарий.</p>
      </div>

      {#if submitMsg}
        <div class="notice notice--success">
          <CheckCircle2 size={19} />
          <div><strong>Отправлено</strong><p>{submitMsg}</p></div>
        </div>
      {/if}
      {#if submitError}
        <div class="notice notice--error">
          <AlertTriangle size={19} />
          <div><strong>Ошибка</strong><p>{submitError}</p></div>
        </div>
      {/if}

      <div class="feedback-form-fields">
        <div class="feedback-form-field">
          <label>ID запроса (необязательно)</label>
          <input type="text" bind:value={feedbackQueryId} placeholder="Например: query-abc123" />
        </div>

        <div class="feedback-form-field">
          <label>Оценка</label>
          <div class="feedback-verdict-buttons">
            <button
              class:feedback-verdict--active={feedbackVerdict === 'accept'}
              onclick={() => feedbackVerdict = 'accept'}
            >
              <ThumbsUp size={18} /> Полезно
            </button>
            <button
              class:feedback-verdict--active={feedbackVerdict === 'reject'}
              onclick={() => feedbackVerdict = 'reject'}
            >
              <ThumbsDown size={18} /> Не полезно
            </button>
          </div>
        </div>

        <div class="feedback-form-field">
          <label>Комментарий</label>
          <textarea
            bind:value={feedbackComment}
            placeholder="Что можно улучшить? Какие данные отсутствуют?"
            rows="3"
          ></textarea>
        </div>

        <div class="feedback-form-actions">
          <button
            class="button button--primary button--small"
            onclick={submitFeedback}
            disabled={feedbackVerdict === null || submitting}
          >
            {#if submitting}<LoaderCircle class="spin" size={16} /> Отправка…{:else}<Send size={16} /> Отправить отзыв{/if}
          </button>
        </div>
      </div>
    </section>

    <!-- Предложения эволюции (для экспертов) -->
    {#if canReview && proposals.length > 0}
      <section class="workspace-panel" style="margin-top:20px">
        <div class="panel-intro">
          <div>
            <p class="section-label">Экспертная проверка</p>
            <h2>Предложения, ожидающие решения ({proposals.filter((p) => p.status === 'proposed').length})</h2>
          </div>
          <p>Принятие или отклонение предложений эволюции базы знаний.</p>
        </div>

        <div class="feedback-proposals">
          {#each proposals.filter((p) => p.status === 'proposed') as proposal}
            <article class="feedback-proposal">
              <div class="feedback-proposal__head">
                <span class={`status status--${proposal.status}`}>ожидает решения</span>
                <small>{proposal.kind}</small>
              </div>
              <h4>{proposal.title}</h4>
              <p>{proposal.change}</p>
              <div class="feedback-proposal__impact">
                Затрагивает: {proposal.impact.join(' · ')}
              </div>
              <div class="review-actions">
                <button class="button button--primary button--small" onclick={() => reviewProposal(proposal, true)}>
                  <CheckCircle2 size={16} /> Принять
                </button>
                <button class="button button--quiet button--small" onclick={() => reviewProposal(proposal, false)}>
                  <X size={16} /> Отклонить
                </button>
              </div>
            </article>
          {/each}
        </div>
      </section>
    {/if}

    <!-- История уведомлений -->
    {#if notifications.length > 0}
      <section class="workspace-panel" style="margin-top:20px">
        <div class="panel-intro">
          <div>
            <p class="section-label">Уведомления</p>
            <h2>Последние события системы</h2>
          </div>
        </div>
        <div class="feedback-notifications">
          {#each notifications as n}
            <div class="feedback-notif">
              <span class="feedback-notif__dot" data-type={n.type ?? 'info'}></span>
              <div class="feedback-notif__body">
                <p>{n.message}</p>
                <small>{formatTime(n.created_at)}</small>
              </div>
            </div>
          {/each}
        </div>
      </section>
    {/if}

    <!-- Пустое состояние -->
    {#if proposals.length === 0 && notifications.length === 0}
      <div class="workspace-panel empty-state">
        <MessageSquare size={32} />
        <h2>Нет данных</h2>
        <p>Предложения и уведомления появятся после взаимодействия с системой.</p>
      </div>
    {/if}
  {/if}
</main>

<style>
  .feedback-form-page {
    padding: 36px;
  }
  .feedback-form-fields {
    display: flex;
    flex-direction: column;
    gap: 18px;
    margin-top: 20px;
  }
  .feedback-form-field {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .feedback-form-field label {
    font-size: 13px;
    font-weight: 700;
    color: var(--ink);
  }
  .feedback-form-field input {
    padding: 10px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    font-size: 14px;
    background: white;
    color: var(--ink);
  }
  .feedback-form-field input:focus {
    border-color: var(--cobalt);
    outline: 2px solid rgb(36 87 214 / 18%);
  }
  .feedback-form-field textarea {
    padding: 10px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    font-size: 14px;
    background: white;
    color: var(--ink);
    resize: vertical;
    font-family: inherit;
  }
  .feedback-form-field textarea:focus {
    border-color: var(--cobalt);
    outline: 2px solid rgb(36 87 214 / 18%);
  }
  .feedback-verdict-buttons {
    display: flex;
    gap: 10px;
  }
  .feedback-verdict-buttons button {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 12px 20px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: white;
    cursor: pointer;
    font-size: 14px;
    font-weight: 600;
    color: var(--ink);
    transition: all 0.15s;
  }
  .feedback-verdict-buttons button:hover {
    border-color: var(--cobalt);
  }
  .feedback-verdict--active {
    border-color: var(--cobalt) !important;
    background: #f0f4ff !important;
    color: var(--cobalt) !important;
  }
  .feedback-form-actions {
    display: flex;
    justify-content: flex-end;
  }
  .feedback-proposals {
    display: flex;
    flex-direction: column;
    gap: 14px;
    margin-top: 20px;
  }
  .feedback-proposal {
    padding: 16px;
    background: var(--surface);
    border-radius: 10px;
  }
  .feedback-proposal__head {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
  }
  .feedback-proposal h4 {
    margin: 0 0 6px;
    font-size: 15px;
  }
  .feedback-proposal p {
    margin: 0 0 8px;
    color: #555f59;
    font-size: 13px;
    line-height: 1.5;
  }
  .feedback-proposal__impact {
    font-size: 11px;
    color: var(--muted);
    margin-bottom: 10px;
  }
  .feedback-notifications {
    display: flex;
    flex-direction: column;
    gap: 0;
    margin-top: 20px;
  }
  .feedback-notif {
    display: flex;
    gap: 10px;
    padding: 12px 0;
    border-bottom: 1px solid var(--line);
  }
  .feedback-notif:last-child {
    border: 0;
  }
  .feedback-notif__dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex: none;
    margin-top: 5px;
    background: var(--cobalt);
  }
  .feedback-notif__dot[data-type="success"] {
    background: var(--green);
  }
  .feedback-notif__dot[data-type="warning"] {
    background: var(--amber);
  }
  .feedback-notif__body p {
    margin: 0;
    font-size: 13px;
    line-height: 1.5;
    color: var(--ink);
  }
  .feedback-notif__body small {
    display: block;
    margin-top: 3px;
    font-size: 11px;
    color: var(--muted);
  }
</style>
