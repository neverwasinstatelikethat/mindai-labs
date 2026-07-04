<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import {
    AlertTriangle, GitCompare, LoaderCircle, Lock, Plus, Search, X,
  } from '@lucide/svelte';
  import AppHeader from '$lib/AppHeader.svelte';
  import { api } from '$lib/api';
  import { getRole } from '$lib/roleStore.svelte';
  import { hasPermission, type ComparisonTable } from '$lib/types';

  let question = $state('');
  let entityInput = $state('');
  let entities: string[] = $state([]);
  let dimensionInput = $state('');
  let dimensions: string[] = $state([]);
  let result: ComparisonTable | null = $state(null);
  let loading = $state(false);
  let error = $state('');
  let initQuestion = $state('');

  const currentRole = $derived(getRole());
  const canAccess = $derived(hasPermission(currentRole, 'export:run'));

  onMount(() => {
    const params = page.url.searchParams;
    const q = params.get('q');
    const ents = params.get('entities');
    if (q) {
      question = q;
      initQuestion = q;
    }
    if (ents) {
      entities = ents.split(',').map((e) => e.trim()).filter(Boolean);
    }
    if (q && entities.length) {
      runCompare();
    }
  });

  function addEntity() {
    const val = entityInput.trim();
    if (val && !entities.includes(val)) {
      entities = [...entities, val];
    }
    entityInput = '';
  }

  function removeEntity(idx: number) {
    entities = entities.filter((_, i) => i !== idx);
  }

  function addDimension() {
    const val = dimensionInput.trim();
    if (val && !dimensions.includes(val)) {
      dimensions = [...dimensions, val];
    }
    dimensionInput = '';
  }

  function removeDimension(idx: number) {
    dimensions = dimensions.filter((_, i) => i !== idx);
  }

  async function runCompare() {
    if (entities.length === 0) {
      error = 'Добавьте хотя бы одну сущность для сравнения';
      return;
    }
    loading = true;
    error = '';
    try {
      result = await api.compare(question || 'Сравнить', entities, dimensions);
    } catch (reason) {
      error = reason instanceof Error ? reason.message : 'Сравнение не удалось';
    } finally {
      loading = false;
    }
  }

  function handleEntityKey(event: KeyboardEvent) {
    if (event.key === 'Enter') { event.preventDefault(); addEntity(); }
  }
  function handleDimensionKey(event: KeyboardEvent) {
    if (event.key === 'Enter') { event.preventDefault(); addDimension(); }
  }
</script>

<svelte:head><title>Сравнение — Научный Клубок</title></svelte:head>

<AppHeader compact />
<main class="workspace" id="main">
  <section class="page-heading">
    <div>
      <p class="section-label">Сравнение сущностей</p>
      <h1>Сопоставление с доказательствами</h1>
    </div>
  </section>

  {#if !canAccess}
    <div class="permission-denied workspace-panel">
      <Lock size={36} />
      <h2>Доступ запрещён</h2>
      <p>Сравнение доступно аналитикам и выше. Переключите роль в шапке страницы.</p>
    </div>
  {:else}
    <!-- Comparison form -->
    <section class="workspace-panel compare-form">
      <div class="compare-field">
        <label for="compare-q">Вопрос</label>
        <input
          id="compare-q"
          type="text"
          bind:value={question}
          placeholder="Какой вопрос исследовать?"
        />
      </div>

      <div class="compare-field">
        <label for="compare-entities">Сущности для сравнения</label>
        <div class="chip-input">
          {#each entities as ent, idx}
            <span class="chip">
              {ent}
              <button onclick={() => removeEntity(idx)} aria-label="Удалить"><X size={13} /></button>
            </span>
          {/each}
          <input
            id="compare-entities"
            type="text"
            bind:value={entityInput}
            onkeydown={handleEntityKey}
            placeholder="Введите название и нажмите Enter"
          />
        </div>
      </div>

      <div class="compare-field">
        <label for="compare-dimensions">Размеры (свойства) — необязательно</label>
        <div class="chip-input">
          {#each dimensions as dim, idx}
            <span class="chip chip--dim">
              {dim}
              <button onclick={() => removeDimension(idx)} aria-label="Удалить"><X size={13} /></button>
            </span>
          {/each}
          <input
            id="compare-dimensions"
            type="text"
            bind:value={dimensionInput}
            onkeydown={handleDimensionKey}
            placeholder="Например: стоимость, эффективность, скорость"
          />
        </div>
      </div>

      <div class="compare-actions">
        <button class="button button--primary" onclick={runCompare} disabled={loading || entities.length === 0}>
          {#if loading}<LoaderCircle class="spin" size={18} /> Сравниваем…{:else}<GitCompare size={18} /> Сравнить{/if}
        </button>
      </div>

      {#if error}
        <div class="notice notice--error">
          <AlertTriangle size={19} />
          <div><strong>Ошибка</strong><p>{error}</p></div>
        </div>
      {/if}
    </section>

    <!-- Results -->
    {#if result}
      <section class="workspace-panel compare-result">
        <div class="panel-intro">
          <div><p class="section-label">Результат сравнения</p><h2>{result.question}</h2></div>
        </div>

        {#if result.rows.length > 0}
          <div class="compare-table-wrap">
            <table class="compare-table">
              <thead>
                <tr>
                  <th></th>
                  {#each result.headers as header}
                    <th>{header}</th>
                  {/each}
                </tr>
              </thead>
              <tbody>
                {#each result.rows as row}
                  <tr>
                    <td class="compare-table__row-label"><strong>{row.item}</strong></td>
                    {#each result.headers as header}
                      {@const cell = row.cells[header]}
                      <td>
                        {#if cell}
                          <div class="compare-cell">
                            <span class="compare-cell__value">{cell.value ?? '—'}</span>
                            {#if cell.unit}<span class="compare-cell__unit">{cell.unit}</span>{/if}
                            {#if cell.confidence > 0}
                              <span class="compare-cell__conf" title="Уверенность">
                                <span class="conf-dot" style={`width:${cell.confidence * 100}%`}></span>
                                {Math.round(cell.confidence * 100)}%
                              </span>
                            {/if}
                            {#if cell.evidence}
                              <span class="compare-cell__evidence" title="Доказательство">{cell.evidence}</span>
                            {/if}
                          </div>
                        {:else}
                          <span class="muted">—</span>
                        {/if}
                      </td>
                    {/each}
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {:else}
          <div class="empty-state">
            <Search size={32} />
            <h2>Нет данных для сравнения</h2>
            <p>Попробуйте изменить сущности или формулировку вопроса.</p>
          </div>
        {/if}
      </section>
    {:else if !loading}
      <div class="workspace-panel empty-state">
        <GitCompare size={32} />
        <h2>Настройте сравнение</h2>
        <p>Добавьте сущности и нажмите «Сравнить», чтобы получить таблицу с доказательствами.</p>
      </div>
    {/if}
  {/if}
</main>
