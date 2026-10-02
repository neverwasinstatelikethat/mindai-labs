<script lang="ts">
  import { page } from '$app/state';
  import { navLabel } from '$lib/nav';
  import Button from '$lib/ui/Button.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import type { IconName } from '$lib/ui/icons';

  let { error }: { error?: App.Error } = $props();

  // Статус перехода — признак для ветвления: он остаётся в коде и не выходит
  // в текст экрана.
  const status = $derived(page.status || 500);
  const pathname = $derived(page.url.pathname);
  // Адрес возврата держит и путь, и запрос к нему: страница, на которой случился
  // сбой, открывается ровно такой, какой была.
  const selfHref = $derived(`${page.url.pathname}${page.url.search}`);
  const message = $derived((page.error?.message ?? error?.message ?? '').trim());

  // Причина, названная сервером (например, «Сервис не отвечает» из guard'а
  // сессии), единственная, которую экран вправе показать: она формулируется
  // для читателя-аналитика. Технические тексты сборки и сообщения фреймворка
  // на русском не выглядят и на экран не попадают.
  const serverReason = $derived(/[А-Яа-яЁё]/.test(message) ? message : '');

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
          'Вы вышли из аккаунта или не входили в него: без входа рабочее пространство не открывается. Находки, карта связей и история ваших запросов ждут за входом.',
        recovery: 'Войдите заново, и вы вернётесь на эту же страницу.',
        primary: { href: `/login?next=${encodeURIComponent(selfHref)}`, label: 'Войти' },
      };
    }
    if (status === 403) {
      return {
        icon: 'shield',
        label: 'нужно экспертное право',
        title: 'Вашему аккаунту это действие не открыто',
        problem:
          'Экспертное право на разбор предложений по ответам, журнал действий и закрытые данные выдаёт администратор сервиса. Остальные разделы работают как обычно, показания корпуса этот отказ не тронул.',
        recovery: 'Вернитесь к разделам, которые открыты сейчас: находки и карта связей.',
        primary: { href: '/findings', label: `Открыть «${navLabel('/findings')}»` },
      };
    }
    if (status === 404) {
      return {
        icon: 'compass',
        label: 'адрес не найден',
        title: 'Такой страницы в Клубке нет',
        problem:
          'Ссылка не ведёт ни в один раздел: обычно это опечатка в адресе или устаревшая закладка. Находки, карта связей и запросы к корпусу на месте.',
        // Витрина открыта всем и не требует входа, поэтому она и есть выход:
        // раздел рабочего пространства без сессии привёл бы сюда же снова.
        recovery: 'Начните с витрины: там видно, чем занят сервис, а вход стоит рядом.',
        primary: { href: '/', label: 'Вернуться на витрину' },
      };
    }
    if (status === 503) {
      return {
        icon: 'sparkles',
        label: 'ответ не собран',
        title: 'Сервис не смог ответить',
        problem:
          'Запрос дошёл до сервиса, но готового ответа нет: модель может быть недоступна или контур занят. Ничего в корпусе не изменилось.',
        recovery:
          'Наберите вопрос заново через минуту. Если ответа нет и второй раз, обратитесь к администратору сервиса: он видит состояние модели.',
        primary: { href: '/research', label: `Открыть «${navLabel('/research')}»` },
      };
    }
    if (status === 502 || status === 504) {
      return {
        icon: 'alert',
        label: 'сервис не отвечает',
        title: 'Клубок не ответил',
        problem:
          'Переход не дошёл до рабочего контура: ответ не обработан, и ничего в корпусе не изменилось.',
        recovery:
          'Повторите переход через минуту. Если повторяется, обратитесь к администратору сервиса.',
        primary: { href: selfHref, label: 'Повторить переход' },
      };
    }
    if (status >= 500) {
      return {
        icon: 'alert',
        label: 'сбой страницы',
        title: 'Страница не открылась',
        problem:
          'Ошибка произошла при открытии страницы и могла затронуть только её. Операцию этим переходом считайте невыполненной: перед повтором загляните в находки, если это был импорт или экспертная правка.',
        recovery: `Повторите переход. Если повторяется, начните с раздела «${navLabel('/dashboard')}»: там видно, что уже в корпусе.`,
        primary: { href: selfHref, label: 'Повторить переход' },
      };
    }
    if (status >= 400) {
      return {
        icon: 'info',
        label: 'переход не принят',
        title: 'Переход не выполнен',
        problem:
          'Клубок не принял такой переход: обычно не хватает части адреса или адрес устарел после перестройки разделов. Данные при этом не менялись.',
        recovery: 'Повторите переход из рабочего пространства, там вопрос формулируется заново.',
        primary: { href: '/research', label: `Открыть «${navLabel('/research')}»` },
      };
    }
    return {
      icon: 'info',
      label: 'сбой экрана',
      title: 'Страница не открылась',
      problem:
        'При переходе произошла ошибка, о которой точнее сказать нечего. Ничего в корпусе не изменилось: попробуйте открыть раздел заново.',
      recovery: `Повторите переход. Если повторяется, начните с раздела «${navLabel('/dashboard')}»: там видно, что уже в корпусе.`,
      primary: { href: selfHref, label: 'Повторить переход' },
    };
  });

  // Адрес полезен при опечатке и бесполезен во всех остальных случаях.
  const showsAddress = $derived(status === 404);
