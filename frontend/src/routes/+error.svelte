<script lang="ts">
  import { page } from '$app/state';
  import Button from '$lib/ui/Button.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Notice from '$lib/ui/Notice.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import type { IconName } from '$lib/ui/icons';

  let { error }: { error?: App.Error } = $props();

  // Статус перехода — признак для ветвления: он остаётся в коде и не выходит
  // в текст экрана.
  const status = $derived(page.status || 500);
  const pathname = $derived(page.url.pathname);
  const message = $derived((page.error?.message ?? error?.message ?? '').trim());

  // Причина, названная сервером (например, «Сервис не отвечает» из guard'а
  // сессии), — единственная, которую экран вправе показать: она формулируется
  // для читателя-аналитика. Технические тексты сборки и сообщения фреймворка
  // на русском не выглядят и на экран не попадают.
  const serverReason = $derived(/[А-Яа-яЁё]/.test(message) ? message : '');

  // «Сервис не отвечает» — про коды, которые выдаёт серверный контур: guard
  // сессии и прокси. 500 так себе не говорит: страницу мог уронить клиентский
  // рендер, и валить это на backend было бы неправдой.
  const serviceDown = $derived(status === 502 || status === 503 || status === 504);

  type Scene = {
    icon: IconName;
    label: string;
    title: string;
    problem: string;
    recovery: string;
    primary: { href: string; label: string };
  };

  // Ветви смысловые, а не косметические: у каждой свой диагноз и ровно одно
  // действие, которое его снимает.
  const scene = $derived.by<Scene>(() => {
    if (status === 401) {
      return {
        icon: 'key',
        label: 'нужен вход',
        title: 'Клубок вас не узнал',
        problem:
          'Вы вышли из аккаунта или не входили в него — поэтому рабочее пространство не открылось. Ничего не потеряно: находки, граф доказательств и история ваших запросов ждут за входом.',
        recovery: 'Войдите заново — сюда вернётесь тем же переходом.',
        primary: { href: `/login?next=${encodeURIComponent(pathname)}`, label: 'Войти' },
      };
    }
    if (status === 403) {
      return {
        icon: 'shield',
        label: 'нужен расширенный доступ',
        title: 'Действие недоступно в вашем доступе',
        problem:
          'Разбор предложений эволюции, аудит и закрытые данные открываются при расширенном доступе. Остальные разделы работают как обычно, и показания корпуса из-за этого отказа не пропали.',
        recovery: 'Вернитесь к разделам, которые читаются сейчас: находки и карта связей.',
        primary: { href: '/findings', label: 'Открыть находки' },
      };
    }
    if (status === 404) {
      return {
        icon: 'compass',
        label: 'адрес не найден',
        title: 'Такой страницы в Клубке нет',
        problem:
          'Ссылка не ведёт ни в один раздел — обычно это опечатка в адресе или устаревшая закладка. Находки, граф доказательств и рабочее пространство на месте.',
        recovery: 'Начните с панели состояния корпуса — там видно, что уже в базе.',
        primary: { href: '/dashboard', label: 'Панель состояния' },
      };
    }
    if (status === 503) {
      return {
        icon: 'sparkles',
        label: 'ответ не собран',
        title: 'Ответ не собрался',
        problem:
          'Запрос не дошёл до готового ответа — чаще всего это временная задержка. Формулировка вопроса и корпус не изменились, ничего заново вводить не нужно.',
        recovery: 'Повторите запрос; если ответ не соберётся и второй раз, начните с панели состояния корпуса.',
        primary: { href: '/research', label: 'Повторить запрос' },
      };
    }
    if (status === 502 || status === 504) {
      return {
        icon: 'alert',
        label: 'сервис не отвечает',
        title: 'Клубок не ответил',
        problem:
          'Переход не дошёл до рабочего контура: ответ не обработан и ничего в корпусе не изменилось. Введённое в формах сохранено на этой странице.',
        recovery: 'Повторите переход через минуту; если повторяется — начните с панели состояния корпуса.',
        primary: { href: '/dashboard', label: 'Панель состояния' },
      };
    }
    if (status >= 500) {
      return {
        icon: 'alert',
        label: 'сбой страницы',
        title: 'Страница не открылась',
        problem:
          'Ошибка произошла при открытии страницы — произойти это могло и на вашей стороне, поэтому про состояние сервиса здесь ничего не утверждается. Операцию этим переходом считайте невыполненной: перед повтором загляните в находки, если это был импорт или экспертная правка.',
        recovery: 'Повторите переход; если повторяется — начните с панели состояния корпуса.',
        primary: { href: '/dashboard', label: 'Панель состояния' },
      };
    }
    if (status >= 400) {
      return {
        icon: 'info',
        label: 'переход не принят',
        title: 'Переход не выполнен',
        problem:
          'Клубок не смог принять такой переход: обычно не хватает части адреса или он устарел после перестройки разделов. Данные при этом не менялись.',
        recovery: 'Повторите переход из рабочего пространства — там запрос формулируется заново.',
        primary: { href: '/research', label: 'Рабочее пространство' },
      };
    }
    return {
      icon: 'info',
      label: 'сбой экрана',
      title: 'Страница не открылась',
      problem:
        'При переходе произошла ошибка, о которой точнее сказать нечего. Ничего не сохранилось и не изменилось — попробуйте открыть раздел заново.',
      recovery: 'Повторите переход; если повторяется — начните с панели состояния корпуса.',
      primary: { href: '/dashboard', label: 'Панель состояния' },
    };
  });

  // Адрес полезен при опечатке и бесполезен во всех остальных случаях.
  const showsAddress = $derived(status === 404);
