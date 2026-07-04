<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { env } from '$env/dynamic/public';
  import { api } from '$lib/api';
  import GraphCanvas from '$lib/GraphCanvas.svelte';
  import { getRoleHeaders } from '$lib/roleStore.svelte';
  import type {
    AnswerPayload,
    EvaluationRun,
    GraphSnapshot,
    GraphNode,
    Evidence,
    Finding,
    SystemStatus,
    QueryResponse,
  } from '$lib/types';
  import {
    Loader2, Check, Search, FileText, BarChart3, Pencil, Wrench, MessageSquare,
    X, ChevronDown, Network, AlertTriangle, ThumbsUp, ThumbsDown, Send,
    Sparkles, Activity,
  } from '@lucide/svelte';

  // ── Types ──────────────────────────────────────────────────────
  interface AgentStep {
    id: string;
    label: string;
    description: string;
    status: 'running' | 'completed';
    result?: string;
    toolDetails?: string[];
  }

  interface ChatMessage {
    id: string;
    role: 'user' | 'assistant';
    content: string;
    timestamp: number;
    agentSteps?: AgentStep[];
    answer?: AnswerPayload;
    evaluation?: EvaluationRun;
    isRefinement?: boolean;
    error?: string;
    retryQuery?: string;
  }

  // ── State ──────────────────────────────────────────────────────
  let messages = $state<ChatMessage[]>([]);
  let input = $state('');
  let loading = $state(false);
  let messagesContainer: HTMLDivElement | null = $state(null);
  let status: SystemStatus | null = $state(null);

  // Graph panel state
  let graphPanelOpen = $state(false);
  let graphPanelData = $state<GraphSnapshot>({ nodes: [], edges: [], communities: [] });
  let graphSelectedId = $state<string | undefined>(undefined);

  // Refinement state
  let refiningMessageId = $state<string | null>(null);
  let refinementInput = $state('');

  // Correction state
  let correctingMessageId = $state<string | null>(null);
  let correctionText = $state('');
  let correctionSubmitted = $state(false);

  // Feedback state
  let feedbackMessageId = $state<string | null>(null);
  let feedbackHelpful = $state<boolean | null>(null);
  let feedbackText = $state('');
  let feedbackSubmitted = $state(false);

  // Expandable sections
  let expandedSources = $state<Set<string>>(new Set());
  let expandedFindings = $state<Set<string>>(new Set());

  // Expanded source items (full quote)
  let expandedSourceItems = $state<Set<string>>(new Set());

  // Expanded trace (agent timeline)
  let expandedTrace = $state<Set<string>>(new Set());

  const API_URL = env.PUBLIC_API_URL || '/backend';

  // ── Helpers ────────────────────────────────────────────────────
  function uid(): string {
    return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
  }

  function pct(v: number): string {
    return `${Math.round(v * 100)}%`;
  }

  function confColor(v: number): string {
    if (v >= 0.7) return '#1f7650';
    if (v >= 0.4) return '#a66513';
    return '#ad3434';
  }

  function statusLabel(mode: string): string {
    const map: Record<string, string> = {
      yandex: 'YandexGPT', gigachat: 'GigaChat', fallback: 'Резервный режим', scripted: 'Демо-режим',
    };
    return map[mode] ?? mode;
  }

  function formatTime(ts: number): string {
    return new Date(ts).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }

  // ── Agent Steps Builder ────────────────────────────────────────
  function buildAgentSteps(): AgentStep[] {
    return [
      { id: 'plan', label: 'Планировщик', description: 'Анализ запроса и планирование инструментов', status: 'running' },
      { id: 'search', label: 'Поиск', description: 'Гибридный поиск по корпусу и графу', status: 'running' },
      { id: 'analyze', label: 'Анализ', description: 'Извлечение фактов и числовых данных', status: 'running' },
      { id: 'synth', label: 'Синтез', description: 'Формирование ответа с доказательствами', status: 'running' },
      { id: 'critic', label: 'Критика', description: 'Проверка конфликтов и пробелов', status: 'running' },
      { id: 'improve', label: 'Улучшение', description: 'Оценка качества и доработка', status: 'running' },
    ];
  }

  const toolLabels: Record<string, string> = {
    hybrid_search: 'Гибридный поиск',
    graph_traverse: 'Обход графа',
    community_search: 'Поиск по сообществам',
    numeric_filter: 'Числовая фильтрация',
    conflict_scan: 'Поиск конфликтов',
    gap_scan: 'Поиск пробелов',
    expert_lookup: 'Поиск экспертов',
  };

  function updateStepsWithResult(steps: AgentStep[], answer: AnswerPayload): AgentStep[] {
    const evidenceCount = answer.findings.reduce((sum, f) => sum + f.evidence.length, 0);
    const conflictCount = answer.conflicts.length;
    const gapCount = answer.knowledge_gaps.length;
    const toolCount = answer.tool_observations.length;
    const toolSummaries = answer.tool_observations.map((t) =>
      `${toolLabels[t.tool] ?? t.tool}: ${t.summary}`
    );
    return steps.map((s) => {
      const updated = { ...s, status: 'completed' as const };
      if (s.id === 'plan') updated.result = `${answer.tool_observations.length} инструментов запланировано`;
      if (s.id === 'search') {
        updated.result = `${evidenceCount} источников · ${toolCount} инструментов`;
        updated.toolDetails = toolSummaries;
      }
      if (s.id === 'analyze') updated.result = `${answer.findings.length} утверждений извлечено`;
      if (s.id === 'synth') updated.result = 'Ответ сформирован';
      if (s.id === 'critic') updated.result = `${conflictCount} конфликтов, ${gapCount} пробелов`;
      if (s.id === 'improve') updated.result = `Уверенность: ${pct(answer.confidence)}`;
      return updated;
    });
  }

  function toggleTrace(msgId: string): void {
    const next = new Set(expandedTrace);
    if (next.has(msgId)) next.delete(msgId);
    else next.add(msgId);
    expandedTrace = next;
  }

  function agentLabel(agent: string): string {
    const map: Record<string, string> = {
      intent_router: 'Маршрутизатор намерений',
      planner: 'Планировщик',
      action_planner: 'Планировщик действий',
      tool_executor: 'Исполнитель инструментов',
      controller: 'Контроллер',
      reasoner: 'Анализ',
      critic: 'Критика',
      improver: 'Улучшение',
      synthesizer: 'Синтез',
    };
    return map[agent] ?? agent;
  }

  async function animateSteps(messageId: string, steps: AgentStep[]): Promise<void> {
    for (let i = 0; i < steps.length; i++) {
      await new Promise((r) => setTimeout(r, 200));
      const msg = messages.find((m) => m.id === messageId);
      if (!msg || !msg.agentSteps) return;
      msg.agentSteps = msg.agentSteps.map((s, idx) =>
        idx <= i ? { ...s, status: 'completed' as const } : s
      );
      messages = [...messages];
    }
  }

  // ── SSE Streaming ─────────────────────────────────────────────
  const sseAgentToStepId: Record<string, string> = {
    intent_router: 'plan',
    planner: 'plan',
    action_planner: 'plan',
    tool_executor: 'search',
    controller: 'search',
    reasoner: 'analyze',
    synthesizer: 'synth',
    critic: 'critic',
    improver: 'improve',
  };

  function updateStepFromSSE(
    messageId: string,
    step: { agent: string; status: string; message?: string },
  ): void {
    const msg = messages.find((m) => m.id === messageId);
    if (!msg || !msg.agentSteps) return;
    const stepId = sseAgentToStepId[step.agent];
    if (!stepId) return;
    msg.agentSteps = msg.agentSteps.map((s) => {
      if (s.id !== stepId) return s;
      return {
        ...s,
        status: step.status === 'completed' ? 'completed' as const : 'running' as const,
        result: step.message ?? s.result,
      };
    });
    messages = [...messages];
  }

  async function streamQuery(query: string, assistantId: string): Promise<void> {
    const response = await fetch(`${API_URL}/api/v1/query/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...getRoleHeaders() },
      body: JSON.stringify({ question: query, language: 'ru', mode: 'hybrid' }),
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    if (!response.body) {
      throw new Error('No response body for streaming');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let data: { type: string; step?: { agent: string; status: string; message?: string }; answer?: AnswerPayload | QueryResponse; message?: string; error?: string };
        try {
          data = JSON.parse(line.slice(6));
        } catch {
          continue;
        }
        if (data.type === 'step' && data.step) {
          updateStepFromSSE(assistantId, data.step);
        } else if (data.type === 'answer' && data.answer) {
          // Handle both QueryResponse and bare AnswerPayload formats
          let answer: AnswerPayload;
          let evaluation: EvaluationRun | undefined;
          if ('answer' in data.answer && data.answer.answer) {
            const qr = data.answer as QueryResponse;
            answer = qr.answer;
            evaluation = qr.evaluation;
          } else {
            answer = data.answer as AnswerPayload;
          }
          const msg = messages.find((m) => m.id === assistantId);
          if (!msg) return;
          msg.agentSteps = updateStepsWithResult(msg.agentSteps!, answer);
          msg.answer = answer;
          msg.evaluation = evaluation;
          msg.content = answer.summary;
          messages = [...messages];
          await scrollToBottom();
        } else if (data.type === 'done') {
          const msg = messages.find((m) => m.id === assistantId);
          if (msg && msg.agentSteps) {
            msg.agentSteps = msg.agentSteps.map((s) => ({ ...s, status: 'completed' as const }));
            messages = [...messages];
          }
        } else if (data.type === 'error') {
          throw new Error(data.message || data.error || 'Ошибка потоковой передачи');
        }
      }
    }
  }

  // ── API Calls ─────────────────────────────────────────────────
  async function sendMessage(query: string, isRefinement = false): Promise<void> {
    if (!query.trim() || loading) return;

    const userMsg: ChatMessage = {
      id: uid(), role: 'user', content: query, timestamp: Date.now(),
    };
    const assistantId = uid();
    const assistantMsg: ChatMessage = {
      id: assistantId, role: 'assistant', content: '', timestamp: Date.now(),
      agentSteps: buildAgentSteps(), isRefinement,
    };
    messages = [...messages, userMsg, assistantMsg];
    input = '';
    loading = true;
    await scrollToBottom();

    try {
      // Try SSE streaming first
      await streamQuery(query, assistantId);
    } catch (streamErr) {
      // If we already received the answer via streaming, just finalize
      const existingMsg = messages.find((m) => m.id === assistantId);
      if (existingMsg && existingMsg.answer) {
        if (existingMsg.agentSteps) {
          existingMsg.agentSteps = existingMsg.agentSteps.map((s) => ({ ...s, status: 'completed' as const }));
          messages = [...messages];
        }
        return;
      }

      // Fall back to non-streaming endpoint
      try {
        const result: QueryResponse = await api.query(query);
        const answer = result.answer;

        const msg = messages.find((m) => m.id === assistantId);
        if (!msg) return;
        msg.agentSteps = updateStepsWithResult(msg.agentSteps!, answer);
        msg.answer = answer;
        msg.evaluation = result.evaluation;
        msg.content = answer.summary;
        messages = [...messages];

        await animateSteps(assistantId, msg.agentSteps!);

        const finalMsg = messages.find((m) => m.id === assistantId);
        if (finalMsg && finalMsg.agentSteps) {
          finalMsg.agentSteps = finalMsg.agentSteps.map((s) => ({ ...s, status: 'completed' as const }));
          messages = [...messages];
        }
      } catch (err) {
        const msg = messages.find((m) => m.id === assistantId);
        if (!msg) return;
        const errMsg = err instanceof Error ? err.message : 'Неизвестная ошибка';
        if (errMsg.includes('403') || errMsg.includes('Forbidden')) {
          msg.error = 'Нет доступа. Проверьте роль пользователя.';
        } else if (errMsg.includes('500') || errMsg.includes('Internal')) {
          msg.error = 'Произошла ошибка. Попробуйте переформулировать вопрос.';
        } else if (errMsg.includes('fetch') || errMsg.includes('Network')) {
          msg.error = 'Не удалось подключиться к серверу. Проверьте, что бэкенд запущен.';
        } else {
          msg.error = errMsg;
        }
        msg.retryQuery = query;
        msg.agentSteps = [];
        messages = [...messages];
      }
    } finally {
      loading = false;
    }
  }

  async function retry(msg: ChatMessage): Promise<void> {
    if (!msg.retryQuery) return;
    // Remove the error message
    messages = messages.filter((m) => m.id !== msg.id);
    await sendMessage(msg.retryQuery, msg.isRefinement);
  }

  // ── Graph Panel ────────────────────────────────────────────────
  function openGraph(msg: ChatMessage): void {
    if (!msg.answer) return;
    graphPanelData = msg.answer.graph;
    graphSelectedId = undefined;
    graphPanelOpen = true;
  }

  function closeGraph(): void {
    graphPanelOpen = false;
  }

  function showFindingInGraph(msg: ChatMessage, finding: Finding): void {
    if (!msg.answer) return;
    graphPanelData = msg.answer.graph;
    // Try to match finding.id to a graph node
    const matchId = msg.answer.graph.nodes.find(
      (n) => n.id === finding.id || n.id === finding.id.replace('finding-', '')
    )?.id;
    graphSelectedId = matchId;
    graphPanelOpen = true;
  }

  // ── Expandable Sections ────────────────────────────────────────
  function toggleSources(msgId: string): void {
    const next = new Set(expandedSources);
    if (next.has(msgId)) next.delete(msgId);
    else next.add(msgId);
    expandedSources = next;
  }

  function toggleFindings(msgId: string): void {
    const next = new Set(expandedFindings);
    if (next.has(msgId)) next.delete(msgId);
    else next.add(msgId);
    expandedFindings = next;
  }

  function toggleSourceItem(itemId: string): void {
    const next = new Set(expandedSourceItems);
    if (next.has(itemId)) next.delete(itemId);
    else next.add(itemId);
    expandedSourceItems = next;
  }

  // ── Refinement Flow ────────────────────────────────────────────
  function startRefinement(msg: ChatMessage): void {
    refiningMessageId = msg.id;
    refinementInput = '';
    correctingMessageId = null;
    feedbackMessageId = null;
  }

  function cancelRefinement(): void {
    refiningMessageId = null;
    refinementInput = '';
  }

  async function submitRefinement(msg: ChatMessage): Promise<void> {
    if (!refinementInput.trim() || !msg.answer) return;
    const originalQuery = msg.answer.question;
    const newQuery = `${originalQuery} Уточнение: ${refinementInput}`;
    refiningMessageId = null;
    refinementInput = '';
    await sendMessage(newQuery, true);
  }

  // ── Correction Flow (Self-Evolving) ────────────────────────────
  function startCorrection(msg: ChatMessage): void {
    correctingMessageId = msg.id;
    correctionText = msg.content || msg.answer?.summary || '';
    correctionSubmitted = false;
    refiningMessageId = null;
    feedbackMessageId = null;
  }

  function cancelCorrection(): void {
    correctingMessageId = null;
    correctionText = '';
    correctionSubmitted = false;
  }

  async function submitCorrection(msg: ChatMessage): Promise<void> {
    if (!msg.answer || !correctionText.trim()) return;
    try {
      await fetch(`${API_URL}/api/v1/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getRoleHeaders() },
        body: JSON.stringify({
          query_id: msg.answer.query_id,
          finding_id: null,
          verdict: 'correct',
          comment: 'Исправление ответа от пользователя',
          correction: correctionText,
        }),
      });
      correctionSubmitted = true;
    } catch {
      correctionSubmitted = true;
    }
    setTimeout(() => {
      correctingMessageId = null;
      correctionText = '';
      correctionSubmitted = false;
    }, 3000);
  }

  // ── Feedback Flow ──────────────────────────────────────────────
  function startFeedback(msg: ChatMessage): void {
    feedbackMessageId = msg.id;
    feedbackHelpful = null;
    feedbackText = '';
    feedbackSubmitted = false;
    refiningMessageId = null;
    correctingMessageId = null;
  }

  function cancelFeedback(): void {
    feedbackMessageId = null;
    feedbackHelpful = null;
    feedbackText = '';
    feedbackSubmitted = false;
  }

  async function submitFeedback(msg: ChatMessage): Promise<void> {
    if (!msg.answer) return;
    const helpful = feedbackHelpful ?? true;
    const comment = feedbackText.trim().length >= 3
      ? feedbackText.trim()
      : (helpful ? 'Ответ был полезен' : 'Ответ не был полезен');
    try {
      await fetch(`${API_URL}/api/v1/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getRoleHeaders() },
        body: JSON.stringify({
          query_id: msg.answer.query_id,
          finding_id: null,
          verdict: helpful ? 'accept' : 'reject',
          comment,
        }),
      });
    } catch { /* ignore */ }
    feedbackSubmitted = true;
    setTimeout(() => {
      feedbackMessageId = null;
      feedbackHelpful = null;
      feedbackText = '';
      feedbackSubmitted = false;
    }, 3000);
  }

  // ── Input Handling ─────────────────────────────────────────────
  function handleKeyDown(e: KeyboardEvent): void {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage(input);
    }
  }

  function handleInput(): void {
    // Auto-resize handled by CSS
  }

  // ── Derived Data ───────────────────────────────────────────────
  function getAllEvidence(answer: AnswerPayload): { evidence: Evidence; finding: Finding }[] {
    return answer.findings.flatMap((f) => f.evidence.map((e) => ({ evidence: e, finding: f })));
  }

  function evidenceCount(answer: AnswerPayload): number {
    return answer.findings.reduce((sum, f) => sum + f.evidence.length, 0);
  }

  // ── Scroll ─────────────────────────────────────────────────────
  async function scrollToBottom(): Promise<void> {
    await tick();
    if (messagesContainer) {
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }
  }

  // ── Lifecycle ──────────────────────────────────────────────────
  onMount(async () => {
    try {
      status = await api.status();
    } catch {
      status = null;
    }
  });
</script>

<div class="chat-panel">
  <!-- Header -->
  <div class="chat-header">
    <div class="chat-header__title">
      <Sparkles size={22} />
      <div>
        <h1>Научный Клубок — Запросы</h1>
        <p>Задайте вопрос — агенты найдут ответ в графе знаний</p>
      </div>
    </div>
    {#if status}
      <div class="chat-status" class:ready={status.status === 'ready'}>
        <span></span>
        {status.status === 'ready' ? 'Все сервисы готовы' : statusLabel(status.model_mode)}
      </div>
    {/if}
  </div>

  <!-- Messages Area -->
  <div class="chat-messages" bind:this={messagesContainer}>
    {#if messages.length === 0}
      <div class="chat-welcome">
        <div class="chat-welcome__icon"><Sparkles size={36} /></div>
        <h2>Задайте вопрос по графику знаний</h2>
        <p>Агенты проанализируют запрос, найдут источники, выявят противоречия и сформируют ответ с оценкой качества.</p>
        <div class="chat-suggestions">
          <button onclick={() => sendMessage('Обзор способов удаления SO2 из отходящих газов металлургических предприятий')}>
            Обзор способов удаления SO₂ из отходящих газов
          </button>
          <button onclick={() => sendMessage('Обзор современных способов переработки свинцово-цинкового сырья')}>
            Переработка свинцово-цинкового сырья
          </button>
          <button onclick={() => sendMessage('Анализ технологий и примеров закачки шахтных вод в глубокие горизонты')}>
            Закачка шахтных вод в глубокие горизонты
          </button>
        </div>
      </div>
    {/if}

    {#each messages as msg (msg.id)}
      {#if msg.role === 'user'}
        <div class="msg msg--user">
          <div class="msg__bubble msg__bubble--user">
            {#if msg.isRefinement}
              <span class="msg__badge">Уточнение</span>
            {/if}
            {msg.content}
          </div>
          <span class="msg__time">{formatTime(msg.timestamp)}</span>
        </div>
      {:else}
        <div class="msg msg--assistant">
          <div class="msg__avatar"><Sparkles size={16} /></div>
          <div class="msg__content">
            {#if msg.isRefinement}
              <span class="msg__badge msg__badge--refined">Уточнённый ответ</span>
            {/if}

            <!-- Agent Steps -->
            {#if msg.agentSteps && msg.agentSteps.length > 0}
              <div class="agent-steps">
                {#each msg.agentSteps as step (step.id)}
                  <div class="agent-step" class:completed={step.status === 'completed'}>
                    <span class="agent-step__icon">
                      {#if step.status === 'running'}
                        <Loader2 size={15} class="spin" />
                      {:else}
                        <Check size={15} />
                      {/if}
                    </span>
                    <span class="agent-step__label">{step.label}</span>
                    {#if step.result}
                      <span class="agent-step__result">{step.result}</span>
                    {/if}
                  </div>
                {/each}
              </div>
            {/if}

            <!-- Error -->
            {#if msg.error}
              <div class="msg__error">
                <AlertTriangle size={18} />
                <div>
                  <strong>{msg.error}</strong>
                  <button class="msg__retry" onclick={() => retry(msg)}>
                    <Loader2 size={14} /> Повторить
                  </button>
                </div>
              </div>
            {/if}

            <!-- Answer -->
            {#if msg.answer}
              <!-- Answer text -->
              <div class="answer-text">
                <p>{msg.content}</p>
                {#if msg.answer.findings.length > 0}
                  <ul class="answer-findings-list">
                    {#each msg.answer.findings.slice(0, 5) as finding}
                      <li>{finding.statement}</li>
                    {/each}
                  </ul>
                  {#if msg.answer.findings.length > 5}
                    <p class="answer-more">и ещё {msg.answer.findings.length - 5} утверждений…</p>
                  {/if}
                {/if}
              </div>

              <!-- Conflicts -->
              {#if msg.answer.conflicts.length > 0}
                <div class="conflict-cards">
                  {#each msg.answer.conflicts as conflict}
                    <div class="card card--conflict">
                      <AlertTriangle size={15} />
                      <span>{conflict}</span>
                    </div>
                  {/each}
                </div>
              {/if}

              <!-- Gaps -->
              {#if msg.answer.knowledge_gaps.length > 0}
                <div class="gap-cards">
                  {#each msg.answer.knowledge_gaps as gap}
                    <div class="card card--gap">
                      <Search size={15} />
                      <span>{gap}</span>
                    </div>
                  {/each}
                </div>
              {/if}

              <!-- Metrics -->
              <div class="metrics-row">
                <span class="metric" style={`color:${confColor(msg.answer.confidence)}`}>
                  Уверенность: {pct(msg.answer.confidence)}
                </span>
                {#if msg.evaluation}
                  <span class="metric">Покрытие: {pct(msg.evaluation.metrics.citation_coverage)}</span>
                {/if}
                <span class="metric metric--model">{statusLabel(msg.answer.model_mode)}</span>
              </div>

              <!-- Action buttons -->
              <div class="action-buttons">
                <button class="action-btn" onclick={() => openGraph(msg)}>
                  <Search size={15} /> Открыть граф
                </button>
                <button class="action-btn" onclick={() => toggleSources(msg.id)}>
                  <FileText size={15} /> Источники ({evidenceCount(msg.answer)})
                  <span class={expandedSources.has(msg.id) ? 'rotated' : ''}><ChevronDown size={13} /></span>
                </button>
                <button class="action-btn" onclick={() => toggleFindings(msg.id)}>
                  <BarChart3 size={15} /> Находки ({msg.answer.findings.length})
                  <span class={expandedFindings.has(msg.id) ? 'rotated' : ''}><ChevronDown size={13} /></span>
                </button>
                <button class="action-btn" onclick={() => toggleTrace(msg.id)}>
                  <Activity size={15} /> Трассировка ({msg.answer.tool_observations.length + msg.answer.trace.length})
                  <span class={expandedTrace.has(msg.id) ? 'rotated' : ''}><ChevronDown size={13} /></span>
                </button>
              </div>

              <!-- Self-evolving controls -->
              <div class="evolving-controls">
                <button class="action-btn action-btn--ghost" onclick={() => startRefinement(msg)}>
                  <Pencil size={14} /> Уточнить
                </button>
                <button class="action-btn action-btn--ghost" onclick={() => startCorrection(msg)}>
                  <Wrench size={14} /> Исправить
                </button>
                <button class="action-btn action-btn--ghost" onclick={() => startFeedback(msg)}>
                  <MessageSquare size={14} /> Отзыв
                </button>
              </div>

              <!-- Expandable: Sources -->
              {#if expandedSources.has(msg.id)}
                <div class="expandable-section">
                  <h4><FileText size={15} /> Источники</h4>
                  {#each getAllEvidence(msg.answer) as { evidence: ev, finding }, i}
                    {@const itemId = `${msg.id}-src-${i}`}
                    <div class="source-item">
                      <div class="source-item__header">
                        <strong>{ev.source_title || 'Источник'}</strong>
                        {#if ev.score != null}
                          <span class="source-item__score">Релевантность: {pct(ev.score)}</span>
                        {/if}
                      </div>
                      <p class="source-item__quote">
                        {expandedSourceItems.has(itemId) ? ev.quote : ev.quote.slice(0, 100) + (ev.quote.length > 100 ? '…' : '')}
                      </p>
                      {#if ev.page || ev.sheet || ev.cell_range}
                        <div class="source-item__meta">
                          {#if ev.page}<span>Стр. {ev.page}</span>{/if}
                          {#if ev.sheet}<span>Лист «{ev.sheet}»</span>{/if}
                          {#if ev.cell_range}<span>Ячейки {ev.cell_range}</span>{/if}
                        </div>
                      {/if}
                      {#if ev.quote.length > 100}
                        <button class="source-item__toggle" onclick={() => toggleSourceItem(itemId)}>
                          {expandedSourceItems.has(itemId) ? 'Свернуть' : 'Показать полностью'}
                        </button>
                      {/if}
                    </div>
                  {/each}
                </div>
              {/if}

              <!-- Expandable: Findings -->
              {#if expandedFindings.has(msg.id)}
                <div class="expandable-section">
                  <h4><BarChart3 size={15} /> Находки</h4>
                  {#each msg.answer.findings as finding}
                    <div class="finding-card">
                      <div class="finding-card__spo">
                        {#if finding.subject && finding.predicate}
                          <span class="finding-card__subject">{finding.subject}</span>
                          <span class="finding-card__arrow">→</span>
                          <span class="finding-card__predicate">{finding.predicate}</span>
                          <span class="finding-card__arrow">→</span>
                        {/if}
                        <span class="finding-card__statement">{finding.statement}</span>
                      </div>
                      <div class="finding-card__conf">
                        <div class="finding-card__bar">
                          <div style={`width:${pct(finding.confidence)};background:${confColor(finding.confidence)}`}></div>
                        </div>
                        <span style={`color:${confColor(finding.confidence)}`}>{pct(finding.confidence)}</span>
                      </div>
                      <div class="finding-card__actions">
                        <button onclick={() => showFindingInGraph(msg, finding)}>
                          <Network size={13} /> Показать в графе
                        </button>
                        <span class="finding-card__evidence-count">
                          {finding.evidence.length} источн.
                        </span>
                        {#if finding.status === 'disputed'}
                          <span class="finding-card__status finding-card__status--disputed">расхождения</span>
                        {:else if finding.status === 'hypothesis'}
                          <span class="finding-card__status finding-card__status--hypothesis">гипотеза</span>
                        {:else}
                          <span class="finding-card__status finding-card__status--consensus">согласуется</span>
                        {/if}
                      </div>
                    </div>
                  {/each}
                </div>
              {/if}

              <!-- Expandable: Trace -->
              {#if expandedTrace.has(msg.id)}
                <div class="expandable-section">
                  <h4><Activity size={15} /> Трассировка агентов</h4>
                  {#if msg.answer.tool_observations.length > 0}
                    <div class="trace-section">
                      <small>Инструменты ({msg.answer.tool_observations.length})</small>
                      {#each msg.answer.tool_observations as obs}
                        <div class="trace-item">
                          <span class="trace-item__status trace-item__status--{obs.status}"></span>
                          <span class="trace-item__tool">{toolLabels[obs.tool] ?? obs.tool}</span>
                          <span class="trace-item__summary">{obs.summary}</span>
                        </div>
                      {/each}
                    </div>
                  {/if}
                  {#if msg.answer.trace.length > 0}
                    <div class="trace-section">
                      <small>Агенты ({msg.answer.trace.length})</small>
                      {#each msg.answer.trace as event}
                        <div class="trace-item">
                          <span class="trace-item__status trace-item__status--{event.status}"></span>
                          <span class="trace-item__tool">{agentLabel(event.agent)}</span>
                          <span class="trace-item__summary">{event.message}</span>
                          {#if event.duration_ms > 0}
                            <span class="trace-item__duration">{(event.duration_ms / 1000).toFixed(1)}с</span>
                          {/if}
                        </div>
                      {/each}
                    </div>
                  {/if}
                </div>
              {/if}

              <!-- Refinement input -->
              {#if refiningMessageId === msg.id}
                <div class="inline-input">
                  <textarea
                    bind:value={refinementInput}
                    placeholder="Уточните запрос (например: только для медной металлургии)…"
                    rows="2"
                  ></textarea>
                  <div class="inline-input__actions">
                    <button class="action-btn" onclick={() => submitRefinement(msg)} disabled={!refinementInput.trim()}>
                      <Send size={14} /> Отправить
                    </button>
                    <button class="action-btn action-btn--ghost" onclick={cancelRefinement}>Отмена</button>
                  </div>
                </div>
              {/if}

              <!-- Correction input -->
              {#if correctingMessageId === msg.id}
                {#if correctionSubmitted}
                  <div class="inline-success">
                    <Check size={16} /> Спасибо! Ваше исправление отправлено для улучшения системы.
                  </div>
                {:else}
                  <div class="inline-input">
                    <textarea
                      bind:value={correctionText}
                      rows="4"
                      placeholder="Отредактируйте ответ…"
                    ></textarea>
                    <div class="inline-input__actions">
                      <button class="action-btn" onclick={() => submitCorrection(msg)} disabled={!correctionText.trim()}>
                        <Send size={14} /> Отправить исправление
                      </button>
                      <button class="action-btn action-btn--ghost" onclick={cancelCorrection}>Отмена</button>
                    </div>
                  </div>
                {/if}
              {/if}

              <!-- Feedback form -->
              {#if feedbackMessageId === msg.id}
                {#if feedbackSubmitted}
                  <div class="inline-success">
                    <Check size={16} /> Спасибо за отзыв!
                  </div>
                {:else}
                  <div class="feedback-form">
                    <p class="feedback-form__question">Был ли ответ полезен?</p>
                    <div class="feedback-form__buttons">
                      <button
                        class:feedback-btn--active={feedbackHelpful === true}
                        onclick={() => feedbackHelpful = true}
                      >
                        <ThumbsUp size={15} /> Да
                      </button>
                      <button
                        class:feedback-btn--active={feedbackHelpful === false}
                        onclick={() => feedbackHelpful = false}
                      >
                        <ThumbsDown size={15} /> Нет
                      </button>
                    </div>
                    <textarea
                      bind:value={feedbackText}
                      placeholder="Что можно улучшить?"
                      rows="2"
                    ></textarea>
                    <div class="inline-input__actions">
                      <button
                        class="action-btn"
                        onclick={() => submitFeedback(msg)}
                        disabled={feedbackHelpful === null}
                      >
                        <Send size={14} /> Отправить
                      </button>
                      <button class="action-btn action-btn--ghost" onclick={cancelFeedback}>Отмена</button>
                    </div>
                  </div>
                {/if}
              {/if}
            {/if}
          </div>
        </div>
      {/if}
    {/each}
  </div>

  <!-- Input Area -->
  <div class="chat-input-area">
    <div class="chat-input-wrapper">
      <textarea
        bind:value={input}
        onkeydown={handleKeyDown}
        oninput={handleInput}
        placeholder="Задайте вопрос по графику знаний…"
        rows="1"
        disabled={loading}
      ></textarea>
      <button
        class="chat-send-btn"
        onclick={() => sendMessage(input)}
        disabled={loading || input.trim().length < 3}
        aria-label="Отправить"
      >
        {#if loading}
          <Loader2 size={18} class="spin" />
        {:else}
          <Send size={18} />
        {/if}
      </button>
    </div>
    <p class="chat-input-hint">Enter — отправить · Shift+Enter — новая строка</p>
  </div>
</div>

<!-- Graph Panel Overlay -->
{#if graphPanelOpen}
  <div class="graph-overlay" onclick={closeGraph} role="presentation">
    <div class="graph-panel" onclick={(e) => e.stopPropagation()} role="dialog" aria-label="Граф связей">
      <div class="graph-panel__header">
        <h3><Network size={18} /> Граф связей запроса</h3>
        <button class="graph-panel__close" onclick={closeGraph} aria-label="Закрыть">
          <X size={20} />
        </button>
      </div>
      <div class="graph-panel__body">
        {#if graphPanelData.nodes.length > 0}
          <GraphCanvas
            graph={graphPanelData}
            selectedId={graphSelectedId}
          />
        {:else}
          <div class="graph-panel__empty">
            <Network size={40} />
            <p>Граф не содержит данных для этого запроса</p>
          </div>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .chat-panel {
    display: flex;
    flex-direction: column;
    height: calc(100vh - 220px);
    min-height: 500px;
    max-width: 100%;
  }

  /* Header */
  .chat-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--line);
    margin-bottom: 0;
    flex-shrink: 0;
  }
  .chat-header__title {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .chat-header__title :global(svg) { color: var(--accent); flex: none; }
  .chat-header h1 {
    margin: 0;
    font-family: Georgia, serif;
    font-size: 24px;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }
  .chat-header p {
    margin: 4px 0 0;
    font-size: 13px;
    color: var(--muted);
  }
  .chat-status {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--muted);
    flex-shrink: 0;
  }
  .chat-status span {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--amber);
  }
  .chat-status.ready span { background: var(--green); }

  /* Messages Area */
  .chat-messages {
    flex: 1;
    overflow-y: auto;
    padding: 20px 0;
    scroll-behavior: smooth;
  }

  /* Welcome state */
  .chat-welcome {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 60px 20px;
    gap: 8px;
  }
  .chat-welcome__icon {
    display: grid;
    place-items: center;
    width: 64px;
    height: 64px;
    border-radius: 16px;
    background: var(--surface);
    color: var(--accent);
    margin-bottom: 8px;
  }
  .chat-welcome h2 {
    font-family: Georgia, serif;
    font-size: 26px;
    margin: 0;
  }
  .chat-welcome p {
    color: var(--muted);
    font-size: 15px;
    line-height: 1.5;
    max-width: 460px;
  }
  .chat-suggestions {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 20px;
    max-width: 520px;
    width: 100%;
  }
  .chat-suggestions button {
    text-align: left;
    padding: 12px 16px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: white;
    cursor: pointer;
    font-size: 13px;
    color: var(--ink);
    transition: all 0.15s;
  }
  .chat-suggestions button:hover {
    border-color: var(--accent);
    background: #fdf9f7;
  }

  /* Messages */
  .msg {
    display: flex;
    gap: 10px;
    margin-bottom: 20px;
    animation: msg-fade-in 0.2s ease;
  }
  @keyframes msg-fade-in {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .msg--user {
    flex-direction: row-reverse;
    align-items: flex-start;
  }
  .msg__avatar {
    display: grid;
    place-items: center;
    width: 32px;
    height: 32px;
    border-radius: 8px;
    background: var(--ink);
    color: white;
    flex-shrink: 0;
  }
  .msg__bubble {
    max-width: 75%;
    padding: 12px 16px;
    border-radius: 14px;
    font-size: 14px;
    line-height: 1.55;
  }
  .msg__bubble--user {
    background: var(--cobalt);
    color: white;
    border-bottom-right-radius: 4px;
  }
  .msg--user .msg__time {
    font-size: 11px;
    color: var(--muted);
    flex-shrink: 0;
    margin-top: 4px;
  }
  .msg--assistant .msg__content {
    max-width: 82%;
    flex: 1;
    min-width: 0;
  }
  .msg__badge {
    display: inline-block;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 5px;
    background: #e8f0ff;
    color: #1e40af;
    margin-bottom: 8px;
  }
  .msg__badge--refined {
    background: #fef3e2;
    color: #92400e;
  }
  .msg__time {
    font-size: 11px;
    color: var(--muted);
    margin-top: 4px;
  }

  /* Agent Steps */
  .agent-steps {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 12px 14px;
    background: var(--surface);
    border-radius: 10px;
    margin-bottom: 12px;
  }
  .agent-step {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: var(--muted);
    transition: color 0.2s;
  }
  .agent-step.completed {
    color: var(--ink);
  }
  .agent-step__icon {
    display: grid;
    place-items: center;
    width: 20px;
    height: 20px;
    flex-shrink: 0;
  }
  .agent-step:not(.completed) .agent-step__icon {
    color: var(--cobalt);
  }
  .agent-step.completed .agent-step__icon {
    color: var(--green);
  }
  .agent-step__label {
    font-weight: 600;
  }
  .agent-step__result {
    font-size: 12px;
    color: var(--muted);
    margin-left: auto;
    white-space: nowrap;
  }
  .agent-step.completed .agent-step__result {
    color: var(--green);
    font-weight: 600;
  }

  /* Error */
  .msg__error {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 14px;
    background: #fde8e6;
    border: 1px solid #f0b5ad;
    border-radius: 10px;
    color: #852d2d;
  }
  .msg__error :global(svg) { color: #ad3434; flex-shrink: 0; margin-top: 1px; }
  .msg__error strong { display: block; font-size: 13px; margin-bottom: 6px; }
  .msg__retry {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    border: 1px solid #f0b5ad;
    background: white;
    color: #852d2d;
    padding: 5px 12px;
    border-radius: 7px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
  }

  /* Answer Text */
  .answer-text {
    font-size: 14px;
    line-height: 1.6;
    color: var(--ink);
    margin-bottom: 12px;
  }
  .answer-text p {
    margin: 0 0 10px;
  }
  .answer-findings-list {
    margin: 8px 0 0;
    padding-left: 18px;
    list-style: none;
  }
  .answer-findings-list li {
    position: relative;
    padding: 4px 0;
    font-size: 13px;
    color: #555f59;
  }
  .answer-findings-list li::before {
    content: "▸";
    position: absolute;
    left: -14px;
    color: var(--accent);
  }
  .answer-more {
    font-size: 12px;
    color: var(--muted);
    margin-top: 6px;
  }

  /* Conflict & Gap Cards */
  .conflict-cards, .gap-cards {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-bottom: 10px;
  }
  .card {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 10px 12px;
    border-radius: 8px;
    font-size: 12px;
    line-height: 1.45;
  }
  .card :global(svg) { flex-shrink: 0; margin-top: 1px; }
  .card--conflict {
    background: #fde8e6;
    border: 1px solid #f0b5ad;
    color: #852d2d;
  }
  .card--conflict :global(svg) { color: #ad3434; }
  .card--gap {
    background: #fef3e2;
    border: 1px solid #f0d4a0;
    color: #84500f;
  }
  .card--gap :global(svg) { color: #a66513; }

  /* Metrics */
  .metrics-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 12px;
  }
  .metric {
    display: inline-flex;
    align-items: center;
    padding: 3px 10px;
    border-radius: 6px;
    background: var(--surface);
    font-size: 12px;
    font-weight: 600;
    color: var(--ink);
  }
  .metric--model {
    color: var(--muted);
    font-weight: 500;
  }

  /* Action Buttons */
  .action-buttons, .evolving-controls {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 8px;
  }
  .action-btn {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 7px 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: white;
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    color: var(--ink);
    transition: all 0.12s;
  }
  .action-btn:hover {
    border-color: var(--cobalt);
    color: var(--cobalt);
  }
  .action-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .action-btn--ghost {
    background: transparent;
    border-color: transparent;
    color: var(--muted);
  }
  .action-btn--ghost:hover {
    background: var(--surface);
    color: var(--ink);
  }
  :global(.rotated) {
    transform: rotate(180deg);
    transition: transform 0.15s;
  }

  /* Expandable Sections */
  .expandable-section {
    margin-top: 8px;
    padding: 12px;
    background: var(--surface);
    border-radius: 10px;
  }
  .expandable-section h4 {
    display: flex;
    align-items: center;
    gap: 6px;
    margin: 0 0 10px;
    font-size: 13px;
  }

  /* Source Items */
  .source-item {
    padding: 10px 0;
    border-bottom: 1px solid var(--line);
  }
  .source-item:last-child { border: 0; }
  .source-item__header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
  }
  .source-item__header strong {
    font-size: 13px;
  }
  .source-item__score {
    font-size: 11px;
    color: var(--muted);
    white-space: nowrap;
  }
  .source-item__quote {
    margin: 0;
    font-size: 12px;
    line-height: 1.5;
    color: #555f59;
  }
  .source-item__meta {
    display: flex;
    gap: 10px;
    margin-top: 4px;
    font-size: 10px;
    color: var(--muted);
  }
  .source-item__toggle {
    border: 0;
    background: transparent;
    color: var(--accent);
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    padding: 4px 0 0;
  }

  /* Finding Cards */
  .finding-card {
    padding: 10px;
    background: white;
    border: 1px solid var(--line);
    border-radius: 9px;
    margin-bottom: 8px;
  }
  .finding-card:last-child { margin: 0; }
  .finding-card__spo {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    font-size: 12px;
    line-height: 1.4;
    margin-bottom: 8px;
  }
  .finding-card__subject {
    font-weight: 700;
    color: var(--cobalt);
  }
  .finding-card__predicate {
    font-weight: 600;
    color: var(--accent);
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 0.04em;
  }
  .finding-card__arrow {
    color: var(--muted);
    font-size: 11px;
  }
  .finding-card__statement {
    color: var(--ink);
    flex: 1;
    min-width: 120px;
  }
  .finding-card__conf {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }
  .finding-card__bar {
    flex: 1;
    height: 5px;
    background: var(--surface);
    border-radius: 3px;
    overflow: hidden;
  }
  .finding-card__bar > div {
    height: 100%;
    border-radius: 3px;
    transition: width 0.3s;
  }
  .finding-card__conf > span {
    font-size: 12px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .finding-card__actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .finding-card__actions button {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    border: 1px solid var(--line);
    background: white;
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    color: var(--ink);
    transition: all 0.12s;
  }
  .finding-card__actions button:hover {
    border-color: var(--cobalt);
    color: var(--cobalt);
  }
  .finding-card__evidence-count {
    font-size: 11px;
    color: var(--muted);
  }
  .finding-card__status {
    font-size: 10px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 4px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .finding-card__status--consensus { background: #e1efe7; color: var(--green); }
  .finding-card__status--disputed { background: #fef3e2; color: var(--amber); }
  .finding-card__status--hypothesis { background: #fde8e6; color: var(--red); }

  /* Trace Section */
  .trace-section {
    margin-bottom: 10px;
  }
  .trace-section:last-child { margin: 0; }
  .trace-section small {
    display: block;
    font-size: 11px;
    font-weight: 700;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 6px;
  }
  .trace-item {
    display: flex;
    align-items: flex-start;
    gap: 6px;
    padding: 5px 0;
    border-bottom: 1px solid var(--line);
    font-size: 12px;
    line-height: 1.4;
  }
  .trace-item:last-child { border: 0; }
  .trace-item__status {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--muted);
    flex-shrink: 0;
    margin-top: 5px;
  }
  .trace-item__status--success, .trace-item__status--completed { background: var(--green); }
  .trace-item__status--warning, .trace-item__status--revised { background: var(--amber); }
  .trace-item__status--error, .trace-item__status--failed { background: var(--red); }
  .trace-item__status--started { background: var(--cobalt); }
  .trace-item__tool {
    font-weight: 600;
    color: var(--ink);
    white-space: nowrap;
    flex-shrink: 0;
  }
  .trace-item__summary {
    color: #555f59;
    flex: 1;
    min-width: 0;
  }
  .trace-item__duration {
    font-size: 10px;
    color: var(--muted);
    white-space: nowrap;
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
  }

  /* Inline Inputs (Refinement, Correction) */
  .inline-input {
    margin-top: 10px;
    padding: 12px;
    background: var(--surface);
    border-radius: 10px;
  }
  .inline-input textarea {
    display: block;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 13px;
    line-height: 1.5;
    resize: vertical;
    min-height: 40px;
    background: white;
    color: var(--ink);
    outline: 0;
  }
  .inline-input textarea:focus {
    border-color: var(--cobalt);
  }
  .inline-input__actions {
    display: flex;
    gap: 6px;
    margin-top: 8px;
  }

  .inline-success {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 12px 14px;
    background: #e4f1e9;
    border-radius: 10px;
    color: var(--green);
    font-size: 13px;
    font-weight: 600;
    margin-top: 10px;
  }

  /* Feedback Form */
  .feedback-form {
    margin-top: 10px;
    padding: 12px;
    background: var(--surface);
    border-radius: 10px;
  }
  .feedback-form__question {
    margin: 0 0 8px;
    font-size: 13px;
    font-weight: 600;
  }
  .feedback-form__buttons {
    display: flex;
    gap: 6px;
    margin-bottom: 8px;
  }
  .feedback-form__buttons button {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 7px 14px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: white;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
    color: var(--ink);
    transition: all 0.12s;
  }
  .feedback-form__buttons button:hover {
    border-color: var(--cobalt);
  }
  .feedback-btn--active {
    border-color: var(--cobalt) !important;
    background: #f0f4ff !important;
    color: var(--cobalt) !important;
  }
  .feedback-form textarea {
    display: block;
    width: 100%;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 13px;
    line-height: 1.5;
    resize: vertical;
    min-height: 36px;
    background: white;
    color: var(--ink);
    outline: 0;
    margin-bottom: 8px;
  }
  .feedback-form textarea:focus {
    border-color: var(--cobalt);
  }

  /* Input Area */
  .chat-input-area {
    flex-shrink: 0;
    padding: 12px 0 0;
    border-top: 1px solid var(--line);
  }
  .chat-input-wrapper {
    display: flex;
    align-items: flex-end;
    gap: 8px;
    background: white;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 8px;
    box-shadow: 0 4px 16px rgb(36 45 39 / 6%);
  }
  .chat-input-wrapper textarea {
    flex: 1;
    border: 0;
    outline: 0;
    resize: none;
    min-height: 24px;
    max-height: 200px;
    padding: 6px 8px;
    font-size: 14px;
    line-height: 1.5;
    background: transparent;
    color: var(--ink);
    font-family: inherit;
  }
  .chat-input-wrapper textarea::placeholder {
    color: var(--muted);
  }
  .chat-send-btn {
    display: grid;
    place-items: center;
    width: 40px;
    height: 40px;
    border: 0;
    border-radius: 10px;
    background: var(--accent);
    color: white;
    cursor: pointer;
    flex-shrink: 0;
    transition: all 0.15s;
  }
  .chat-send-btn:hover:not(:disabled) {
    background: var(--accent-dark);
    transform: translateY(-1px);
  }
  .chat-send-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .chat-input-hint {
    margin: 6px 0 0;
    font-size: 11px;
    color: var(--muted);
    text-align: center;
  }

  /* Graph Panel Overlay */
  .graph-overlay {
    position: fixed;
    inset: 0;
    z-index: 100;
    background: rgb(0 0 0 / 40%);
    backdrop-filter: blur(2px);
    display: flex;
    justify-content: flex-end;
    animation: overlay-fade 0.2s ease;
  }
  @keyframes overlay-fade {
    from { opacity: 0; }
    to { opacity: 1; }
  }
  .graph-panel {
    width: 60%;
    min-width: 400px;
    height: 100%;
    background: white;
    display: flex;
    flex-direction: column;
    box-shadow: -12px 0 40px rgb(0 0 0 / 15%);
    animation: panel-slide 0.25s ease;
  }
  @keyframes panel-slide {
    from { transform: translateX(100%); }
    to { transform: translateX(0); }
  }
  .graph-panel__header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 14px 20px;
    border-bottom: 1px solid var(--line);
    flex-shrink: 0;
  }
  .graph-panel__header h3 {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0;
    font-size: 16px;
    font-family: Georgia, serif;
  }
  .graph-panel__close {
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    border: 0;
    border-radius: 9px;
    background: var(--surface);
    cursor: pointer;
    color: var(--ink);
    transition: all 0.12s;
  }
  .graph-panel__close:hover {
    background: #fde8e6;
    color: var(--red);
  }
  .graph-panel__body {
    flex: 1;
    overflow: hidden;
    padding: 16px;
  }
  .graph-panel__empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    height: 100%;
    color: var(--muted);
    text-align: center;
  }
  .graph-panel__empty p {
    font-size: 14px;
  }

  /* Responsive */
  @media (max-width: 768px) {
    .graph-panel {
      width: 100%;
      min-width: 0;
    }
    .msg__bubble { max-width: 85%; }
    .msg--assistant .msg__content { max-width: 90%; }
    .chat-header h1 { font-size: 20px; }
    .chat-header p { font-size: 12px; }
    .chat-status { display: none; }
  }

  /* Spinner (global .spin class from app.css applies to child components) */
</style>
