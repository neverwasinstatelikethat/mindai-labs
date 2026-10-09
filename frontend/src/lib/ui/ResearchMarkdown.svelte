<script lang="ts">
  import { Lexer, type Token, type Tokens } from 'marked';
  import type { Finding } from '$lib/types';

  let { text, findings = [], onfinding }: {
    text: string;
    findings?: Finding[];
    onfinding?: (id: string) => void;
  } = $props();

  const tokens = $derived(Lexer.lex(text, { gfm: true }));

  function findingFor(href: string): Finding | undefined {
    return href.startsWith('finding:') ? findings.find((item) => item.id === href.slice(8)) : undefined;
  }

  function safeHref(href: string): string | null {
    // Внешние ссылки допускаются только по явному web-протоколу.
    return /^https?:\/\//i.test(href) ? href : null;
  }
</script>

<div class="research-markdown">
  {@render blocks(tokens)}
</div>

{#snippet inline(items: Token[])}
  {#each items as token}
    {#if token.type === 'strong'}
      <strong>{@render inline((token as Tokens.Strong).tokens)}</strong>
    {:else if token.type === 'em'}
      <em>{@render inline((token as Tokens.Em).tokens)}</em>
    {:else if token.type === 'del'}
      <del>{@render inline((token as Tokens.Del).tokens)}</del>
    {:else if token.type === 'codespan'}
      <code>{(token as Tokens.Codespan).text}</code>
    {:else if token.type === 'br'}
      <br />
    {:else if token.type === 'link'}
      {@const link = token as Tokens.Link}
      {@const finding = findingFor(link.href)}
      {#if finding && onfinding}
        <button class="citation" onclick={() => onfinding?.(finding.id)} title="Открыть цитату и разбор: {finding.statement}">
          {@render inline(link.tokens)}
        </button>
      {:else if safeHref(link.href)}
        <a href={safeHref(link.href)} target="_blank" rel="noopener noreferrer">{@render inline(link.tokens)}</a>
      {:else}
        {@render inline(link.tokens)}
      {/if}
    {:else if token.type === 'image'}
      <span>{(token as Tokens.Image).text}</span>
    {:else if token.type === 'text' && (token as Tokens.Text).tokens}
      {@render inline((token as Tokens.Text).tokens ?? [])}
    {:else}
      {token.type === 'escape' ? (token as Tokens.Escape).text : token.raw}
    {/if}
  {/each}
{/snippet}

{#snippet blocks(items: Token[])}
  {#each items as token}
    {#if token.type === 'heading'}
      {@const heading = token as Tokens.Heading}
      <svelte:element this={`h${Math.min(heading.depth + 1, 6)}`}>
        {@render inline(heading.tokens)}
      </svelte:element>
    {:else if token.type === 'paragraph'}
      <p>{@render inline((token as Tokens.Paragraph).tokens)}</p>
    {:else if token.type === 'text'}
      {@const content = token as Tokens.Text}
      {#if content.tokens}{@render inline(content.tokens)}{:else}{content.text}{/if}
    {:else if token.type === 'blockquote'}
      <blockquote>{@render blocks((token as Tokens.Blockquote).tokens)}</blockquote>
    {:else if token.type === 'code'}
      <pre><code>{(token as Tokens.Code).text}</code></pre>
    {:else if token.type === 'list'}
      {@const list = token as Tokens.List}
      <svelte:element this={list.ordered ? 'ol' : 'ul'} start={list.ordered ? list.start || 1 : undefined}>
        {#each list.items as item}
          <li>{@render blocks(item.tokens)}</li>
        {/each}
      </svelte:element>
    {:else if token.type === 'table'}
      {@const table = token as Tokens.Table}
      <!-- Область прокручивается с клавиатуры, когда таблица шире экрана. -->
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <div class="table-scroll" tabindex="0" role="region" aria-label="Таблица в ответе">
        <table>
          <thead><tr>{#each table.header as cell}<th>{@render inline(cell.tokens)}</th>{/each}</tr></thead>
          <tbody>{#each table.rows as row}<tr>{#each row as cell}<td>{@render inline(cell.tokens)}</td>{/each}</tr>{/each}</tbody>
        </table>
      </div>
    {:else if token.type === 'hr'}
      <hr />
    {:else if token.type !== 'space'}
      <!-- HTML из модели остаётся текстом, без DOM-синков. -->
      <p>{token.raw}</p>
    {/if}
  {/each}
{/snippet}

<style>
  .research-markdown { color: var(--ink-2); font-size: var(--t-body); line-height: var(--lh-body); overflow-wrap: anywhere; }
  .research-markdown :global(p) { margin: 0 0 var(--s4); }
  .research-markdown :global(h2), .research-markdown :global(h3), .research-markdown :global(h4),
  .research-markdown :global(h5), .research-markdown :global(h6) { color: var(--ink); line-height: var(--lh-tight); margin: var(--s6) 0 var(--s3); font-size: var(--t-h4); }
  .research-markdown :global(ul), .research-markdown :global(ol) { margin-block: var(--s3) var(--s4); padding-inline-start: var(--s6); }
  .research-markdown :global(li) { margin-block: var(--s2); }
  .research-markdown :global(blockquote) { margin: var(--s4) 0; padding-inline-start: var(--s4); border-inline-start: 2px solid var(--line); }
  .research-markdown :global(code) { font-family: var(--font-data); font-size: var(--t-small); background: var(--surface-sunk); border-radius: var(--r-sm); padding-inline: var(--s1); }
  .research-markdown :global(pre) { overflow-x: auto; padding: var(--s4); background: var(--surface-sunk); border-radius: var(--r-sm); }
  .research-markdown :global(pre code) { padding: 0; }
  .research-markdown :global(hr) { margin-block: var(--s5); border: 0; border-top: 1px solid var(--line); }
  .citation, .research-markdown :global(a) { color: var(--action-ink); text-decoration: underline; text-underline-offset: 0.2em; }
  .citation { display: inline; padding: 0; border: 0; background: transparent; font: inherit; text-align: inherit; cursor: pointer; }
  .citation:hover { color: var(--ink); }
  .citation:focus-visible, .research-markdown :global(a:focus-visible), .table-scroll:focus-visible { outline: 2px solid var(--action-ink); outline-offset: 3px; border-radius: var(--r-sm); }
  .table-scroll { max-width: 100%; overflow-x: auto; margin-block: var(--s4); }
  table { width: 100%; border-collapse: collapse; font-size: var(--t-small); }
  th, td { border-bottom: 1px solid var(--line); padding: var(--s3); text-align: left; vertical-align: top; }
  th { color: var(--ink); }
  @media (max-width: 640px) { th, td { min-width: 14ch; } }
</style>
