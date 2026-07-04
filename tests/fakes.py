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

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[StructuredOutput],
    ) -> StructuredOutput:
        self.calls.append(schema.__name__)
        output = self._outputs[schema].popleft()
        return cast(StructuredOutput, output.model_copy(deep=True))
