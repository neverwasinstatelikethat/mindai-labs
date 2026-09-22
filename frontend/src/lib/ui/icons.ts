// Иконки нарисованы в одной грамматике: сетка 24×24, штрих 1.6, скруглённые
// окончания; цвет наследуется от текста (stroke=currentColor).
export type IconName =
  | 'search'
  | 'ask'
  | 'upload'
  | 'download'
  | 'graph'
  | 'compare'
  | 'conflict'
  | 'shield'
  | 'gauge'
  | 'doc'
  | 'quote'
  | 'list'
  | 'filter'
  | 'clock'
  | 'check'
  | 'checkCircle'
  | 'close'
  | 'plus'
  | 'minus'
  | 'alert'
  | 'info'
  | 'user'
  | 'users'
  | 'mail'
  | 'lock'
  | 'key'
  | 'logout'
  | 'eye'
  | 'eyeOff'
  | 'refresh'
  | 'link'
  | 'external'
  | 'menu'
  | 'arrowRight'
  | 'arrowUp'
  | 'arrowUpRight'
  | 'chevronDown'
  | 'chevronRight'
  | 'chevronLeft'
  | 'layers'
  | 'sparkles'
  | 'target'
  | 'scale'
  | 'compass'
  | 'pin'
  | 'dot';

