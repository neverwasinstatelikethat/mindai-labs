import { api, setUnauthorizedHandler, ApiError } from './api';
import type { AccountInfo, Capability } from './types';

export type SessionState = 'unknown' | 'anonymous' | 'authenticated';

let account = $state<AccountInfo | null>(null);
let state = $state<SessionState>('unknown');

// Проверка сессии при старте: httpOnly-cookie читает только сервер, поэтому
// клиент узнаёт о пользователе из /auth/me.
async function refresh(): Promise<void> {
  try {
    account = await api.me();
    state = 'authenticated';
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      account = null;
      state = 'anonymous';
      return;
    }
    // Сервис недоступен — состояние неизвестно, экраны показывают свою ошибку.
    account = null;
    state = 'unknown';
  }
}

setUnauthorizedHandler(() => {
  if (state !== 'anonymous') {
    account = null;
    state = 'anonymous';
  }
});

export const session = {
  get account(): AccountInfo | null {
    return account;
  },
  get state(): SessionState {
    return state;
  },
  get signedIn(): boolean {
    return state === 'authenticated' && account !== null;
  },
  get name(): string {
    return account?.display_name ?? '';
  },
  get initials(): string {
    const parts = (account?.display_name ?? '').trim().split(/\s+/).filter(Boolean);
    if (parts.length === 0) return account?.email.slice(0, 1).toUpperCase() ?? '·';
    return parts
      .slice(0, 2)
      .map((part) => part[0].toUpperCase())
      .join('');
  },
  get expert(): boolean {
    return account?.review_enabled ?? false;
  },
  // Права приходят с сервера: клиент ничего не домысливает про доступ.
  can(capability: Capability): boolean {
    return account?.capabilities.includes(capability) ?? false;
  },
  refresh,
  // Серверный guard уже получил /auth/me — передаём аккаунт клиенту,
  // чтобы не делать второй round trip на каждой навигации.
  hydrate(incoming: AccountInfo | null): void {
    account = incoming;
    state = incoming ? 'authenticated' : 'anonymous';
  },
  async login(email: string, password: string): Promise<AccountInfo> {
    account = await api.login({ email, password });
    state = 'authenticated';
    return account;
  },
  async register(input: { email: string; display_name: string; password: string }): Promise<AccountInfo> {
    account = await api.register(input);
    state = 'authenticated';
    return account;
  },
  async logout(): Promise<void> {
    try {
      await api.logout();
    } finally {
      account = null;
      state = 'anonymous';
    }
  },
};