</script>

<svelte:head>
  <title>{scene.title} — Научный Клубок</title>
</svelte:head>

<div class="page fault page--cover">
  <div class="scene" aria-hidden="true">
    <span class="blob blob--coral" style="width:44vmax;height:44vmax;top:-20vmax;right:-14vmax"></span>
    <span class="blob blob--sage" style="width:38vmax;height:38vmax;top:12vmax;left:-16vmax"></span>
    <span class="blob blob--lav" style="width:36vmax;height:36vmax;bottom:-16vmax;right:-10vmax"></span>
  </div>

  <div class="wrap wrap--narrow fault__inner">
    <h1 class="display reveal">{scene.title}</h1>
    <p class="lead reveal" style="--reveal-delay: 90ms">{scene.problem}</p>

    {#if showsAddress}
      <Panel tone="sunk" class="reveal">
        <p class="small muted">Вы переходили по адресу <code class="code">{pathname}</code>.</p>
      </Panel>
    {/if}

    {#if serverReason}
      <Panel tone="sunk" class="reveal">
        <p class="small muted">Причина, названная сервисом: {serverReason}</p>
      </Panel>
    {/if}

    {#if serviceDown}
      <Notice tone="warn" title="Похоже, Клубок сейчас не отвечает">
        Страницы и запросы не доходят до конца. Дайте минуту и повторите переход: введённое в формах
        сохраняется, и сам повтор ничего в корпусе не меняет.
      </Notice>
    {/if}

    <Panel tone="sage" class="reveal">
      <div class="fault__fix">
        <p class="eyebrow"><Icon name="arrowRight" size={16} /> одно действие, которое чинит</p>
        <p class="small">{scene.recovery}</p>
        <div class="row fault__actions">
          <Button variant="action" href={scene.primary.href} iconEnd="arrowRight">
            {scene.primary.label}
          </Button>
          <Button variant="quiet" href="/">На витрину платформы</Button>
        </div>
      </div>
    </Panel>
  </div>
</div>

<style>
  .fault__inner {
    display: flex;
    flex-direction: column;
    gap: var(--s5);
  }

  .fault__inner .display {
    max-width: 18ch;
  }

  .fault__fix {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
  }

  .fault__actions {
    --gap: var(--s4);
    margin-top: var(--s1);
  }
</style>