</script>

<svelte:head>
  <title>Научный Клубок: {scene.title}</title>
</svelte:head>

<div class="page fault page--cover">
  <div class="scene" aria-hidden="true">
    <span class="blob blob--coral" style="width:44vmax;height:44vmax;top:-20vmax;right:-14vmax"></span>
    <span class="blob blob--sage" style="width:38vmax;height:38vmax;top:12vmax;left:-16vmax"></span>
    <span class="blob blob--lav" style="width:36vmax;height:36vmax;bottom:-16vmax;right:-10vmax"></span>
  </div>

  <div class="wrap wrap--narrow fault__inner">
    <!-- Состояние называется словом до того, как человек прочитает пояснение. -->
    <span class="chip chip--static reveal">
      <Icon name={scene.icon} size={14} />
      {scene.label}
    </span>
    <h1 class="display reveal">{scene.title}</h1>
    <p class="lead reveal" style="--reveal-delay: 90ms">{scene.problem}</p>

    {#if showsAddress}
      <Panel tone="sunk" class="reveal">
        <p class="small muted">Вы переходили по адресу <code class="code">{pathname}</code>.</p>
      </Panel>
    {/if}

    <Panel tone="sage" class="reveal">
      <div class="fault__fix">
        <p class="eyebrow"><Icon name="arrowRight" size={16} /> что сделать сейчас</p>
        <p class="small">{scene.recovery}</p>
        <!-- Ровно одно действие: второй ссылкой экран уводил бы человека в
             раздумья, а не из состояния. -->
        <div class="row fault__actions">
          <Button variant="action" href={scene.primary.href} iconEnd="arrowRight">
            {scene.primary.label}
          </Button>
        </div>
      </div>
    </Panel>

    <!-- Код перехода и то, что назвал сервер, нужны администратору сервиса, а не
         человеку на этой странице: они лежат свёрнутыми под «Служебные данные». -->
    <details class="fault__tech reveal">
      <summary class="micro">Служебные данные</summary>
      <div class="stack fault__tech__body">
        <p class="micro muted">Код ответа: {status}.</p>
        {#if serverReason}
          <p class="micro muted">Сервис назвал причину так: {serverReason}</p>
        {:else}
          <p class="micro muted">Сервис причину не назвал.</p>
        {/if}
        <p class="micro muted">
          Эти строки передают администратору сервиса, когда переход не открывается повторно.
        </p>
      </div>
    </details>
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

  /* Служебные данные свёрнуты по умолчанию: код перехода и текст сервера нужны
     администратору сервиса, а человеку на экране важно только состояние. */
  .fault__tech {
    align-self: flex-start;
    max-width: var(--maxw-measure);
  }

  .fault__tech summary {
    cursor: pointer;
    color: var(--ink-3);
  }

  .fault__tech summary:hover {
    color: var(--ink);
  }

  .fault__tech__body {
    --gap: var(--s1);
    margin-top: var(--s2);
    padding-inline-start: var(--s3);
    border-inline-start: 1px solid var(--line);
  }
</style>
