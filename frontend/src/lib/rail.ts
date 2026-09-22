import type { NumericObservation } from './types';

/**
 * Рельс интервалов — керновая шкала: у группы интервалов одна общая шкала,
 * потому что сопоставимы только величины в одной единице. Смешивать мг/л и °C
 * в одном рельсе нельзя, поэтому группировка идёт по единице наблюдения.
 */

export interface IntervalSource {
  observations: NumericObservation[];
}

export interface IntervalRow<T extends IntervalSource> {
  item: T;
  /** Порядковый номер в исходном списке: рельс нумерует интервалы всего ответа. */
  index: number;
  bounds: [number, number] | null;
}

export interface IntervalGroup<T extends IntervalSource> {
  unit: string;
  lo: number;
  hi: number;
  rows: IntervalRow<T>[];
}

const NO_UNIT = 'без единицы';

export function unitOf(source: IntervalSource): string {
  for (const observation of source.observations) {
    const unit = (observation.normalized_unit || observation.unit || '').trim();
    if (unit) return unit;
  }
  return NO_UNIT;
}

export function envelopeOf(source: IntervalSource): [number, number] | null {
  const ranges = source.observations
    .map((observation) => {
      const lo = observation.normalized_min ?? observation.min_value;
      const hi = observation.normalized_max ?? observation.max_value;
      if (lo != null && hi != null) return [lo, hi] as [number, number];
      const point = observation.normalized_value ?? observation.value;
      return point == null ? null : ([point, point] as [number, number]);
    })
    .filter((item): item is [number, number] => item !== null);
  if (!ranges.length) return null;
  return [Math.min(...ranges.map((item) => item[0])), Math.max(...ranges.map((item) => item[1]))];
}

export function groupIntervals<T extends IntervalSource>(items: readonly T[]): IntervalGroup<T>[] {
  const groups = new Map<string, IntervalGroup<T>>();
  items.forEach((item, index) => {
    const unit = unitOf(item);
    let group = groups.get(unit);
    if (!group) {
      group = { unit, lo: Number.NaN, hi: Number.NaN, rows: [] };
      groups.set(unit, group);
    }
    const bounds = envelopeOf(item);
    group.rows.push({ item, index, bounds });
    if (!bounds) return;
    if (!Number.isFinite(group.lo)) {
      group.lo = bounds[0];
      group.hi = bounds[1];
      return;
    }
    group.lo = Math.min(group.lo, bounds[0]);
    group.hi = Math.max(group.hi, bounds[1]);
  });

  for (const group of groups.values()) {
    if (!Number.isFinite(group.lo)) continue;
    // Точка вместо интервала: шкале нужен запас, иначе полоса липнет к краю.
    if (group.hi === group.lo) {
      const pad = Math.abs(group.lo) * 0.1 || 1;
      group.lo -= pad;
      group.hi += pad;
    }
  }
  return [...groups.values()];
}

export function bandOf(
  row: IntervalRow<IntervalSource>,
  group: IntervalGroup<IntervalSource>,
): { left: number; width: number } | null {
  if (!row.bounds || !Number.isFinite(group.lo) || group.hi <= group.lo) return null;
  const span = group.hi - group.lo;
  const left = ((row.bounds[0] - group.lo) / span) * 100;
  const right = ((row.bounds[1] - group.lo) / span) * 100;
  return {
    left: Math.max(0, Math.min(left, 100)),
    width: Math.max(1, Math.min(right - left, 100 - Math.max(left, 0))),
  };
}
