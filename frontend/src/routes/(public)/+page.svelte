<script lang="ts">
  import { goto } from '$app/navigation';
  import { NAV_LINKS } from '$lib/nav';
  import Button from '$lib/ui/Button.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import type { IconName } from '$lib/ui/icons';
  import Mascot from '$lib/ui/Mascot.svelte';
  import Panel from '$lib/ui/Panel.svelte';
  import PromptInput from '$lib/ui/PromptInput.svelte';
  import SectionHead from '$lib/ui/SectionHead.svelte';

  // Три ступени трассы тезиса — то, чем ответ отличается от поиска по фрагментам.
  // Слов о числе здесь меньше, чем самого числа: способ измерения живёт за «i».
  const TRACE: { icon: IconName; name: string; text: string }[] = [
    {
      icon: 'doc',
      name: 'Документ',
      text: 'Научно-технический источник.',
    },
    {
      icon: 'pin',
      name: 'Фрагмент с адресом',
      text: 'Страница, лист или ячейки, где это можно проверить.',
    },
    {
      icon: 'scale',
      name: 'Утверждение с условиями',
      text: 'Значение, единица и условия применения.',
    },
  ];

  // Вопрос витрины держит то, что сервис действительно разбирает: ссылка уводит
  // не в картинку ответа, а в настоящий запрос рабочего контура.
  const DEMO_QUESTION =
    'Какие методы обессоливания подходят для шахтной воды с сульфатами и хлоридами 200–300 мг/л при сухом остатке ≤1000 мг/л?';
  const demoHref = `/research?q=${encodeURIComponent(DEMO_QUESTION)}`;

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

</script>

<svelte:head>
  <title>Научный Клубок: проверяемые ответы по горно-металлургическим источникам</title>
  <meta
    name="description"
    content="Сервис проверяемых ответов для горной металлургии: каждое число раскрывается до цитаты, страницы, листа и диапазона ячеек первоисточника, условия применения остаются в записи, расхождения и пробелы источников видны в ответе."
  />
</svelte:head>

