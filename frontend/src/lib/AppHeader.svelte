<script lang="ts">
  import { onMount } from 'svelte';
  import { AlertTriangle, Bell, ChevronDown, ArrowRight, LayoutDashboard, GitCompare, LoaderCircle, Search, ShieldCheck, MessageSquare, Network, Lightbulb } from '@lucide/svelte';
  import { page } from '$app/state';
  import { getRole, setRole, getUserId } from './roleStore.svelte';
  import { api } from './api';
  import { ROLE_LABELS, ROLE_DESCRIPTIONS, ROLE_PERMISSIONS, hasPermission, type Notification, type RoleId } from './types';

  let { compact = false }: { compact?: boolean } = $props();

  let roleOpen = $state(false);

  // Уведомления (FT-24)
  let notifications: Notification[] = $state([]);
  let notifOpen = $state(false);
  let notifLoading = $state(false);
  let notifError = $state('');
  let subscribed = $state(false);

  const currentRole = $derived(getRole());
  const currentUserId = $derived(getUserId());
  const roles: RoleId[] = ['researcher', 'analyst', 'project_manager', 'administrator', 'external_partner'];

  const canSeeDashboard = $derived(hasPermission(currentRole, 'audit:read'));
  const canSeeCompare = $derived(hasPermission(currentRole, 'export:run'));
  const canSeeFeedback = $derived(hasPermission(currentRole, 'feedback:give'));
  const canSeeGraph = $derived(hasPermission(currentRole, 'knowledge:read'));
  const canSeeFindings = $derived(hasPermission(currentRole, 'knowledge:read'));
  const rolePermissions = $derived(ROLE_PERMISSIONS[currentRole] ?? []);
  const roleDescription = $derived(ROLE_DESCRIPTIONS[currentRole] ?? '');
  let statusOk = $state(true);

  // Количество уведомлений
  const unreadCount = $derived(notifications.length);

  function selectRole(role: RoleId) {
    setRole(role);
    roleOpen = false;
  }

  // Тип уведомления по теме
  function notifType(topic: string): 'info' | 'warning' | 'success' {
    if (topic.includes('accepted') || topic.includes('success')) return 'success';
    if (topic.includes('superseded') || topic.includes('warning')) return 'warning';
    return 'info';
  }

  // Форматирование времени уведомления
  function formatNotifTime(ts: string): string {
    try {
      return new Date(ts).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
    } catch {
      return ts;
    }
  }

  // Загрузка уведомлений
  async function loadNotifications() {
    notifLoading = true; notifError = '';
    try {
      notifications = await api.notifications();
    } catch (reason) {
      notifError = reason instanceof Error ? reason.message : 'Ошибка загрузки';
    } finally {
      notifLoading = false;
    }
  }

  // Подписка на новые утверждения
  async function handleSubscribe() {
    try {
      await api.subscribe('new_claims');
      subscribed = true;
    } catch (reason) {
      notifError = reason instanceof Error ? reason.message : 'Не удалось подписаться';
    }
  }

  function toggleNotif() {
    notifOpen = !notifOpen;
    if (notifOpen && notifications.length === 0 && !notifLoading) {
      loadNotifications();
    }
  }

  onMount(() => {
    loadNotifications();
  });

  function handleClickOutside(event: MouseEvent) {
    const target = event.target as HTMLElement;
    if (!target.closest('.role-switcher')) {
      roleOpen = false;
    }
    if (!target.closest('.notif-bell')) {
      notifOpen = false;
    }
  }
</script>

<svelte:window onclick={handleClickOutside} />

