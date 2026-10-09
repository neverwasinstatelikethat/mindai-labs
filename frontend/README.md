# Frontend StormIdea

Интерфейс на SvelteKit 2, Svelte 5 runes и TypeScript. API-запросы и схемы
согласованы с backend через `src/lib/api.ts`; сервер остаётся источником сессии
и прав.

## Маршруты

- `/` — описание продукта и переход к чату или регистрации.
- `/research` — диалог, ответ и проверяемые источники.
- `/findings` — аналитические гипотезы: вывод, основание и следующий шаг.
- `/findings?facet=findings` — находки корпуса: утверждения с привязкой к источнику.
- `/findings?facet=numbers` и `?facet=gaps` — числовые сигналы, расхождения и пробелы.
- `/graph` — связи между материалами.
- `/account` — профиль и доступные действия.

## Разработка

Из каталога `frontend/`:

```bash
npm ci
npm run dev
```

Проверки:

```bash
npm run check
npm run build
npm run check:numbers
```

`check:numbers` нужен для изменений сравнения числовых наблюдений.

## Структура

- `src/routes/` — публичные и рабочие маршруты.
- `src/lib/api.ts` и `src/lib/types.ts` — клиентские контракты.
- `src/lib/terms/` — словари пользовательских текстов.
- `src/lib/ui/` — общие элементы интерфейса.
- `src/app.css` и `src/tokens.css` — общие стили и семантические токены.
