<script lang="ts">
  import { ArrowRight, BookOpen, CheckCircle2, FileSearch, Network, Quote, ShieldCheck } from '@lucide/svelte';
  import AppHeader from '$lib/AppHeader.svelte';
</script>

<svelte:head>
  <title>Научный Клубок — проверяемые R&D-знания</title>
  <meta name="description" content="Исследовательский поиск по горно-металлургическим документам с точными цитатами, числовыми условиями и графом доказательств." />
</svelte:head>

<AppHeader />

<main id="main">
  <section class="hero page-shell">
    <div class="hero__copy">
      <p class="section-label">Для исследований в горной металлургии</p>
      <h1>Не просто ответ.<br />Путь до каждого доказательства.</h1>
      <p class="hero__lead">Задайте сложный технический вопрос. Платформа сопоставит документы, числовые условия и связи — и покажет, на чём основан каждый вывод.</p>
      <div class="hero__actions">
        <a class="button button--primary" href="/research">Начать исследование <ArrowRight size={19} /></a>
        <a class="text-link" href="#how">Посмотреть процесс</a>
      </div>
      <div class="trust-line"><ShieldCheck size={19} /><span>Выводы модели всегда отделены от извлечённых и подтверждённых фактов</span></div>
    </div>

    <div class="answer-preview" aria-label="Пример проверяемого ответа">
      <div class="answer-preview__top"><span>Пример результата</span><span class="status status--verified"><CheckCircle2 size={14} /> подтверждено источниками</span></div>
      <h2>Обратный осмос применим после предварительной очистки</h2>
      <p>Для воды с минерализацией до 500 мг/л метод обеспечивает требуемое качество при контроле жёсткости.</p>
      <dl class="conditions">
        <div><dt>Минерализация</dt><dd>до 500 мг/л</dd></div>
        <div><dt>Жёсткость</dt><dd>до 5 ммоль/л</dd></div>
        <div><dt>Источников</dt><dd>12</dd></div>
      </dl>
      <blockquote><Quote size={18} /> «…обратный осмос эффективно используется для обессоливания шахтных вод…»</blockquote>
      <div class="source-line"><BookOpen size={16} /> Обзор методов обессоливания · стр. 47</div>
    </div>
  </section>

  <section class="process-section" id="how">
    <div class="page-shell">
      <div class="section-heading"><p class="section-label">Один понятный процесс</p><h2>От документа до проверяемого решения</h2><p>Граф работает в фоне. Пользователь видит привычные вопросы, таблицы, цитаты и понятные связи.</p></div>
      <ol class="process-list">
        <li><span>01</span><FileSearch size={25} /><div><h3>Добавьте материалы</h3><p>PDF, DOCX, XLSX и JSON сохраняются вместе с точным положением каждого фрагмента.</p></div></li>
        <li><span>02</span><Network size={25} /><div><h3>Система сопоставит знания</h3><p>Термины, утверждения, условия и числа связываются без потери первоисточника.</p></div></li>
        <li><span>03</span><CheckCircle2 size={25} /><div><h3>Проверьте результат</h3><p>Откройте цитату, сравните методы, найдите конфликт и подтвердите вывод как эксперт.</p></div></li>
      </ol>
    </div>
  </section>

  <section class="trust-section page-shell" id="trust">
    <div class="trust-section__statement"><p class="section-label">Доказательность по умолчанию</p><h2>Система не скрывает неопределённость</h2></div>
    <div class="trust-rules">
      <article><strong>Извлечено</strong><p>Факт найден в документе и привязан к точному фрагменту.</p></article>
      <article><strong>Выведено моделью</strong><p>Гипотеза показана отдельно и не попадает в доверенный слой автоматически.</p></article>
      <article><strong>Проверено экспертом</strong><p>Решение, автор и дата проверки сохраняются в журнале аудита.</p></article>
    </div>
  </section>

  <section class="final-cta page-shell">
    <div><h2>Начните с вопроса, который обычный поиск упрощает слишком сильно</h2><p>Числовые диапазоны, география, период и условия применения останутся частью ответа.</p></div>
    <a class="button button--primary" href="/research">Открыть рабочее пространство <ArrowRight size={19} /></a>
  </section>
</main>

<footer class="footer page-shell"><span>© Научный Клубок, 2026</span><span>Платформа проверяемых R&D-знаний</span></footer>

