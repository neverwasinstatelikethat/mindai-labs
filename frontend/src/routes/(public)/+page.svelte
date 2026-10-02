<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { api } from '$lib/api';
  import { NAV_LINKS, navLabel } from '$lib/nav';
  import { num } from '$lib/format';
  import {
    PROPERTY_LABELS,
    RELATION_LABELS,
    SCOPE_KEY_LABELS,
    SCOPE_VALUE_LABELS,
    STATUS_PHRASE,
    termOf,
  } from '$lib/terms';
  import type { CorpusStats, SystemStatus } from '$lib/types';
  import Button from '$lib/ui/Button.svelte';
  import Empty from '$lib/ui/Empty.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import type { IconName } from '$lib/ui/icons';
  import Mascot from '$lib/ui/Mascot.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import PromptInput from '$lib/ui/PromptInput.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';
  import StatusPill from '$lib/ui/StatusPill.svelte';
  import TickerFlow from '$lib/ui/TickerFlow.svelte';

  // Витрина читает только то, что доступно без входа: агрегированные показания
  // корпуса и отметку о состоянии сервиса. Ни одного числа здесь нет до ответа
  // сервера, и ничего кроме этих двух обращений страница не делает.
  type ServiceStatus = 'consensus' | 'hypothesis' | 'off';
  type ServiceView = { status: ServiceStatus; label: string; note: string };

  // Четыре свойства одного тезиса: то, чего соседний поиск по фрагментам не
  // повторяет, потому что адрес, условия, расхождение и версии держатся в одной
  // записи. `artifact` выбирает, какой фрагмент трассы показан под свойством:
  // артефакт всегда принадлежит тезису, а не является отдельным блоком.
  const FACETS: {
    icon: IconName;
    name: string;
    text: string;
    artifact?: 'versus' | 'versions';
  }[] = [
    {
      icon: 'pin',
      name: 'Адрес в источнике',
      text: 'Цитата указывает место в документе: страницу, лист, диапазон ячеек или точное место в тексте. По адресу число проверяют в первоисточнике.',
    },
    {
      icon: 'scale',
      name: 'Число с условиями',
      text: 'Свойство, оператор, значение или диапазон, единица измерения и пересчёт. Условия применения остаются частью утверждения и читаются под цитатой.',
    },
    {
      icon: 'conflict',
      name: 'Расхождение видно',
      text: 'Если источники дают непересекающиеся значения при одних и тех же условиях, оба значения остаются в ответе и ждут разбора эксперта.',
      artifact: 'versus',
    },
    {
      icon: 'layers',
      name: 'Версия за версией',
      text: 'Экспертная правка создаёт новую версию утверждения со связкой «заменяет», история остаётся. Модель не записывает своё утверждение в корпус сама.',
      artifact: 'versions',
    },
  ];

  // Пара значений одного свойства из разных источников: так выглядит кандидат
  // расхождения. Значения взяты из примеров начального наполнения корпуса
  // (`origin=demo` в seed-контуре), а не выдуманы для красоты.
  //
  // Живой ответ на зафиксированный вопрос сервис отдаёт только по сессии
  // (`/api/v1/demo` требует входа), поэтому витрина не притворяется прогоном:
  // здесь стоит снятый снимок с честной подписью, а настоящую сборку делает
  // ссылка `demoHref` под ним.
  const TRACE_PAIR: { at: string; from: string; page: string; left: string; width: string; tone: 'consensus' | 'disputed' }[] = [
    {
      at: '95–99 %',
      from: 'Обзор методов обессоливания шахтных вод',
      page: 'с. 14',
      left: '95%',
      width: '4%',
      tone: 'consensus',
    },
    {
      at: '80–85 %',
      from: 'Пилот обратного осмоса',
      page: 'с. 5',
      left: '80%',
      width: '5%',
      tone: 'disputed',
    },
  ];

  // Три остановки маршрута аналитика: остальные разделы перечислены в подвале.
  // Названия берутся из `nav.ts`, того же списка, что и навигация после входа:
  // витрина не должна учить одному языку, а рабочий интерфейс называть иначе.
  const STOPS: {
    icon: IconName;
    href: string;
    text: string;
    more: string[];
  }[] = [
    {
      icon: 'ask',
      href: '/research',
      text: 'Спросить про число, технологию или условие и получить ответ, где каждый тезис раскрывается до цитаты и адреса в документе. Сюда же попадают импорт документа и выгрузка ответа.',
      more: ['/graph'],
    },
    {
      icon: 'search',
      href: '/findings',
      text: 'Находки корпуса: у каждой число, условия применения и доказательство. Рядом сравнение технологий по измерениям и перечень того, где источники расходятся.',
      more: ['/compare', '/conflicts'],
    },
    {
      icon: 'shield',
      href: '/feedback',
      text: 'Подтвердить, отклонить или попросить правку по ответу. Решение остаётся за человеком и создаёт новую версию утверждения.',
      more: ['/dashboard'],
    },
  ];

  // Границы названы списком без карточек: то, чего продукт не делает, должно
  // читаться как перечень фактов, а не как витрина возможностей.
  const LIMITS: { title: string; text: string }[] = [
    {
      title: 'За источник не додумывает',
      text: 'Ответ собирается из того, что документы сказали в корпусе. Если находки нет, сервис называет пробел словами и не подставляет правдоподобный пересказ.',
    },
    {
      title: 'Не отвечает числом без адреса',
      text: 'Тезис без места в источнике в ответ не попадает: к нему идут цитата и адрес, страница, лист, диапазон ячеек или точное место в тексте.',
    },
    {
      title: 'Расхождение не сглаживает',
      text: 'Там, где источники не сходятся, ответ не усредняется: оба значения остаются видимыми и ждут решения человека.',
    },
    {
      title: 'Ответ может быть неполным',
      text: 'У запроса есть срок и предел шагов. Если чего-то не хватило, ответ говорит об этом словами.',
    },
    {
      title: 'Никуда не стучится',
      text: 'О принятом решении, о новой версии утверждения и о разобранном предложении остаётся запись в журнале действий. Писем и push-уведомлений нет: проверять результат нужно самому.',
    },
    {
      title: 'Одна предметная область',
      text: 'Корпус и разбор настроены под горно-металлургическую литературу. Обещаний «работает на любых документах» на этой странице не найдёте.',
    },
  ];

  // Три тихие метрики витрины — ровно те поля, что отдаёт статистика корпуса.
  // Имена держатся словаря продукта: единица корпуса это находка, её текст
  // это утверждение, высказывание в ответе это тезис.
  const METRICS: { key: 'documents' | 'claims' | 'entities'; label: string }[] = [
    { key: 'documents', label: 'документов в корпусе' },
    { key: 'claims', label: 'находок в корпусе' },
    { key: 'entities', label: 'сущностей в карте связей' },
  ];

  // Вопрос витрины зафиксирован тем же текстом, что держит сервис: ссылка уводит
  // не в картинку, а в настоящий запрос, который собирается после входа.
  const DEMO_QUESTION =
    'Какие методы обессоливания подходят для шахтной воды с сульфатами и хлоридами 200–300 мг/л при сухом остатке ≤1000 мг/л?';
  const demoHref = `/research?q=${encodeURIComponent(DEMO_QUESTION)}`;

  let stats = $state<CorpusStats | null>(null);
  let statsLoading = $state(true);
  let statsFailed = $state(false);
  let service = $state<SystemStatus | null>(null);
  let serviceLoading = $state(true);
  let serviceFailed = $state(false);

  // Черновик вопроса живёт на витрине ровно до отправки: текст уходит вместе с
  // человеком в рабочее пространство параметром адреса.
  let draft = $state('');
  let sending = $state(false);

  async function ask(text: string): Promise<void> {
    const question = text.trim();
    if (!question) return;
    sending = true;
    try {
      // Логики доступа здесь нет: без входа на этом пути разбирается guard
      // рабочего пространства, а не витрина.
      await goto(`/research?q=${encodeURIComponent(question)}`);
    } finally {
      sending = false;
    }
  }

  async function load(): Promise<void> {
    statsLoading = true;
    serviceLoading = true;
    statsFailed = false;
    serviceFailed = false;
    const [statsResult, serviceResult] = await Promise.allSettled([api.corpusStats(), api.status()]);
    if (statsResult.status === 'fulfilled') {
      stats = statsResult.value;
    } else {
      stats = null;
      statsFailed = true;
    }
    statsLoading = false;
    if (serviceResult.status === 'fulfilled') {
      service = serviceResult.value;
    } else {
      service = null;
      serviceFailed = true;
    }
    serviceLoading = false;
  }

  onMount(() => {
    void load();
  });

  const corpusEmpty = $derived(
    !!stats && stats.documents === 0 && stats.claims === 0 && stats.entities === 0,
  );

  const serviceView = $derived.by((): ServiceView => {
    if (serviceLoading) {
      return { status: 'off', label: 'Считываем показания корпуса', note: 'показания появятся, когда сервис ответит' };
    }
    if (serviceFailed || !service) {
      return { status: 'off', label: 'Показания не пришли', note: 'повторите запрос' };
    }
    return service.status === 'ready'
      ? {
          status: 'consensus',
          label: 'Сервис отвечает',
          note: 'открыты корпус, карта связей и запросы',
        }
      : {
          status: 'hypothesis',
          label: 'Отвечает не всё',
          note: 'часть запросов не собирает ответ, это видно по результату',
        };
  });

  // Упрощённый контур держит аккаунты и историю только до перезапуска сервиса.
  // Витрина называет это прямо, иначе обещание «история останется вашей»
  // оказывается ложью ровно в момент регистрации.
  const volatileStorage = $derived(
    !!service && (service.accounts === 'in-memory' || service.state_backend === 'in-memory'),
  );
