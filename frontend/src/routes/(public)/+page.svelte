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
      <p class="hero__brand">StormIdea</p>
      <h1 class="display hero__title">От вопроса — к проверяемой гипотезе</h1>
      <p class="lead hero__copy">
        Находите идеи и узкие места, сопоставляйте материалы и проверяйте, на чём держится вывод.
      </p>
      <div class="hero__ask">
        <PromptInput
          bind:value={draft}
          variant="hero"
          name="hero"
          busy={sending}
          mascot={false}
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

  <section class="wrap section" aria-labelledby="how-title">
    <h2 class="h2" id="how-title">Работа начинается с вопроса</h2>
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

  <section class="wrap section workspace" aria-labelledby="workspace-title">
    <h2 class="h2" id="workspace-title">В одном рабочем пространстве</h2>
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
    border-bottom: 1px solid var(--line);
    background: var(--surface);
  }

  .hero__inner {
    display: grid;
    align-content: center;
    gap: var(--s4);
    min-height: min(660px, calc(100svh - var(--topbar-h)));
    padding-block: var(--s8);
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
    max-width: 16ch;
    text-wrap: balance;
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
    padding-top: var(--s3);
    border-top: 1px solid var(--line-strong);
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
    border-top: 1px solid var(--line);
  }

  .rooms li {
    border-bottom: 1px solid var(--line);
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
    border-top: 1px solid var(--line);
  }

  .site-foot__actions {
    display: flex;
    gap: var(--s4);
  }

  @media (max-width: 720px) {
    .hero__inner {
      min-height: auto;
      padding-block: var(--s7);
    }

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
