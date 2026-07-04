from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from scientific_tangle.domain.intelligence import PipelineRun, PipelineStage, StageResult

StageHandler = Callable[[UUID], Awaitable[tuple[int, int]]]


class KnowledgeGraphPipeline:
    """Наблюдаемый каркас document → evidence graph с частичными ошибками chunk-level."""

    def __init__(self, handlers: dict[PipelineStage, StageHandler]) -> None:
        self._handlers = handlers
        self._runs_by_checksum: dict[str, PipelineRun] = {}

    async def run(self, content: bytes, correlation_id: str) -> PipelineRun:
        checksum = hashlib.sha256(content).hexdigest()
        existing = self._runs_by_checksum.get(checksum)
        if existing and existing.status == "completed":
            return existing.model_copy(deep=True)

        document_id = uuid5(NAMESPACE_URL, checksum)
        run = PipelineRun(
            document_id=document_id,
            checksum=checksum,
            correlation_id=correlation_id,
        )
        self._runs_by_checksum[checksum] = run

        for stage in PipelineStage:
            handler = self._handlers.get(stage)
            result = StageResult(stage=stage, status="running")
            run.stages.append(result)
            if handler is None:
                result.status = "failed"
                result.message = "Обработчик этапа не настроен."
                run.status = "failed"
                break
            try:
                processed, failed = await handler(document_id)
            except Exception as error:
                result.status = "failed"
                result.message = f"{type(error).__name__}: {error}"
                run.status = "failed"
                break
            result.processed = processed
            result.failed = failed
            result.status = "partial" if failed else "completed"
            if failed:
                run.status = "partial"
        else:
            if run.status == "running":
                run.status = "completed"
        return run.model_copy(deep=True)

    def get(self, checksum: str) -> PipelineRun | None:
        run = self._runs_by_checksum.get(checksum)
        return run.model_copy(deep=True) if run else None
