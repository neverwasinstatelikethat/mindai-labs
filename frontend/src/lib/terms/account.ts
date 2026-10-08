import { num } from '../format';
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
  heading: 'Ваша работа',
  loading: 'Читаем ваши записи…',
  failedTitle: 'Список не пришёл',
  failedBody: 'Записи не загрузились. Повторите.',
  partialTitle: 'Часть списка не пришла',
  emptyTitle: 'Записей пока нет',
  emptyBody: 'Задайте вопрос или загрузите документ: запись появится здесь после обновления.',
  filterAll: 'Всё',
  filterActivity: 'Действия',
  filterDecisions: 'Решения',
  filterEmpty: 'В этом отборе записей нет. Переключите отбор или обновите список.',
} as const;

export function workFeedLine(
  activity: { read: number; total: number | null },
  decisions: { read: number; total: number | null },
): string {
  const part = (item: { read: number; total: number | null }): string =>
    item.total === null ? num(item.read) : `${num(item.read)} из ${num(item.total)}`;
  return `Действий прочитано ${part(activity)}, решений — ${part(decisions)}.`;
}

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
  signedOutBody: 'Профиль, права, ваша работа и смена пароля появляются здесь после входа.',
} as const;

export const DECISION_ACTION_LABELS: Record<string, string> = {
  'proposal.created': 'Создано предложение правки',
  'proposal.reviewed': 'Решение по предложению',
  'resolution.reviewed': 'Решение по склейке сущностей',
  'claim.superseded': 'Утверждение заменено правкой',
  'answer.exported': 'Ответ выгружен',
  'conflict.reviewed': 'Решение по расхождению',
};

export const ACTIVITY_ACTION_LABELS: Record<string, string> = {
  'auth.register': 'Регистрация аккаунта',
  'auth.login': 'Вход в сервис',
  'auth.logout': 'Выход',
  'auth.password': 'Смена пароля',
  'auth.profile': 'Правка имени',
  'document.ingest': 'Загрузка документа',
  'query.run': 'Вопрос к корпусу',
  'query.stream': 'Вопрос к корпусу',
  'query.cancelled': 'Отмена вопроса',
  'query.stream.cancelled': 'Отмена вопроса',
  'compare.run': 'Сравнение чисел',
  'conflict.review': 'Решение по расхождению',
  'experiment.run': 'Замер предложения',
  'export.run': 'Выгрузка ответа',
  'feedback.submit': 'Отзыв на ответ',
  'feedback.supersede': 'Правка утверждения',
  'proposal.created': 'Предложение по ответу',
  'proposal.review': 'Решение по предложению',
  'resolution.review': 'Решение по слиянию сущностей',
};

export const ACTIVITY_ACTION_FALLBACK = 'Другое действие';

export const ACTIVITY_OUTCOME_LABELS: Record<string, string> = {
  allowed: 'разрешено',
  denied: 'отказано',
  success: 'успех',
  failure: 'сбой',
};

export const ACTIVITY_OUTCOME_UNKNOWN = 'итог не указан';
export const WORK_NOUNS = JOURNAL_NOUNS.entry;

export const WORK_FEED_NOUNS = {
  activity: { one: 'действие', few: 'действия', many: 'действий', gender: 'n' },
  decisions: { one: 'решение', few: 'решения', many: 'решений', gender: 'n' },
} as const satisfies Record<string, JournalNoun>;