<header class="site-header">
  <a class="skip-link" href="#main">Перейти к содержанию</a>
  <div class="site-header__inner">
    <a class="brand" href="/" aria-label="Научный Клубок — главная">
      <span class="brand__mark">НК</span>
      <span><strong>Научный Клубок</strong><small>проверяемые R&D-знания</small></span>
    </a>

    <nav class="site-nav" aria-label="Основная навигация">
      <a href="/research" class:active={page.url.pathname === '/research'}><Search size={16} /> Чат/Запросы</a>
      {#if canSeeGraph}
        <a href="/graph" class:active={page.url.pathname === '/graph'}><Network size={16} /> Граф знаний</a>
      {/if}
      {#if canSeeFindings}
        <a href="/findings" class:active={page.url.pathname === '/findings'}><Lightbulb size={16} /> Находки</a>
        <a href="/conflicts" class:active={page.url.pathname === '/conflicts'}><AlertTriangle size={16} /> Конфликты</a>
      {/if}
      {#if canSeeDashboard}
        <a href="/dashboard" class:active={page.url.pathname === '/dashboard'}><LayoutDashboard size={16} /> Дашборд</a>
      {/if}
      {#if canSeeFeedback}
        <a href="/feedback" class:active={page.url.pathname === '/feedback'}><MessageSquare size={16} /> Обратная связь</a>
      {/if}
      {#if !compact}
        <a href="/#how">Как это работает</a>
        <a href="/#trust">Доказательность</a>
      {/if}
    </nav>

    <div class="header-actions">
      <div class="notif-bell">
        <button
          class="notif-bell__btn"
          onclick={(e) => { e.stopPropagation(); toggleNotif(); }}
          aria-label="Уведомления"
          aria-expanded={notifOpen}
        >
          <Bell size={18} />
          {#if unreadCount > 0}
            <span class="notif-bell__badge">{unreadCount > 9 ? '9+' : unreadCount}</span>
          {/if}
        </button>
        {#if notifOpen}
          <div class="notif-panel" role="dialog" aria-label="Уведомления">
            <div class="notif-panel__header">
              <strong>Уведомления</strong>
              {#if unreadCount > 0}<span class="notif-panel__count">{unreadCount}</span>{/if}
            </div>
            {#if notifLoading}
              <div class="notif-panel__loading"><LoaderCircle class="spin" size={20} /> Загрузка…</div>
            {:else if notifError}
              <div class="notif-panel__error">{notifError}</div>
            {:else if notifications.length === 0}
              <div class="notif-panel__empty">Нет уведомлений</div>
            {:else}
              <div class="notif-panel__list">
                {#each notifications as n}
                  <div class="notif-item" data-type={n.type ?? notifType(n.topic)}>
                    <span class="notif-item__dot" data-type={n.type ?? notifType(n.topic)}></span>
                    <div class="notif-item__body">
                      <p>{n.message}</p>
                      <small>{formatNotifTime(n.created_at)}</small>
                    </div>
                  </div>
                {/each}
              </div>
            {/if}
            <div class="notif-panel__footer">
              <button
                class="button button--quiet button--small"
                onclick={handleSubscribe}
                disabled={subscribed}
              >
                {#if subscribed}Подписано{:else}Подписаться{/if}
              </button>
            </div>
          </div>
        {/if}
      </div>

      <div class="status-badge" class:ok={statusOk} title="Состояние системы">
        <span></span>
      </div>

      <div class="role-switcher">
        <button
          class="role-switcher__trigger"
          onclick={(e) => { e.stopPropagation(); roleOpen = !roleOpen; }}
          aria-expanded={roleOpen}
          aria-label="Сменить роль"
        >
          <ShieldCheck size={16} />
          <span class="role-switcher__label">{ROLE_LABELS[currentRole]}</span>
          <ChevronDown size={14} class={roleOpen ? 'rotated' : ''} />
        </button>
        {#if roleOpen}
          <div class="role-switcher__menu" role="listbox" aria-label="Выбор роли">
            <div class="role-switcher__user">
              <small>Пользователь</small>
              <code>{currentUserId}</code>
            </div>
            {#each roles as role}
              <button
                class="role-switcher__option"
                class:selected={role === currentRole}
                onclick={() => selectRole(role)}
                role="option"
                aria-selected={role === currentRole}
              >
                <span class="role-switcher__dot" data-role={role}></span>
                <span>
                  <strong>{ROLE_LABELS[role]}</strong>
                  <small class="role-switcher__desc">{ROLE_DESCRIPTIONS[role]}</small>
                </span>
                {#if role === currentRole}<span class="role-switcher__check">✓</span>{/if}
              </button>
            {/each}
            {#if rolePermissions.length > 0}
              <div class="role-switcher__perms">
                <small>Права ({ROLE_LABELS[currentRole]}):</small>
                <div class="role-switcher__perms-list">
                  {#each rolePermissions as perm}
                    <span class="role-perm-chip">{perm}</span>
                  {/each}
                </div>
              </div>
            {/if}
          </div>
        {/if}
      </div>

      {#if !compact}
        <a class="button button--primary button--small" href="/research">
          Начать исследование <ArrowRight size={17} />
        </a>
      {:else}
        <a class="button button--quiet button--small" href="/">О платформе</a>
      {/if}
    </div>
  </div>
</header>
