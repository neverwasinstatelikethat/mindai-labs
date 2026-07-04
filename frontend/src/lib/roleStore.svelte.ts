import type { RoleId } from './types';

const STORAGE_KEY = 'nk_role';
const STORAGE_USER_KEY = 'nk_user_id';

const VALID_ROLES: RoleId[] = [
  'researcher', 'analyst', 'project_manager', 'administrator', 'external_partner',
];

// SSR-safe defaults — no localStorage access at module init
let currentRole = $state<RoleId>('researcher');
let currentUserId = $state<string>('anonymous');
let initialized = false;

/** Вызов из onMount в layout — инициализация из localStorage на клиенте */
export function initRoleStore(): void {
  if (initialized || typeof localStorage === 'undefined') return;
  initialized = true;

  const storedRole = localStorage.getItem(STORAGE_KEY);
  if (storedRole && VALID_ROLES.includes(storedRole as RoleId)) {
    currentRole = storedRole as RoleId;
  }

  const storedUserId = localStorage.getItem(STORAGE_USER_KEY);
  if (storedUserId) {
    currentUserId = storedUserId;
  } else {
    const generated = `user_${Math.random().toString(36).slice(2, 10)}`;
    localStorage.setItem(STORAGE_USER_KEY, generated);
    currentUserId = generated;
  }
}

export function getRole(): RoleId {
  return currentRole;
}

export function setRole(role: RoleId): void {
  currentRole = role;
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem(STORAGE_KEY, role);
  }
}

export function getUserId(): string {
  return currentUserId;
}

export function setUserId(id: string): void {
  currentUserId = id;
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem(STORAGE_USER_KEY, id);
  }
}

export function getRoleHeaders(): Record<string, string> {
  return {
    'X-User-Role': currentRole,
    'X-User-Id': currentUserId,
  };
}

export const roleState = {
  get role() { return currentRole; },
  get userId() { return currentUserId; },
};