export const MARKUP: Record<IconName, string> = {
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/>',
  ask: '<path d="M12 4v3M12 17v3M4 12h3M17 12h3M6.6 6.6l2.1 2.1M15.3 15.3l2.1 2.1M17.4 6.6l-2.1 2.1M8.7 15.3l-2.1 2.1"/><circle cx="12" cy="12" r="2.4"/>',
  upload: '<path d="M12 16V4M7.5 8.5L12 4l4.5 4.5M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
  download: '<path d="M12 4v12M7.5 11.5L12 16l4.5-4.5M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
  graph: '<circle cx="7" cy="7" r="2.6"/><circle cx="17" cy="10" r="2.6"/><circle cx="9.5" cy="17" r="2.6"/><path d="M9.3 8.4l5.3 1.3M8.1 9.4l1.1 5M12 16.4l4.3-4.3"/>',
  compare: '<path d="M12 4v16M6 8h12M4 8l-2 5h4l-2-5ZM20 8l-2 5h4l-2-5Z"/>',
  conflict: '<path d="M4 20L20 4M4 4l16 16"/><circle cx="12" cy="12" r="3.2"/>',
  shield: '<path d="M12 3.5l7 2.6v5.4c0 4.2-2.9 7.5-7 9-4.1-1.5-7-4.8-7-9V6.1l7-2.6Z"/><path d="M9 12l2.2 2.2L15.5 10"/>',
  gauge: '<path d="M4 17a8 8 0 1 1 16 0"/><path d="M12 17l4-5"/><circle cx="12" cy="17" r="1.4"/>',
  doc: '<path d="M6 3.5h7l5 5V20a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1Z"/><path d="M13 3.5V9h5"/><path d="M8.5 13h7M8.5 16.5h4.5"/>',
  quote: '<path d="M9 6.5C6.5 8 5 10.2 5 13v4.5h5V12H7.6C7.9 10 8.6 8.6 9.9 7.7Z"/><path d="M18 6.5c-2.5 1.5-4 3.7-4 6.5v4.5h5V12h-2.4c.3-2 1-3.4 2.3-4.3Z"/>',
  list: '<path d="M8 6.5h12M8 12h12M8 17.5h12M4 6.5h.01M4 12h.01M4 17.5h.01"/>',
  filter: '<path d="M4 5.5h16l-6.2 7v6.2l-3.6-2v-4.2L4 5.5Z"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3.2 2"/>',
  check: '<path d="M4.5 12.5L9.5 17.5 19.5 6.5"/>',
  checkCircle: '<circle cx="12" cy="12" r="8"/><path d="M8.2 12.2l2.6 2.6 5-5.4"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  alert: '<path d="M12 4.5l8.5 15H3.5l8.5-15Z"/><path d="M12 10v4M12 16.8h.01"/>',
  info: '<circle cx="12" cy="12" r="8"/><path d="M12 11.2v5M12 7.8h.01"/>',
  user: '<circle cx="12" cy="8.5" r="3.6"/><path d="M5 20c1-3.6 3.6-5.4 7-5.4s6 1.8 7 5.4"/>',
  users: '<circle cx="9.5" cy="8.5" r="3.2"/><path d="M3.5 19.5c.9-3.2 3.2-4.8 6-4.8s5.1 1.6 6 4.8"/><path d="M16 5.8a3.2 3.2 0 0 1 0 6M18.5 14.4c1.7.7 2.9 2.3 3.5 4.6"/>',
  mail: '<path d="M3.5 6.5h17v11h-17z"/><path d="M3.5 7l8.5 6 8.5-6"/>',
  lock: '<rect x="4.5" y="10.5" width="15" height="10" rx="3"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5"/><path d="M12 14.5v2"/>',
  key: '<circle cx="8" cy="12" r="3.5"/><path d="M11.5 12H21M17.5 12v3.2M20 12v2.2"/>',
  logout: '<path d="M15 4.5H6a1.5 1.5 0 0 0-1.5 1.5v12A1.5 1.5 0 0 0 6 19.5h9"/><path d="M16.5 8.5L20 12l-3.5 3.5M20 12H9.5"/>',
  eye: '<path d="M2.5 12S6 6.5 12 6.5 21.5 12 21.5 12 18 17.5 12 17.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/>',
  eyeOff: '<path d="M4 4l16 16"/><path d="M9.6 6.9A9.8 9.8 0 0 1 12 6.5c6 0 9.5 5.5 9.5 5.5a17 17 0 0 1-2.9 3.4M6.2 8.6A16.6 16.6 0 0 0 2.5 12S6 17.5 12 17.5c1 0 2-.2 2.9-.5"/><path d="M9.6 10.6a2.8 2.8 0 0 0 3.8 3.8"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.6-5.9"/><path d="M20 4v4.2h-4.2"/>',
  link: '<path d="M10 14a4 4 0 0 1 0-5.7l2.2-2.2a4 4 0 0 1 5.7 5.7L16.5 13"/><path d="M14 10a4 4 0 0 1 0 5.7l-2.2 2.2a4 4 0 0 1-5.7-5.7L7.5 11"/>',
  external: '<path d="M14 4.5h5.5V10"/><path d="M19.5 4.5L11 13"/><path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h11"/>',
  arrowRight: '<path d="M4 12h15M13.5 6.5L19 12l-5.5 5.5"/>',
  arrowUp: '<path d="M12 20V5M6.5 10.5L12 5l5.5 5.5"/>',
  arrowUpRight: '<path d="M7 17L17 7M8.5 7H17v8.5"/>',
  chevronDown: '<path d="M6 9.5l6 6 6-6"/>',
  chevronRight: '<path d="M9.5 6l6 6-6 6"/>',
  chevronLeft: '<path d="M14.5 6l-6 6 6 6"/>',
  layers: '<path d="M12 3.5L21 8l-9 4.5L3 8l9-4.5Z"/><path d="M3 12.5l9 4.5 9-4.5"/><path d="M3 17l9 4.5L21 17"/>',
  sparkles: '<path d="M12 4l1.6 4.4L18 10l-4.4 1.6L12 16l-1.6-4.4L6 10l4.4-1.6L12 4Z"/><path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2Z"/>',
  target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.4"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"/>',
  scale: '<path d="M12 4v16M5 20h14M4 9h16"/><path d="M4 9l-1.5 5h3L4 9ZM20 9l-1.5 5h3L20 9Z"/>',
  compass: '<circle cx="12" cy="12" r="8"/><path d="M14.8 9.2l-1.6 4.6-4.6 1.6 1.6-4.6 4.6-1.6Z"/>',
  pin: '<path d="M12 3.5c3.3 0 6 2.6 6 5.8 0 4.2-6 11-6 11s-6-6.8-6-11c0-3.2 2.7-5.8 6-5.8Z"/><circle cx="12" cy="9.4" r="2.3"/>',
  dot: '<circle cx="12" cy="12" r="3.4"/>',
};
