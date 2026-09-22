from __future__ import annotations

from collections import defaultdict, deque
from typing import TypeVar, cast

from pydantic import BaseModel

StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)


class ScriptedProvider:
    mode = "scripted"

    def __init__(self, *outputs: BaseModel) -> None:
        self._outputs: dict[type[BaseModel], deque[BaseModel]] = defaultdict(deque)
        for output in outputs:
            self._outputs[type(output)].append(output)
        self.calls: list[str] = []
        # Промпт — часть контракта рабочего процесса: без него нельзя проверить,
        # что бюджет не выбросил обязательную секцию и что кириллица не сэкранирована.
        self.prompts: dict[str, list[tuple[str, str]]] = defaultdict(list)

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[StructuredOutput],
    ) -> StructuredOutput:
        self.calls.append(schema.__name__)
        self.prompts[schema.__name__].append((system, user))
        output = self._outputs[schema].popleft()
        return cast(StructuredOutput, output.model_copy(deep=True))

    def prompt_for(self, schema: str, index: int = -1) -> str:
        """Пользовательская часть промпта N-го обращения к схеме."""
        return self.prompts[schema][index][1]
