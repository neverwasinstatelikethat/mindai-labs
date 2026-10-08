/**
 * Поведенческая проверка арифметики раздела «Числа».
 *
 * Типы и сборка не доказывают, что экран не объявит расхождением честную пару
 * чисел. Здесь ровно то, на что опираются все экраны сразу: `boundsOf` берёт
 * нормализованную величину (иначе полоса в одной единице показывает число в
 * другой), односторонний предел остаётся без сравнения (иначе «не меньше 95» и
 * «96–97» сходятся в мнимый разрыв), а `gapBetween` считает расстояние только
 * между непересекающимися полосами.
 *
 * Запуск: npm run check:numbers   (из frontend/)
 */

import { build } from 'esbuild';
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';

const outFile = path.resolve('.tmp-numbers-behavior.mjs');

await build({
  entryPoints: [path.resolve('src/lib/numbers.ts')],
  bundle: true,
  format: 'esm',
  outfile: outFile,
  logLevel: 'error',
});

const m = await import(pathToFileURL(outFile).href);
fs.rmSync(outFile, { force: true });

const obs = (o) => ({ property_name: 'p', unit: '%', raw_text: '', operator: 'between', ...o });

let failed = 0;
function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failed += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : `: получено ${JSON.stringify(got)}, ожидалось ${JSON.stringify(want)}`}`);
}

check('диапазон читается обоими краями', m.boundsOf(obs({ min_value: 95, max_value: 97 })), { lo: 95, hi: 97 });
check('нормализованное важнее сырого', m.boundsOf(obs({ min_value: 95, max_value: 97, normalized_min: 1, normalized_max: 2 })), { lo: 1, hi: 2 });
check('точка читается', m.boundsOf(obs({ operator: 'eq', value: 8, min_value: null, max_value: null })), { lo: 8, hi: 8 });
check('односторонний предел остаётся без сравнения', m.boundsOf(obs({ min_value: 95, max_value: null, value: null })), null);
check('нет ни числа, ни краёв', m.boundsOf(obs({ min_value: null, max_value: null, value: null })), null);

check('общая точка не расхождение', m.gapBetween({ lo: 0, hi: 10 }, { lo: 10, hi: 20 }), null);
check('пересечение не расхождение', m.gapBetween({ lo: 90, hi: 100 }, { lo: 95, hi: 120 }), null);
check('разрыв считается', m.gapBetween({ lo: 95, hi: 97 }, { lo: 90, hi: 94 }), 1);
check('разрыв считается в другую сторону', m.gapBetween({ lo: 90, hi: 94 }, { lo: 95, hi: 97 }), 1);

check('нулевой знаменатель не подменяется', m.spreadRatio(0, 5), null);
check('знаменатель размаза назван', m.spreadRatio(95, 97), { ratio: 2 / 95, denominator: 95 });
check('разница при нулевой базе называет причину', m.deltaFromBase(0, 5), { ok: false, miss: 'zero' });

console.log(failed === 0 ? '\nарифметика сходится: все проверки пройдены' : `\nпровалов: ${failed}`);
process.exit(failed === 0 ? 0 : 1);
