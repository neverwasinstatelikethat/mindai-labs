/* Зона словаря: journal. Вырезана из terms.ts механически, состав не менялся. */

import { countOf } from '../format';

// ── Журналы, у которых есть серверная страница ──────────────────────────────
// Лента приходит страницей (`limit` + `offset`), полное число подходящих записей
// несёт заголовок X-Total-Count, а не длина страницы. Три состояния разведены
// словами: сбой, подтверждённая пустота и неполный список. Показ не называет
// внутреннюю механику выборки: человек видит «показано», «показать ещё» и, если
// список неполный, одну короткую фразу об этом.

export type JournalNoun = { one: string; few: string; many: string; gender?: 'f' | 'm' | 'n' };

// Одна запись в списке: причастие согласуется с родом существительного. Без этого
// «показаны все 1 запись» звучит как сбой склонения на живом экране.
const SHOWN_ONE = { f: 'показана', m: 'показан', n: 'показано' } as const;

export const JOURNAL_NOUNS = {
  entry: { one: 'запись', few: 'записи', many: 'записей', gender: 'f' },
  run: { one: 'прогон', few: 'прогона', many: 'прогонов', gender: 'm' },
  experiment: { one: 'A/B-прогон', few: 'A/B-прогона', many: 'A/B-прогонов', gender: 'm' },
} as const satisfies Record<string, JournalNoun>;

/** Сколько показано и сколько всего: «показаны 50 из 214 записей». Без полного
 *  числа экран говорит «показаны 50 записей» и молчит про неизвестность: это не
 *  показания сервиса, а отсутствие показаний. */
export function journalShownOf(read: number, total: number | null, noun: JournalNoun): string {
  const found = countOf(read, noun.one, noun.few, noun.many);
  if (read === 1) {
    const one = `${SHOWN_ONE[noun.gender ?? 'n']} 1 ${noun.one}`;
    return total === null || total === 1 ? one : `${one} из ${total}`;
  }
  if (total === null) return `показаны ${found}`;
  if (total > read) return `показаны ${found} из ${total}`;
  return `показаны все ${found}`;
}

/** Подпись кнопки: следующие записи по серверному `offset`, а не прокрутка
 *  уже показанного. */
export function journalLoadMoreOf(left: number, pageSize: number, noun: JournalNoun): string {
  return `Показать ещё ${countOf(Math.min(left, pageSize), noun.one, noun.few, noun.many)}`;
}

export const JOURNAL_WINDOW_WORDS = {
  loadingMore: 'Загружаем следующие записи…',
  moreFailed: 'Сервис не ответил: новых записей нет, показанное осталось на экране.',
  // Тупик догрузки: полное число больше показанного, а страница пришла пустой,
  // поэтому кнопка звала бы в никуда. Состояние называется отдельно от сбоя и
  // от «показаны все»: действие убирается, расхождение числа объясняется одной
  // строкой.
  stalledTitle: 'Дальше загружать нечего',
  stalled:
    'Сервис называет больше записей, чем показано, но следующая страница пришла пустой. Показанное остаётся на экране; полное число может опережать хранилище, когда старые строки уже обрезаны.',
} as const;

/** Пустая страница при неизвестном или нулевом полном числе — это незагруженный
 *  журнал, а не пустой; при подтверждённом нуле возвращает null, и экран остаётся
 *  со своей обычной пустотой. `scope` — родительный падеж («ваших действий»,
 *  «корпуса»). */
export function journalUnloaded(
  total: number | null,
  noun: JournalNoun,
  scope: string,
): { title: string; body: string } | null {
  if (total === 0) return null;
  if (total === null) {
    return {
      title: `Журнал ${scope} не загружен`,
      body: 'Записи не пришли, их общее число неизвестно. Повторите загрузку; если повтор даст то же самое, обратитесь к администратору сервиса.',
    };
  }
  return {
    title: `Журнал ${scope} не загружен, хотя записи есть`,
    body: `По сведениям сервиса, это ${countOf(total, noun.one, noun.few, noun.many)}, но страница пришла пустой. Повторите загрузку.`,
  };
}
