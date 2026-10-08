<script lang="ts">
  import { page } from '$app/state';
  import { navLabel } from '$lib/nav';
  import Button from '$lib/ui/Button.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import type { IconName } from '$lib/ui/icons';

  // Статус перехода — признак для ветвления: он остаётся в коде и не выходит
  // в текст экрана.
  const status = $derived(page.status || 500);
  // Адрес возврата держит и путь, и запрос к нему: страница, на которой случился
  // сбой, открывается ровно такой, какой была.
  const selfHref = $derived(`${page.url.pathname}${page.url.search}`);

  type Action = { kind: 'back' } | { kind: 'href'; href: string; label: string };

  type Scene = {
    icon: IconName;
    /** Состояние задачи — одним словом, до всякого пояснения. */
    label: string;
    title: string;
    /** Одна фраза о том, что произошло. Не абзац. */
    problem: string;
    /** Ровно одно действие: назад или в раздел. */
    action: Action;
  };

  const back: Action = { kind: 'back' };
  const toResearch: Action = { kind: 'href', href: '/research', label: `Открыть «${navLabel('/research')}»` };

  // Ветви смысловые, а не косметические: у каждой своё состояние задачи и
  // ровно одно действие, которое его снимает.
  const scene = $derived.by<Scene>(() => {
    if (status === 401) {
      return {
        icon: 'key',
        label: 'нужен вход',
        title: 'Войдите в StormIdea',
        problem: 'Рабочее пространство открыто только по входу: находки, карта связей и история ваших запросов ждут за ним.',
        action: { kind: 'href', href: `/login?next=${encodeURIComponent(selfHref)}`, label: 'Войти' },
      };
    }
    if (status === 403) {
      return {
        icon: 'shield',
        label: 'право не выдано',
        title: 'Это действие вашему аккаунту не открыто',
        problem: 'Решения по предложениям и закрытые данные относятся к экспертным правам.',
        action: back,
      };
    }
    if (status === 404) {
      return {
        icon: 'compass',
        label: 'адрес не найден',
        title: 'Такой страницы в StormIdea нет',
        problem: 'Ссылка не ведёт ни в один раздел: обычно это опечатка или устаревшая закладка.',
        action: toResearch,
      };
    }
    if (status === 503) {
      return {
        icon: 'sparkles',
        label: 'ответ не собран',
        title: 'Сервис не смог ответить',
        problem: 'Не удалось подготовить ответ. Попробуйте задать вопрос ещё раз чуть позже.',
        action: toResearch,
      };
    }
    if (status === 502 || status === 504) {
      return {
        icon: 'alert',
        label: 'сервис не отвечает',
        title: 'StormIdea не ответил',
        problem: 'Переход не дошёл до рабочего контура, данные при этом не менялись.',
        action: back,
      };
    }
    if (status >= 500) {
      return {
        icon: 'alert',
        label: 'сбой страницы',
        title: 'Страница не открылась',
        problem: 'Ошибка могла затронуть только этот переход: повторите его, ничего не потеряется.',
        action: back,
      };
    }
    if (status >= 400) {
      return {
        icon: 'info',
        label: 'переход не принят',
        title: 'Переход не выполнен',
        problem: 'StormIdea не открыл этот адрес: проверьте ссылку или вернитесь в чат.',
        action: toResearch,
      };
    }
    return {
      icon: 'info',
      label: 'сбой экрана',
      title: 'Страница не открылась',
      problem: 'При переходе произошла ошибка, о которой точнее сказать нечего.',
      action: back,
    };
  });

  function goBack(): void {
    // Если истории нет (прямая загрузка ошибки), выход — в рабочий раздел.
    if (window.history.length > 1) window.history.back();
    else window.location.assign('/research');
  }
</script>

<svelte:head>
  <title>StormIdea: {scene.title}</title>
</svelte:head>

<div class="page fault page--cover">
  <div class="scene" aria-hidden="true">
    <span class="blob blob--coral" style="width:44vmax;height:44vmax;top:-20vmax;right:-14vmax"></span>
    <span class="blob blob--sage" style="width:38vmax;height:38vmax;bottom:-16vmax;left:-14vmax"></span>
  </div>

  <div class="wrap wrap--narrow fault__inner">
    <!-- Состояние называется словом до того, как человек прочтёт пояснение. -->
    <p class="micro fault__label reveal">
      <Icon name={scene.icon} size={14} />
      {scene.label}
    </p>
    <h1 class="display reveal">{scene.title}</h1>
    <p class="lead reveal" style="--reveal-delay: 90ms">{scene.problem}</p>

    <div class="row fault__actions reveal" style="--reveal-delay: 140ms">
      {#if scene.action.kind === 'back'}
        <Button variant="action" onclick={goBack}>Вернуться назад</Button>
      {:else}
        <Button variant="action" href={scene.action.href}>{scene.action.label}</Button>
      {/if}
    </div>
  </div>
</div>

<style>
  .fault__inner {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
  }

  .fault__inner .display {
    max-width: 18ch;
  }

  /* Действие одно, и оно стоит рядом с пояснением, а не под ним. */
  .fault__actions {
    --gap: var(--s4);
    align-items: center;
    margin-top: var(--s2);
  }
</style>
