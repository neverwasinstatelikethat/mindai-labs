import fs from 'node:fs';
import path from 'node:path';

const SRC = path.resolve('src/lib/terms.ts');
const OUT = path.resolve('src/lib/terms');
const text = fs.readFileSync(SRC, 'utf8');
const lines = text.split('\n');

const zones = [
  { file: 'shared', start: 1, end: 356 },
  { file: 'map', start: 358, end: 612 },
  { file: 'findings', start: 615, end: 764 },
  { file: 'conflicts', start: 768, end: 1179 },
  { file: 'compare', start: 1183, end: 1426 },
  { file: 'research', start: 1430, end: 2148 },
  { file: 'quality', start: 2152, end: 2400 },
  { file: 'journal', start: 2404, end: 2480 },
  { file: 'merges', start: 2482, end: 2714 },
];

const bodies = new Map();
for (const z of zones) {
  bodies.set(z.file, lines.slice(z.start - 1, z.end).join('\n'));
}

const exported = new Map();
for (const [file, body] of bodies) {
  const names = new Set();
  const re = /^export\s+(?:declare\s+)?(?:const|let|function|type|interface|class)\s+([A-Za-z_$][\w$]*)/gm;
  let m;
  while ((m = re.exec(body))) names.add(m[1]);
  exported.set(file, [...names]);
}

const TYPE_ONLY = new Set(['UnitLabel', 'MapLayer', 'RunStage', 'RunFailureKind', 'ConflictGapKind', 'JournalNoun', 'MergeStatus']);

function words(body) {
  return new Set(body.match(/[A-Za-z_$][\w$]*/g) ?? []);
}

const deps = new Map();
for (const [file, body] of bodies) {
  const used = words(body);
  const own = new Set(exported.get(file));
  const list = [];
  for (const [other, names] of exported) {
    if (other === file) continue;
    const need = names.filter((n) => used.has(n) && !own.has(n));
    if (need.length) list.push({ from: other, need });
  }
  deps.set(file, list);
}

const cycles = [];
for (const [file, list] of deps) {
  for (const d of list) {
    const back = deps.get(d.from) ?? [];
    if (back.some((b) => b.from === file)) {
      cycles.push(`${file} <-> ${d.from}`);
    }
  }
}

fs.mkdirSync(OUT, { recursive: true });

const FORMAT_NAMES = ['countOf', 'num', 'plural', 'duration', 'pct', 'dateTime'];
const TYPE_IMPORTS = [
  'AgentEvent', 'ConflictCandidateStatus', 'DataClass', 'FindingApiStatus',
  'IntentClassification', 'NumericObservation',
];

for (const [file, body] of bodies) {
  const used = words(body);
  const parts = [];
  const fmt = FORMAT_NAMES.filter((n) => used.has(n));
  if (fmt.length) parts.push(`import { ${fmt.join(', ')} } from '../format';`);
  const tys = TYPE_IMPORTS.filter((n) => used.has(n));
  if (tys.length) parts.push(`import type {\n  ${tys.join(',\n  ')},\n} from '../types';`);
  for (const d of deps.get(file) ?? []) {
    const values = d.need.filter((n) => !TYPE_ONLY.has(n));
    const types = d.need.filter((n) => TYPE_ONLY.has(n));
    if (values.length) parts.push(`import { ${values.join(', ')} } from './${d.from}';`);
    if (types.length) parts.push(`import type { ${types.join(', ')} } from './${d.from}';`);
  }
  const header = `/* Зона словаря: ${file}. Вырезана из terms.ts механически, состав не менялся. */\n`;
  const out = parts.length ? `${header}\n${parts.join('\n')}\n\n${body.trim()}\n` : `${header}\n${body.trim()}\n`;
  fs.writeFileSync(path.join(OUT, `${file}.ts`), out);
}

const indexParts = zones.map((z) => `export * from './${z.file}';`);
fs.writeFileSync(
  path.join(OUT, 'index.ts'),
  `/**\n * Единый словарь отображения. Разбит на зоны по экранам: каждый рабочий экран\n * владеет своей зоной и не правит чужую.\n */\n\n${indexParts.join('\n')}\n`,
);

fs.writeFileSync(path.join(OUT, '_report.json'), JSON.stringify({
  counts: Object.fromEntries([...exported].map(([k, v]) => [k, v.length])),
  deps: Object.fromEntries([...deps].map(([k, v]) => [k, v.map((d) => `${d.from}:${d.need.length}`)])),
  cycles: [...new Set(cycles)],
  totalLines: lines.length,
}, null, 2));

console.log('zones written:', zones.length);
console.log('cycles:', [...new Set(cycles)].join(', ') || 'none');
