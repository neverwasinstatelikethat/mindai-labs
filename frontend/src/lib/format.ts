const decimal = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });
const tenth = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });

export function num(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? decimal.format(value) : '—';
}

export function pct(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${decimal.format(value * 100)} %` : '—';
}

// Русская числительная: 1 сообщество, 2 сообщества, 5 сообществ.
export function plural(count: number, one: string, few: string, many: string): string {
  const mod10 = Math.abs(count) % 10;
  const mod100 = Math.abs(count) % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

export function countOf(total: number, one: string, few: string, many: string): string {
  return `${new Intl.NumberFormat('ru-RU').format(total)} ${plural(total, one, few, many)}`;
}

// Длительность: время прохода запроса, шаги трассировки, задержка ответа.
// До минуты — десятые доли секунды, дальше — минуты без дробей. Нет показания —
// прочерк, а не «0,0 с»: ноль здесь означал бы мгновенный ответ.
export function duration(ms: number | null | undefined): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return '—';
  if (ms < 60_000) return `${tenth.format(ms / 1000)} с`;
  const minutes = Math.floor(ms / 60_000);
  const rest = Math.round((ms - minutes * 60_000) / 1000);
  return rest > 0 ? `${minutes} мин ${rest} с` : `${minutes} мин`;
}

// Момент в времени пользователя: журнал, «обновлено», версии утверждения.
// Секунды включаются, когда записи идут плотнее минуты. Нераспознанное значение
// остаётся как пришло — страница не показывает вместо него пустоту.
export function dateTime(value: string | Date | null | undefined, withSeconds = false): string {
  const date = value instanceof Date ? value : new Date(value ?? Number.NaN);
  if (Number.isNaN(date.getTime())) return typeof value === 'string' && value ? value : '—';
  return date.toLocaleString('ru-RU', {
    dateStyle: 'short',
    timeStyle: withSeconds ? 'medium' : 'short',
  });
}
