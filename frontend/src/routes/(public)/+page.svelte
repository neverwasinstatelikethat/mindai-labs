<script lang="ts">
  import { goto } from '$app/navigation';
  import { NAV_LINKS } from '$lib/nav';
  import Button from '$lib/ui/Button.svelte';
  import Icon from '$lib/ui/Icon.svelte';
  import PromptInput from '$lib/ui/PromptInput.svelte';

  let draft = $state('');
  let sending = $state(false);

  async function ask(text: string): Promise<void> {
    const question = text.trim();
    if (!question) return;
    sending = true;
    try {
      await goto(`/research?q=${encodeURIComponent(question)}`);
    } finally {
      sending = false;
    }
  }
</script>

<svelte:head>
  <title>StormIdea — гипотезы, которые можно проверить</title>
  <meta
    name="description"
    content="StormIdea помогает находить гипотезы и узкие места, проверять их по материалам и видеть связи между фактами."
  />
</svelte:head>

<div class="page landing">
  <section class="hero">
    <div class="wrap hero__inner">
      <div class="hero__story">
        <p class="hero__brand">StormIdea</p>
        <h1 class="display hero__title">Идеи, которые <span>выдерживают проверку</span></h1>
        <p class="lead hero__copy">
          Исследуйте гипотезы и узкие места, сопоставляйте материалы и возвращайтесь от вывода к источнику.
        </p>
        <p class="hero__scribble">От любопытства до ясности</p>
      </div>
      <div class="hero__ask">
        <PromptInput
          bind:value={draft}
          variant="hero"
          name="hero"
          busy={sending}
          placeholder="С чего хотите разобраться?"
          label="Ваш вопрос"
          hint="Опишите задачу своими словами"
          onsubmit={(text) => void ask(text)}
        />
      </div>
      <div class="hero__actions">
        <Button href="/register" variant="action">Создать пространство</Button>
        <Button href="/login" variant="ghost">Войти</Button>
      </div>
    </div>
  </section>

  <section class="trace section" aria-labelledby="trace-title">
    <div class="wrap trace__inner">
      <div class="trace__intro">
        <p class="eyebrow">Как устроена проверка</p>
        <h2 class="h1" id="trace-title">У каждого вывода есть путь назад</h2>
        <p class="lead">Вопрос соединяется с материалами, а ответ показывает, где подтверждение, где расхождение и чего пока не хватает.</p>
      </div>
      <ol class="trace__path">
        <li><span>01</span><strong>Вопрос</strong><small>задаёт направление поиска</small></li>
        <li><span>02</span><strong>Материалы</strong><small>дают фрагменты и источники</small></li>
        <li><span>03</span><strong>Вывод</strong><small>возвращает к доказательству</small></li>
        <li><span>04</span><strong>Следующий шаг</strong><small>помогает решить, что проверить дальше</small></li>
      </ol>
    </div>
  </section>

  <section class="wrap section" aria-labelledby="how-title">
    <h2 class="h2" id="how-title">Начните с того, что хотите понять</h2>
    <ol class="steps">
      <li>
        <span class="steps__number">01</span>
        <div>
          <h3 class="h4">Сформулируйте гипотезу</h3>
          <p class="small muted">Опишите наблюдение, проблему или идею, которую хотите проверить.</p>
        </div>
      </li>
      <li>
        <span class="steps__number">02</span>
        <div>
          <h3 class="h4">Сопоставьте материалы</h3>
          <p class="small muted">StormIdea находит подтверждения, расхождения и пробелы в источниках.</p>
        </div>
      </li>
      <li>
        <span class="steps__number">03</span>
        <div>
          <h3 class="h4">Решите, что делать дальше</h3>
          <p class="small muted">Перейдите от вывода к фактам и связанным наблюдениям.</p>
        </div>
      </li>
    </ol>
  </section>

  <section class="workspace section" aria-labelledby="workspace-title">
    <div class="wrap">
      <div class="workspace__head">
        <div>
          <p class="eyebrow">Одно пространство</p>
          <h2 class="h1" id="workspace-title">От первого вопроса до связанной картины</h2>
        </div>
        <p class="lead">Переключайтесь между задачами, сохраняя контекст и путь к материалам.</p>
      </div>
    <nav aria-label="Возможности StormIdea">
      <ul class="rooms">
        {#each NAV_LINKS as room (room.href)}
          <li>
            <a href={room.href}>
              <span>{room.label}</span>
              <span class="small muted">{room.gloss}</span>
              <Icon name="arrowRight" size={18} />
            </a>
          </li>
        {/each}
      </ul>
    </nav>
    </div>
  </section>

  <section class="boundaries section" aria-labelledby="boundaries-title">
    <div class="wrap boundaries__inner">
      <div>
        <p class="eyebrow">Прозрачная работа с материалами</p>
        <h2 class="h1" id="boundaries-title">Не додумывает то, чего нет в источниках</h2>
      </div>
      <p class="lead">Ответ связан с подтверждениями. Если источники расходятся или данных недостаточно, это остаётся заметно. Решение о следующем шаге остаётся за вами.</p>
    </div>
  </section>

  <footer class="wrap site-foot">
    <p class="h4">StormIdea</p>
    <div class="site-foot__actions">
      <a href="/register">Создать пространство</a>
      <a href="/login">Войти</a>
    </div>
  </footer>
</div>

<style>
  .hero {
    background: var(--sage);
  }

  .hero__inner {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(24rem, 0.78fr);
    align-items: center;
    gap: var(--s4);
    min-height: min(700px, calc(100svh - var(--topbar-h)));
    padding-block: var(--s7);
  }

  .hero__story {
    align-self: center;
    max-width: 58rem;
  }

  .hero__ask {
    padding: var(--s5);
    border-radius: var(--r-lg);
    background: var(--surface-raised);
    box-shadow: var(--shadow-lift);
  }

  .hero__brand {
    color: var(--action-ink);
    font-size: var(--t-body);
    font-weight: 650;
  }

  .hero__title,
  .hero__copy,
  .hero__ask,
  .hero__actions {
    max-width: 52rem;
  }

  .hero__title {
    max-width: 13ch;
    text-wrap: balance;
  }

  .hero__title span {
    color: var(--action-ink);
    font-family: var(--font-hand);
    font-size: 1.08em;
    font-weight: 500;
  }

  .hero__scribble {
    margin-top: var(--s4);
    color: var(--action-ink);
    font-family: var(--font-hand);
    font-size: var(--t-hand);
  }

  .hero__copy {
    max-width: 62ch;
  }

  .hero__actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s2);
  }

  .section {
    padding-block: var(--s8);
  }

  .section > h2 {
    margin-bottom: var(--s5);
  }

  .trace { background: var(--lavender); }

  .trace__inner {
    display: grid;
    grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr);
    align-items: center;
    gap: var(--s7);
  }

  .trace__intro { max-width: 54ch; }
  .trace__intro h2 { max-width: 14ch; }

  .trace__path {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--s4) var(--s6);
  }

  .trace__path li {
    display: grid;
    align-content: start;
    gap: var(--s2);
    min-height: 132px;
    padding: var(--s4);
    border-radius: var(--r-md);
    background: var(--surface-raised);
  }

  .trace__path li > span {
    color: var(--action-ink);
    font-family: var(--font-data);
    font-size: var(--t-small);
  }

  .trace__path strong { font-size: var(--t-h4); }
  .trace__path small { color: var(--ink-2); line-height: var(--lh-body); }

  .workspace { background: var(--peach-wash); }

  .workspace__head {
    display: flex;
    justify-content: space-between;
    align-items: end;
    gap: var(--s6);
    margin-bottom: var(--s5);
  }

  .workspace__head > * { max-width: 56ch; }
  .workspace__head h2 { max-width: 15ch; }

  .boundaries { background: var(--surface-sunk); }

  .boundaries__inner {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(16rem, 0.72fr);
    align-items: center;
    gap: var(--s6);
  }

  .boundaries__inner h2 { max-width: 18ch; }

  .steps {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--s5);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .steps li {
    display: grid;
    align-content: start;
    gap: var(--s3);
  }

  .steps__number {
    color: var(--ink-3);
    font-size: var(--t-small);
    font-variant-numeric: tabular-nums;
  }

  .steps h3 {
    margin-bottom: var(--s1);
  }

  .rooms {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  /* Строки раздела разделены одной линией: рамки вокруг списка и под
     последним пунктом не было бы видно, где список кончается. */
  .rooms li + li {
    border-top: 1px solid var(--line);
  }

  .rooms a {
    display: grid;
    grid-template-columns: minmax(8rem, 0.5fr) minmax(0, 1fr) auto;
    align-items: center;
    gap: var(--s4);
    min-height: 64px;
    color: var(--ink);
    text-decoration: none;
  }

  .rooms a:hover span:first-child {
    color: var(--action-ink);
  }

  .site-foot {
    display: flex;
    justify-content: space-between;
    gap: var(--s4);
    padding-block: var(--s5);
    margin-top: var(--s5);
  }

  /* Ссылки подвала — цели нажатия, а не строки текста: 34 px по высоте без
     видимого изменения строки. */
  .site-foot__actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s4);
  }

  .site-foot__actions a {
    display: inline-flex;
    align-items: center;
    min-height: 34px;
    border-radius: var(--r-sm);
  }

  @media (max-width: 720px) {
    .hero__inner {
      grid-template-columns: minmax(0, 1fr);
      min-height: auto;
      padding-block: var(--s7);
    }

    .hero__ask { padding: var(--s4); }
    .trace__inner,
    .boundaries__inner { grid-template-columns: minmax(0, 1fr); }
    .workspace__head { align-items: flex-start; flex-direction: column; }
    .trace__path { gap: var(--s3); }

    .steps {
      grid-template-columns: 1fr;
    }

    .rooms a {
      grid-template-columns: minmax(6rem, 0.5fr) minmax(0, 1fr) auto;
      gap: var(--s2);
      padding-block: var(--s3);
    }

    .site-foot {
      flex-direction: column;
    }
  }
</style>
