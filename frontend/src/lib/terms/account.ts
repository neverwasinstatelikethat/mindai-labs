import { JOURNAL_NOUNS, journalShownOf, type JournalNoun } from './journal';

export const EXPERT_RIGHT_LINE =
  'Экспертный признак выдаёт администратор сервиса. В интерфейсе его включить нельзя.';

export const CHECK_NOUNS: JournalNoun = {
  one: 'проверка',
  few: 'проверки',
  many: 'проверок',
  gender: 'f',
};

export function journalIncomplete(noun: JournalNoun): string {
  return `Показаны не все ${noun.few}`;
}

export function shownSentenceOf(read: number, total: number | null, noun: JournalNoun): string {
  const phrase = journalShownOf(read, total, noun);
  return `${phrase.slice(0, 1).toUpperCase()}${phrase.slice(1)}`;
}

export const MY_WORK = {
  heading: 'История решений',
  loading: 'Загружаем решения…',
  failedTitle: 'Решения не загрузились',
  failedBody: 'Повторите загрузку.',
  emptyTitle: 'Решений пока нет',
  emptyBody: 'Принятые решения по материалам появятся здесь.',
} as const;

export const PROFILE = {
  heading: 'Что вам открыто',
  basicTitle: 'Открыто каждому подтверждённому аккаунту',
  expertTitle: 'Открыто с экспертным признаком',
  classesTitle: 'Классы данных',
  classesEmpty: 'Классы данных не открыты: находки корпуса не видны.',
  open: 'открыто',
  closed: 'закрыто',
  goto: 'в раздел',
  nameTitle: 'Отображаемое имя',
  nameHint: 'Имя видно в шапке и в записях вашей работы.',
  editName: 'Изменить имя',
  saveName: 'Сохранить имя',
  savedTitle: 'Имя сохранено',
  savedBody: (name: string): string => `Теперь вы подписаны как ${name}.`,
  nameFailed: 'Имя не сохранено. Проверьте соединение и повторите.',
  sinceTitle: 'В аккаунте с',
  passwordTitle: 'Смена пароля',
  passwordAction: 'Сменить пароль',
  passwordSave: 'Сохранить новый пароль',
  passwordSavedTitle: 'Пароль сохранён',
  passwordSavedBody: 'Этот вход продолжает работу, остальные входы аккаунта закрыты.',
  passwordClear: 'Очистить поля',
  signoutTitle: 'Выход',
  signoutAction: 'Выйти',
  signoutFailed: 'Выход не завершён. Проверьте соединение и повторите.',
  signedOutTitle: 'Нужен вход в аккаунт',
  signedOutBody: 'Настройки аккаунта и история решений доступны после входа.',
} as const;

export const DECISION_ACTION_LABELS: Record<string, string> = {
  'proposal.created': 'Создано предложение правки',
  'proposal.reviewed': 'Решение по предложению',
  'resolution.reviewed': 'Решение по склейке сущностей',
  'claim.superseded': 'Утверждение заменено правкой',
  'answer.exported': 'Ответ выгружен',
  'conflict.reviewed': 'Решение по расхождению',
};

export const DECISION_NOUN: JournalNoun = {
  one: 'решение',
  few: 'решения',
  many: 'решений',
  gender: 'n',
};