<div class="page landing">
  <!-- ── 1. Первый экран ──────────────────────────────────────────────── -->
  <section class="hero">
    <div class="scene" aria-hidden="true">
      <span class="blob blob--coral" style="width:54vmax;height:54vmax;top:-24vmax;right:-16vmax"></span>
      <span class="blob blob--lav" style="width:40vmax;height:40vmax;bottom:-18vmax;left:-14vmax"></span>
    </div>

    <div class="wrap hero__inner">
      <div class="hero__pitch">
        <h1 class="display hero__title reveal">
          Каждое число ведёт к <span class="hand hero__hand">источнику</span>
        </h1>
        <p class="lead reveal" style="--reveal-delay: 90ms">
          Клубок разбирает научно-технические документы на находки с адресом в источнике.
          Число остаётся вместе с единицей и условиями, расхождение видно уже в ответе.
        </p>

        <div class="hero__ask reveal" style="--reveal-delay: 150ms">
          <PromptInput
            bind:value={draft}
            variant="hero"
            name="hero"
            busy={sending}
            placeholder="Что найти в корпусе: число, технология, условие"
            hint="Enter отправляет вопрос в рабочее пространство"
            onsubmit={(text) => void ask(text)}
          />
        </div>

        <div class="row hero__meta reveal" style="--reveal-delay: 200ms">
          <a class="hero__cue micro" href="#trace">
            <Icon name="chevronDown" size={15} />
            как устроена проверка
          </a>
        </div>
      </div>
    </div>
  </section>

  <!-- ── 2. Трасса одного тезиса: единственный сильный жест витрины ─────── -->
  <section class="wrap section" id="trace">
    <SectionHead
      title="Тезис стоит ровно столько, сколько его трасса"
      lead="Ответ раскрывается до места в документе: цитата, адрес, число с условиями."
    />

    <!-- Анатомия трассы — поверхность, а не три абзаца: шаги идут строкой,
         определения произносятся один раз под «i». Панель несит и жест, и
         единственный переход в продукт, поэтому под сценой не остаётся
         пустого поля. -->
    <div class="trace__panel">
      <Panel tone="sage">
        <ol class="chain">
          {#each TRACE as step, i (step.name)}
            <li class="chain__step reveal" style="--reveal-delay: {i * 80}ms">
              <span class="chain__mark" aria-hidden="true"><Icon name={step.icon} size={19} /></span>
              <p class="chain__name h4">{step.name}</p>
            </li>
          {/each}
        </ol>
        <div class="row trace__asks">
          <a class="trace__ask" href={demoHref}>Попробовать пример про шахтную воду</a>
        </div>
      </Panel>
    </div>
  </section>

  <!-- ── 3. Четыре раздела: маршрут аналитика без карточек ──────────────── -->
  <section class="wrap section" id="route">
    <SectionHead
      title="Куда вы попадёте после входа"
      lead="Четыре раздела — по числу задач, которые аналитик решает каждый день."
    />

    <ul class="rooms">
      {#each NAV_LINKS as room, i (room.href)}
        <li class="rooms__i reveal" style="--reveal-delay: {i * 60}ms">
          <a href={room.href}>
            <span class="rooms__icon" aria-hidden="true"><Icon name={room.icon} size={18} /></span>
            <span class="rooms__label">{room.label}</span>
            <span class="rooms__gloss small muted">{room.gloss}</span>
          </a>
        </li>
      {/each}
    </ul>
  </section>

  <!-- ── 5. Закрытие в действие ─────────────────────────────────────────── -->
  <section class="close section">
    <div class="scene" aria-hidden="true">
      <span class="blob blob--sage" style="width:34vmax;height:34vmax;top:-12vmax;left:-6vmax"></span>
      <span class="blob blob--coral" style="width:46vmax;height:46vmax;bottom:-24vmax;right:-14vmax"></span>
    </div>
    <div class="wrap wrap--narrow close__inner">
      <div class="close__head">
        <span class="close__mark"><Mascot mood="listen" size={56} /></span>
        <h2 class="h1">
          Начните с вопроса, в котором есть <span class="hand close__hand">условия</span>
        </h2>
      </div>
      <p class="lead reveal" style="--reveal-delay: 80ms">
        Аккаунт один на всё рабочее пространство: вопрос, находки, карта связей.
      </p>
      <div class="row close__cta reveal" style="--reveal-delay: 130ms">
        <Button href="/register" variant="action">Создать аккаунт</Button>
        <Button href="/login" variant="ghost">У меня есть аккаунт</Button>
      </div>
    </div>
  </section>

  <!-- ── 6. Подвал ──────────────────────────────────────────────────────── -->
  <footer class="wrap">
    <div class="site-foot foot">
      <div class="site-foot__col">
        <p class="h4 foot__name">Научный Клубок</p>
        <p class="micro muted">
          Проверяемые ответы по горно-металлургическим источникам.
        </p>
      </div>

      <nav class="site-foot__col" aria-label="Разделы рабочего пространства">
        <p class="micro muted">разделы</p>
        {#each NAV_LINKS as room (room.href)}
          <a href={room.href}>{room.label}</a>
        {/each}
      </nav>

      <div class="site-foot__col">
        <p class="micro muted">доступ</p>
        <a href="/login">Войти</a>
        <a href="/register">Создать аккаунт</a>
        <p class="micro muted">
          Имя и email указываются при регистрации. Экспертное право выдаёт администратор сервиса.
        </p>
      </div>
    </div>
  </footer>
</div>

<style>
  /* Якорные переходы не прячутся под плавающей навигацией. */
  .landing section[id] {
    scroll-margin-top: calc(var(--topbar-h) + var(--s5));
  }

  /* ── Первый экран ────────────────────────────────────────────────────── */
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

  /* Композер — единственное действие первого экрана. */
  .hero__ask {
    margin-top: var(--s2);
    max-width: 720px;
  }

  .hero__meta {
    --gap: var(--s3);
    flex-wrap: wrap;
  }

  .hero__cue {
    display: inline-flex;
    align-items: center;
    gap: var(--s2);
    /* Зона касания шрифта не видна: строка чтения остаётся micro, а попасть
       надо по 32 px. Отрицательная компенсация держит строку на своём месте. */
    min-block-size: 32px;
    padding-block: var(--s2);
    margin-block: calc(var(--s2) * -1);
    color: var(--ink-3);
    text-decoration: none;
    transition: color var(--dur-fast) var(--ease-soft);
  }

  .hero__cue:hover {
    color: var(--action-ink);
  }

  /* ── Трасса тезиса ───────────────────────────────────────────────────── */
  .chain {
    display: grid;
    gap: clamp(var(--s5), 3.4vw, var(--s7));
    margin: 0;
    padding: 0;
    list-style: none;
  }

  /* Трасса живёт внутри панели-сцены: панель несёт и шаги, и переход, поэтому
     у шагов нет своего верхнего отступа и определений под именем. */
  .trace__panel {
    margin-top: clamp(var(--s6), 5vw, var(--s8));
  }

  .trace__panel .trace__asks {
    margin-top: var(--s5);
  }

  .chain__step {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    column-gap: var(--s4);
    row-gap: var(--s2);
    align-items: baseline;
    padding-block: var(--s4);
    border-top: 1px solid var(--line);
  }

  .chain__mark {
    display: grid;
    place-items: center;
    align-self: center;
    width: 40px;
    height: 40px;
    border-radius: var(--r-pill);
    background: var(--peach-wash);
    border: 1px solid var(--coral-mist);
    color: var(--ink-2);
  }

  .chain__name {
    grid-column: 2;
    margin: 0;
  }

  .trace__asks {
    --gap: var(--s5);
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
  }

  .trace__ask {
    display: inline-flex;
    align-items: center;
    /* Цель нажатия 32 px геометрией строки: кегль тот же, ритм панели держит
       отрицательная компенсация поля. */
    min-block-size: 32px;
    padding-block: var(--s1);
    margin-block: calc(var(--s1) * -1);
    color: var(--action-ink);
    font-weight: 600;
    text-decoration: none;
    border-bottom: 1px solid var(--line-strong);
    transition: color var(--dur-fast) var(--ease-soft);
  }

  .trace__ask:hover {
    color: var(--ink);
  }

  /* ── Разделы ─────────────────────────────────────────────────────────── */
  .rooms {
    display: grid;
    gap: 0 clamp(var(--s6), 5vw, var(--s9));
    margin: clamp(var(--s6), 5vw, var(--s8)) 0 0;
    padding: 0;
    list-style: none;
  }

  .rooms__i a {
    display: grid;
    grid-template-columns: auto auto minmax(0, 1fr);
    align-items: center;
    gap: var(--s4);
    padding-block: var(--s5);
    border-top: 1px solid var(--line);
    color: inherit;
    text-decoration: none;
    transition: color var(--dur) var(--ease-soft);
  }

  .rooms__i a:hover .rooms__label {
    color: var(--action-ink);
  }

  .rooms__icon {
    color: var(--ink-3);
  }

  .rooms__label {
    font-weight: 600;
    transition: color var(--dur-fast) var(--ease-soft);
  }

  .rooms__gloss {
    min-width: 0;
  }

  /* ── Закрытие ────────────────────────────────────────────────────────── */
  .close {
    position: relative;
    padding-block-end: clamp(var(--s9), 10vw, var(--s10));
  }

  .close__inner {
    display: grid;
    gap: var(--s5);
    justify-items: start;
  }

  .close__head {
    display: flex;
    gap: var(--s5);
    align-items: center;
  }

  .close__head .h1 {
    max-width: 24ch;
  }

  .close__hand {
    font-size: 1.06em;
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

  /* ── Подвал ──────────────────────────────────────────────────────────── */
  .foot {
    grid-template-columns: repeat(auto-fit, minmax(min(220px, 100%), 1fr));
  }

  .foot__name {
    color: var(--ink);
  }

  /* Ссылки подвала ведут в разделы и во вход: строка 23 px не цель. Высота
     boxes 32 px, а колонку не разъедает отрицательная компенсация поля. */
  .foot a {
    min-block-size: 32px;
    padding-block: var(--s1);
    margin-block: calc(var(--s1) * -1);
  }

  /* ── Desktop-композиция ──────────────────────────────────────────────── */
  @media (min-width: 1024px) {
    /* Первый экран — весь вьюпорт за вычетом плавающей навигации. */
    .hero {
      min-height: calc(100dvh - var(--topbar-h) - var(--s6));
      padding-block: var(--s6) var(--s7);
    }

    .hero__inner { grid-template-columns: minmax(0, 1fr); }

    .chain,
    .rooms {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .rooms {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  /* ── Мобильная композиция ────────────────────────────────────────────── */
  @media (max-width: 900px) {
    .hero {
      padding-block: var(--s4) var(--s6);
    }

    .hero__inner {
      gap: var(--s6);
    }

    .hero__ask {
      max-width: none;
    }

    .rooms__i a {
      grid-template-columns: auto minmax(0, 1fr);
      gap: var(--s2) var(--s3);
    }

    .rooms__gloss {
      grid-column: 2;
    }

    .close__head {
      flex-direction: column;
      align-items: flex-start;
      gap: var(--s4);
    }
  }
</style>
