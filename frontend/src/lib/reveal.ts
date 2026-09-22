import { browser } from '$app/environment';

// Появление включается только когда движению ничего не мешает: без JS и при
// prefers-reduced-motion контент остаётся видимым (в app.css правило зависит от
// класса motion-ok на <html>).
export function initMotion(): void {
  if (!browser) return;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!reduced) document.documentElement.classList.add('motion-ok');
}

// Одна маршрутная область: наблюдатели видят содержимое текущего листа и
// снимаются, когда открывается следующий. Наблюдатель на весь документ
// переживал бы навигацию за навигацией и следил за узлами уже несуществующей
// страницы.
type Scope = { io: IntersectionObserver; mo: MutationObserver };

let scope: Scope | null = null;

const OPTIONS: IntersectionObserverInit = { rootMargin: '0px 0px -8% 0px', threshold: 0.08 };

function shown(entries: IntersectionObserverEntry[], io: IntersectionObserver): void {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    entry.target.classList.add('is-in');
    io.unobserve(entry.target);
  }
}

function scan(root: ParentNode, io: IntersectionObserver): void {
  root.querySelectorAll<HTMLElement>('.reveal:not(.is-in)').forEach((node) => io.observe(node));
}

function closeScope(): void {
  if (!scope) return;
  scope.io.disconnect();
  scope.mo.disconnect();
  scope = null;
}

function revealRoot(explicit?: ParentNode): ParentNode {
  if (explicit) return explicit;
  // Маршруты живут внутри <main id="main">: за ним, а не за всем документом.
  return document.getElementById('main') ?? document.body;
}

/**
 * Отмечает появлением все `.reveal` внутри маршрутной области. Возвращает
 * снятие наблюдателей; повторный вызов (новая навигация) закрывает прошлую
 * область, поэтому наблюдателей не накапливается.
 */
export function observeReveals(root?: ParentNode): () => void {
  if (!browser) return () => {};

  closeScope();

  const target = revealRoot(root);

  if (!document.documentElement.classList.contains('motion-ok')) {
    target.querySelectorAll<HTMLElement>('.reveal').forEach((node) => node.classList.add('is-in'));
    return () => {};
  }

  const io = new IntersectionObserver((entries) => shown(entries, io), OPTIONS);

  // Контент рабочих экранов приходит асинхронно: узел с .reveal, добавленный
  // после навигации, иначе навсегда остался бы невидимым.
  const mo = new MutationObserver((records) => {
    for (const record of records) {
      for (const added of record.addedNodes) {
        if (!(added instanceof HTMLElement)) continue;
        if (added.classList.contains('reveal')) io.observe(added);
        scan(added, io);
      }
    }
  });

  mo.observe(target, { childList: true, subtree: true });
  scan(target, io);
  scope = { io, mo };

  return () => {
    if (scope?.io === io) closeScope();
    else {
      io.disconnect();
      mo.disconnect();
    }
  };
}