</script>

<svelte:head>
  <title>Научный Клубок: проверяемые ответы по горно-металлургическим источникам</title>
  <meta
    name="description"
    content="Сервис проверяемых ответов для горной металлургии: каждое число раскрывается до цитаты, страницы, листа и диапазона ячеек первоисточника, условия применения остаются в записи, расхождения и пробелы источников видны в ответе."
  />
</svelte:head>

<div class="page landing">
  <!-- ── 1. Первый экран: питч, композер с маскотом, показания корпуса ──── -->
  <section class="hero">
    <div class="scene" aria-hidden="true">
      <span class="blob blob--coral" style="width:54vmax;height:54vmax;top:-24vmax;right:-16vmax"></span>
      <span class="blob blob--lav" style="width:40vmax;height:40vmax;bottom:-18vmax;left:-14vmax"></span>
    </div>

    <div class="wrap hero__inner">
      <div class="hero__pitch">
        <h1 class="display hero__title reveal" style="--reveal-delay: 60ms">
          Каждое число ведёт к <span class="hand hero__hand">источнику</span>
        </h1>
        <p class="lead reveal" style="--reveal-delay: 120ms">
          Клубок разбирает научно-технические документы на находки с адресом в источнике.
          Число держится вместе с единицей и условиями применения, расхождение источников видно
          уже в ответе. Это рабочий инструмент аналитика.
        </p>

        <div class="hero__ask reveal" style="--reveal-delay: 180ms">
          <PromptInput
            bind:value={draft}
            variant="hero"
            name="hero"
            busy={sending}
            placeholder="Что найти в корпусе: число, технология, источник"
            hint="Enter отправляет вопрос, Shift + Enter переносит строку"
            onsubmit={(text) => void ask(text)}
          />
        </div>
        <p class="micro muted hero__asknote reveal" style="--reveal-delay: 220ms">
          Вопрос открывается в рабочем пространстве, где по нему собирают ответ с цитатами и
          адресами. Аккаунт нужен с первого вопроса, регистрация открытая.
        </p>

        <a class="hero__cue micro reveal" style="--reveal-delay: 260ms" href="#trace">
          <Icon name="chevronDown" size={16} />
          что именно продукт доказывает
        </a>
      </div>

      <aside class="hero__aside reveal" style="--reveal-delay: 240ms">
        <p class="micro muted">показания корпуса</p>
        {#if statsLoading}
          <dl class="metrics" aria-busy="true">
            {#each METRICS as metric (metric.key)}
              <div class="metric">
                <dt class="micro muted">{metric.label}</dt>
                <dd><span class="skeleton metric__sk"></span></dd>
              </div>
            {/each}
          </dl>
          <p class="micro muted">считаем корпус, чисел здесь пока нет</p>
        {:else if statsFailed || !stats}
          <Empty
            icon="alert"
            title="Показания корпуса не пришли"
            body="Состав корпуса прочитать не удалось. Проверьте соединение и повторите запрос: витрина ничего не хранит и приближённые числа не показывает."
          >
            {#snippet action()}
              <Button size="sm" variant="quiet" icon="refresh" onclick={() => void load()}>
                Повторить запрос
              </Button>
            {/snippet}
          </Empty>
        {:else if corpusEmpty}
          <Empty
            icon="doc"
            title="В корпусе пока пусто"
            body="Здесь появятся документы, находки с адресом в источнике и сущности карты связей. Полный состав корпуса показывает раздел «{navLabel('/dashboard')}», он доступен после входа."
          >
            {#snippet action()}
              <!-- Кнопка ведёт во вход, а не в «Качество»: называть её разделом
                   значило бы обещать переход, которого не будет. -->
              <Button size="sm" variant="quiet" href="/login">Войти в рабочее пространство</Button>
            {/snippet}
          </Empty>
        {:else}
          <dl class="metrics">
            {#each METRICS as metric, i (metric.key)}
              <div class="metric reveal" style="--reveal-delay: {i * 80}ms">
                <dt class="micro muted">{metric.label}</dt>
                <dd class="metric__num">{num(stats[metric.key])}</dd>
              </div>
            {/each}
          </dl>
          <p class="micro muted">
            Эти три числа считает корпус; показателей, за которыми не стоит реальный подсчёт,
            на странице нет.
          </p>
        {/if}
        {#if volatileStorage}
          <p class="micro muted hero__aside-note">
            Сервис работает без постоянного хранилища: аккаунты и история ответов обнуляются при
            его перезапуске. Постоянный контур настраивает администратор.
          </p>
        {/if}
      </aside>
    </div>
  </section>

  <!-- ── 2. Трасса одного тезиса: единственный сильный жест витрины ─────── -->
  <section class="wrap section" id="trace">
    <SectionHead
      title="Что стоит за одним ответом"
      lead="Один тезис и всё, что за ним: адрес в документе, число с условиями, видимое расхождение и история версий. Эти четыре свойства отличают такой ответ от поиска по фрагментам."
    />

    <div class="trace">
      <Panel tone="sage">
      <div class="trace__head">
        <span class="chip chip--static"><Icon name="info" size={14} /> учебный пример</span>
        <p class="micro muted trace__note">
          Так выглядит ответ: цитата, адрес в источнике, число с условиями и его статус. Значения
          взяты из примеров начального наполнения корпуса, их цитату нельзя проверить по
          настоящему документу. Снимок не меняется от открытия к открытию, живой ответ
          собирается после входа.
        </p>
        <a class="trace__ask micro" href={demoHref}>
          Открыть этот вопрос в рабочем пространстве
          <Icon name="arrowRight" size={14} />
        </a>
      </div>

      <div class="trace__body">
        <figure class="thesis">
          <blockquote class="quote thesis__quote">
            «Задержание растворённых солей мембраной обратного осмоса составляет 95–99%.»
          </blockquote>
          <figcaption class="stack thesis__cap">
            <p class="locator">
              <Icon name="pin" size={14} /> «Обзор методов обессоливания шахтных вод», страница 14
            </p>
            <p class="micro muted">
              У таблицы к адресу добавляются лист и диапазон ячеек.
            </p>
            <div class="measure">
              <p class="micro muted">свойство: отторжение растворённых солей</p>
              <p class="measure__value">95–99 <span class="measure__unit">%</span></p>
              <p class="micro muted">
                условие применения: шахтная вода, единица измерения и пересчёт в записи
              </p>
              <StatusPill status="consensus" label="источники согласуются" />
            </div>
          </figcaption>
        </figure>

        <ol class="facets">
          {#each FACETS as facet, i (facet.name)}
            <li class="facet reveal" style="--reveal-delay: {i * 70}ms">
              <span class="facet__mark"><Icon name={facet.icon} size={20} /></span>
              <div class="grow facet__body">
                <h3 class="h4">{facet.name}</h3>
                <p class="small muted">{facet.text}</p>

                {#if facet.artifact === 'versus'}
                  <div class="versus">
                    {#each TRACE_PAIR as row (row.from)}
                      <div class="versus__row">
                        <div class="versus__line">
                          <p class="micro muted">«{row.from}», {row.page}</p>
                          <p class="versus__value">{row.at}</p>
                        </div>
                        <div class="bar">
                          <span
                            class="bar__fill {row.tone === 'consensus' ? 'bar__fill--consensus' : 'bar__fill--disputed'}"
                            style="left: {row.left}; width: {row.width}"></span>
                        </div>
                      </div>
                    {/each}
                    <p class="micro muted versus__scale">
                      общая шкала: 0–100 % отторжения солей
                    </p>
                    <p class="micro muted">
                      Один субъект, одно свойство, одни единицы, совместимые условия применения и
                      непересекающиеся диапазоны: такая пара становится кандидатом расхождения.
                    </p>
                  </div>
                {:else if facet.artifact === 'versions'}
                  <p class="chain micro">
                    <span class="chain__node">версия 3</span>
                    <span class="chain__note">заменяется</span>
                    <span class="chain__node">версией 4</span>
                    <span class="chain__note">история остаётся</span>
                  </p>
                {/if}
              </div>
            </li>
          {/each}
        </ol>
      </div>
      </Panel>
    </div>
  </section>

  <!-- ── 3. Маршрут аналитика: три остановки в реальных разделах ────────── -->
  <section class="wrap section" id="route">
    <SectionHead
      title="Как выглядит работа с корпусом"
      lead="Три остановки одного маршрута: спросить, проверить число, оставить отзыв. Каждая ведёт в настоящий раздел."
    />

    <ol class="stops">
      {#each STOPS as stop, i (stop.href)}
        <li class="stop reveal" style="--reveal-delay: {i * 80}ms">
          <div class="stop__head">
            <span class="stop__num" aria-hidden="true">{i + 1}</span>
            <h3 class="h3">{navLabel(stop.href)}</h3>
            <span class="stop__icon" aria-hidden="true"><Icon name={stop.icon} size={18} /></span>
          </div>
          <p class="small muted">{stop.text}</p>
          <div class="row stop__links">
            <a class="stop__main" href={stop.href}>
              Открыть «{navLabel(stop.href)}»
              <Icon name="arrowRight" size={16} />
            </a>
            {#each stop.more as href (href)}
              <a class="stop__more micro" href={href}>{navLabel(href)}</a>
            {/each}
          </div>
          {#if i < STOPS.length - 1}
            <span class="stop__next" aria-hidden="true"><Icon name="chevronRight" size={22} /></span>
          {/if}
        </li>
      {/each}
    </ol>

    <p class="micro muted stops__note">
      Экспертное право на разбор предложений по ответам, журнал действий и закрытые данные
      выдаёт администратор сервиса. Остальное открывается сразу после входа.
    </p>
  </section>

  <!-- ── 4. Текстучая строка: чем продукт занят с каждым документом ─────── -->
  <TickerFlow />

  <!-- ── 5. Честные границы ─────────────────────────────────────────────── -->
  <section class="wrap section" id="limits">
    <SectionHead
      title="Что честно не обещаем"
      lead="Границы инструмента входят в работу с ним: то, чего он не делает, названо словами, чтобы решение о проверке числа оставалось за человеком."
    />

    <ol class="limits">
      {#each LIMITS as limit, i (limit.title)}
        <li class="limits__i reveal" style="--reveal-delay: {(i % 2) * 90}ms">
          <h3 class="h4">{limit.title}</h3>
          <p class="small muted">{limit.text}</p>
        </li>
      {/each}
    </ol>
  </section>

  <!-- ── 6. Закрытие в действие ─────────────────────────────────────────── -->
  <section class="close section">
    <div class="scene" aria-hidden="true">
      <span class="blob blob--sage" style="width:34vmax;height:34vmax;top:-12vmax;left:-6vmax"></span>
      <span class="blob blob--coral" style="width:46vmax;height:46vmax;bottom:-24vmax;right:-14vmax"></span>
    </div>
    <div class="wrap wrap--narrow close__inner">
      <div class="close__head">
        <span class="close__mark"><Mascot mood="listen" size={56} /></span>
        <h2 class="h1 reveal">
          Начните с вопроса, в котором есть <span class="hand close__hand">число и условие</span>
        </h2>
      </div>
      <p class="lead reveal" style="--reveal-delay: 90ms">
        Создайте аккаунт и задайте вопрос: ответ соберётся по карте связей корпуса, и каждое число
        в нём раскроется до цитаты и места в источнике. Проверить условия применения, увидеть
        расхождение, оставить отзыв: за этим сюда и приходят аналитики.
      </p>
      <div class="row close__cta reveal" style="--reveal-delay: 150ms">
        <Button href="/register" variant="action" iconEnd="arrowRight">Создать аккаунт</Button>
        <Button href="/login" variant="ghost">У меня есть аккаунт</Button>
      </div>
    </div>
  </section>

  <!-- ── 7. Подвал ──────────────────────────────────────────────────────── -->
  <footer class="wrap">
    <div class="site-foot foot">
      <div class="site-foot__col">
        <p class="h4 foot__name">Научный Клубок</p>
        <p class="micro muted">
          Проверяемые ответы по горно-металлургическим источникам: импорт документов, карта связей
          утверждений с источниками, запрос к корпусу, экспертный разбор и оценка качества.
        </p>
      </div>

      <nav class="site-foot__col" aria-label="Разделы рабочего пространства">
        <p class="micro muted">разделы</p>
        {#each NAV_LINKS as room (room.href)}
          <a href={room.href}>{room.label}</a>
        {/each}
      </nav>

      <nav class="site-foot__col" aria-label="Доступ">
        <p class="micro muted">доступ</p>
        <a href="/login">Войти</a>
        <a href="/register">Создать аккаунт</a>
        <p class="micro muted">Один аккаунт на всё рабочее пространство.</p>
        <p class="micro muted">
          Для входа нужны email и пароль: имя указываете при регистрации. Экспертное право
          выдаёт администратор сервиса.
        </p>
      </nav>

      <div class="site-foot__col">
        <p class="micro muted">показания</p>
        <StatusPill status={serviceView.status} label={serviceView.label} />
        <p class="micro muted">{serviceView.note}</p>
        <p class="micro muted">Снимок на момент открытия страницы.</p>
        {#if !serviceLoading && serviceFailed}
          <Button size="sm" variant="quiet" icon="refresh" onclick={() => void load()}>
            Обновить показания
          </Button>
        {/if}
      </div>
    </div>
  </footer>
</div>

<style>
  /* ── Общие границы сцены ──────────────────────────────────────────────── */
  /* Якорные переходы не прячутся под плавающей навигацией. */
  .landing section[id] {
    scroll-margin-top: calc(var(--topbar-h) + var(--s5));
  }

  /* ── Герой ────────────────────────────────────────────────────────────── */
  .hero {
    position: relative;
    display: grid;
    align-content: center;
    padding-block-end: clamp(var(--s7), 7vw, var(--s9));
  }

  .hero__inner {
    display: grid;
    gap: clamp(var(--s6), 5vw, var(--s8));
    align-items: start;
  }

  .hero__pitch {
    display: flex;
    flex-direction: column;
    gap: var(--s5);
    min-width: 0;
  }

  .hero__title {
    max-width: 16ch;
  }

  .hero__hand {
    display: inline-block;
    /* Caveat в заголовке идёт масштабу дисплея, а не «рукописной вставке». */
    font-size: 1.04em;
    padding-inline: var(--s1) var(--s3);
    color: var(--action-ink);
  }

  /* Композер — единственное действие первого экрана: он шире текстовой меры,
     но не тянется на всю колонку, чтобы поле читалось как рабочий инструмент. */
  .hero__ask {
    margin-top: var(--s2);
    max-width: 760px;
  }

  .hero__asknote {
    max-width: var(--maxw-measure);
  }

  .hero__cue {
    display: inline-flex;
    align-items: center;
    gap: var(--s2);
    align-self: flex-start;
    padding-block: var(--s2);
    color: var(--ink-3);
    text-decoration: none;
    transition: color var(--dur-fast) var(--ease-soft), transform var(--dur) var(--ease-enter);
  }

  .hero__cue:hover {
    color: var(--action-ink);
    transform: translateY(2px);
  }

  .hero__aside {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    min-width: 0;
  }

  /* Отдельный контур без постоянного хранилища называется прямо в первом
     экране: до регистрации, а не после потери записей. */
  .hero__aside-note {
    padding-top: var(--s4);
    border-top: 1px solid var(--line);
    max-width: var(--maxw-measure);
  }

  .metrics {
    display: grid;
    gap: var(--s5);
  }

  .metric {
    display: flex;
    flex-direction: column;
    gap: var(--s1);
  }

  .metric__sk {
    display: block;
    width: calc(var(--s5) * 4);
    height: calc(var(--s5) * 2);
  }

  /* ── Трасса тезиса ────────────────────────────────────────────────────── */
  .trace {
    margin-top: clamp(var(--s6), 5vw, var(--s8));
  }

  .trace__head {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--s2);
    margin-bottom: clamp(var(--s5), 4vw, var(--s7));
  }

  .trace__note {
    max-width: var(--maxw-measure);
  }

  /* Ссылка ведёт тот же вопрос в рабочий контур: снимок остаётся снимком,
     настоящий ответ собирается после входа. */
  .trace__ask {
    display: inline-flex;
    align-items: center;
    gap: var(--s2);
    color: var(--action-ink);
    font-weight: 600;
    text-decoration: none;
    border-bottom: 1px solid var(--line-strong);
    transition: color var(--dur-fast) var(--ease-soft);
  }

  .trace__ask:hover {
    color: var(--ink);
  }

  .trace__body {
    display: grid;
    gap: clamp(var(--s6), 5vw, var(--s8));
    align-items: start;
  }

  /* Лист тезиса: цитата, её адрес и её число — один поднятый над сценой объект. */
  .thesis {
    margin: 0;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--s4);
    width: 100%;
  }

  .thesis__quote {
    font-size: var(--t-lead);
    line-height: var(--lh-head);
    width: 100%;
  }

  .thesis__cap {
    --gap: var(--s2);
  }

  .locator {
    display: inline-flex;
    align-items: center;
    gap: var(--s2);
    color: var(--ink-2);
  }

  /* Свойства читаются списком на той же сцене: карточка нужна артефакту,
     а не каждому пояснению. */
  .facets {
    display: grid;
    gap: clamp(var(--s5), 3.4vw, var(--s7));
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .facet {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: var(--s4);
    align-items: start;
  }

  .facet__mark {
    display: grid;
    place-items: center;
    width: 44px;
    height: 44px;
    border-radius: var(--r-pill);
    background: var(--peach-wash);
    border: 1px solid var(--coral-mist);
    color: var(--ink-2);
    flex: none;
  }

  .facet__body {
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    min-width: 0;
  }

  .measure {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--s2);
    width: 100%;
    padding: var(--s4) var(--s5);
    border-radius: var(--r-md);
    background: var(--surface);
    box-shadow: var(--shadow-inset);
  }

  .measure__value {
    font-family: var(--font-data);
    font-variant-numeric: tabular-nums;
    font-size: var(--t-h2);
    font-weight: 600;
    line-height: 1.05;
    letter-spacing: var(--tr-head);
  }

  .measure__unit {
    font-size: var(--t-body);
    color: var(--ink-3);
  }

  .versus {
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    padding: var(--s4) var(--s5);
    border-radius: var(--r-md);
    background: var(--surface);
    box-shadow: var(--shadow-inset);
  }

  .versus__row {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  /* Значение и его адрес читаются в одной строке, без склейки символом. */
  .versus__line {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--s3);
    min-width: 0;
  }

  .versus__line p {
    min-width: 0;
  }

  .versus__value {
    font-family: var(--font-data);
    font-variant-numeric: tabular-nums;
    color: var(--ink);
    white-space: nowrap;
  }

  .chain {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--s1) var(--s3);
    font-family: var(--font-data);
    color: var(--ink-2);
  }

  .chain__node {
    padding: var(--s1) var(--s2);
    border-radius: var(--r-sm);
    background: var(--surface-sunk);
    color: var(--ink);
  }

  .chain__note {
    font-family: var(--font-ui);
    color: var(--ink-3);
  }

  /* ── Маршрут аналитика ────────────────────────────────────────────────── */
  .stops {
    display: grid;
    gap: clamp(var(--s5), 3vw, var(--s6));
    margin: clamp(var(--s6), 5vw, var(--s8)) 0 0;
    padding: 0;
    list-style: none;
  }

  .stop {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: var(--s3);
    padding: clamp(var(--s5), 3vw, var(--s6));
    border-radius: var(--r-lg);
    background: var(--surface-raised);
    box-shadow: var(--shadow-soft);
    transition: transform var(--dur) var(--ease-enter), box-shadow var(--dur) var(--ease-soft);
  }

  .stop:hover {
    transform: translateY(-3px);
    box-shadow: var(--shadow-lift);
  }

  /* Маршрут, а не набор карточек: номер и название открывают остановку,
     линейка под ними держит горизонталь чтения. */
  .stop__head {
    display: flex;
    align-items: baseline;
    gap: var(--s3);
    padding-bottom: var(--s3);
    border-bottom: 1px solid var(--line);
  }

  .stop__num {
    font-size: var(--t-h3);
    font-weight: 600;
    line-height: 1;
    color: var(--ink-3);
  }

  .stop__head .h3 {
    margin: 0;
  }

  .stop__icon {
    display: inline-flex;
    align-items: center;
    margin-inline-start: auto;
    color: var(--ink-3);
  }

  .stop__links {
    --gap: var(--s4);
    margin-top: var(--s2);
  }

  .stop__main {
    display: inline-flex;
    align-items: center;
    gap: var(--s2);
    color: var(--action-ink);
    font-weight: 600;
    text-decoration: none;
  }

  .stop__more {
    color: var(--ink-3);
    text-decoration: none;
    border-bottom: 1px solid var(--line-strong);
  }

  .stop__main:hover,
  .stop__more:hover {
    color: var(--action-ink);
  }

  .stops__note {
    margin-top: var(--s5);
    max-width: var(--maxw-measure);
  }

  /* Маршрут, а не три возможности: остановки соединены порядком чтения. */
  .stop__next {
    display: none;
    position: absolute;
    top: 50%;
    color: var(--ink-3);
    transform: translateY(-50%);
  }

  /* ── Границы ──────────────────────────────────────────────────────────── */
  /* Нумерованный перечень, а не сетка карточек: перечень читается строками,
     и у него есть потолок в шесть пунктов. */
  .limits {
    display: grid;
    gap: 0 clamp(var(--s6), 5vw, var(--s9));
    margin: clamp(var(--s6), 5vw, var(--s8)) 0 0;
    padding: 0;
    list-style: none;
    counter-reset: limit;
  }

  .limits__i {
    counter-increment: limit;
    display: grid;
    grid-template-columns: 2ch minmax(0, 1fr);
    column-gap: var(--s4);
    row-gap: var(--s2);
    align-items: baseline;
    padding-block: var(--s5);
    border-top: 1px solid var(--line);
  }

  .limits__i::before {
    content: counter(limit);
    font-weight: 600;
    color: var(--ink-3);
  }

  .limits__i h3 {
    grid-column: 2;
    margin: 0;
  }

  .limits__i p {
    grid-column: 2;
    margin: 0;
    max-width: var(--maxw-measure);
  }

  /* ── Закрытие ─────────────────────────────────────────────────────────── */
  .close {
    position: relative;
    padding-block-end: clamp(var(--s9), 10vw, var(--s10));
  }

  .close__inner {
    display: grid;
    gap: var(--s5);
    justify-items: start;
  }

  /* Закрытие читается слева направо, как остальные блоки витрины: знак и
     заголовок в одной строке, ниже — пояснение и действия. */
  .close__head {
    display: flex;
    gap: var(--s5);
    align-items: center;
  }

  .close__head .h1 {
    max-width: 24ch;
  }

  .close__hand {
    /* рукописный акцент держит масштаб заголовка */
    font-size: 1.06em;
    white-space: nowrap;
  }

  .close__mark {
    display: grid;
    place-items: center;
    flex: none;
  }

  .close__cta {
    --gap: var(--s3);
    margin-top: var(--s2);
  }

  /* ── Подвал ───────────────────────────────────────────────────────────── */
  .foot {
    grid-template-columns: repeat(auto-fit, minmax(min(220px, 100%), 1fr));
  }

  .foot__name {
    color: var(--ink);
  }

  /* ── Desktop-композиция ──────────────────────────────────────────────── */
  @media (min-width: 1024px) {
    /* Первый экран — весь вьюпорт за вычетом плавающей навигации: колонка
       питча с композером и отдельная колонка показаний корпуса. */
    .hero {
      min-height: calc(100dvh - var(--topbar-h) - var(--s6));
      padding-block: var(--s6) var(--s7);
    }

    .hero__inner {
      grid-template-columns: minmax(0, 1.42fr) minmax(280px, 0.58fr);
      align-items: center;
      column-gap: clamp(var(--s7), 6vw, var(--s10));
    }

    /* Показания корпуса отделяются линейкой, а не отдельной карточкой:
       витрина остаётся бумагой, а не набором панелей. */
    .hero__aside {
      padding-inline-start: clamp(var(--s5), 3vw, var(--s7));
      border-inline-start: 1px solid var(--line);
    }

    /* Лист трассы: цитата держит левую колонку, четыре свойства читаются
       справа как её расшифровка. */
    .trace__body {
      grid-template-columns: minmax(0, 0.82fr) minmax(0, 1.18fr);
      column-gap: clamp(var(--s7), 5vw, var(--s9));
    }

    .thesis {
      position: sticky;
      top: calc(var(--topbar-h) + var(--s5));
    }

    /* Остановки читаются как временная линия: блоки разной ширины, а не
       три одинаковые карточки. */
    .stops {
      grid-template-columns: minmax(0, 1.12fr) minmax(0, 1fr) minmax(0, 0.84fr);
    }

    /* Стрелка живёт в междустолбцевом зазоре: на узком экране маршрут
       читается сверху вниз и указатель не нужен. */
    .stop__next {
      display: block;
      right: calc(var(--s5) * -1 - 11px);
    }

    .limits {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  /* ── Мобильная композиция ────────────────────────────────────────────── */
  @media (max-width: 900px) {
    /* Первый экран на узком вьюпорте — новая композиция, а не сжатая
       desktop-колонка: показания корпуса уходят компактной строкой вниз. */
    .hero {
      padding-block: var(--s4) var(--s6);
    }

    .hero__inner {
      gap: var(--s6);
    }

    .hero__ask {
      max-width: none;
    }

    .hero__aside {
      gap: var(--s3);
      padding: var(--s4) var(--s5);
      border-radius: var(--r-lg);
      background: var(--surface);
      box-shadow: var(--shadow-soft);
    }

    .hero__aside .metrics {
      grid-template-columns: repeat(auto-fit, minmax(min(120px, 100%), 1fr));
      gap: var(--s4);
    }

    .stops {
      grid-template-columns: minmax(0, 1fr);
    }

    /* Закрытие на узком экране: знак над заголовком, иначе рукописная вставка
       не помещается в сжатую колонку. */
    .close__head {
      flex-direction: column;
      align-items: flex-start;
      gap: var(--s4);
    }
  }

  @media (max-width: 640px) {
    .facet {
      grid-template-columns: minmax(0, 1fr);
      gap: var(--s3);
    }

    .measure__value {
      font-size: var(--t-h3);
    }
  }
</style>
