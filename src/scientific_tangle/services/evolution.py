from __future__ import annotations

from uuid import UUID

from scientific_tangle.domain.contracts import (
    EvolutionDraft,
    EvolutionProposal,
    FeedbackRequest,
)
from scientific_tangle.services.context_budget import to_prompt_json
from scientific_tangle.services.provider import ModelProvider

EVOLUTION_SYSTEM = """Ты Self-Evolve Agent платформы MindAI.
По экспертной обратной связи предложи ровно одно безопасное улучшение: prompt, rule, alias
или gold_case. Не изменяй trusted graph и production prompt напрямую. Сформулируй проверяемое
изменение и перечисли затронутые regression areas.
"""


class EvolutionService:
    """LLM формирует proposal, человек решает, публиковать ли его.

    Хранилище ограничено и упорядочено детерминированно: proposal'ов на каждую
    экспертную реплику хватает на месяцы работы, а активная политика из них
    собирается в порядке, не зависящем от того, в каком порядке их заносили.
    """

    def __init__(self, provider: ModelProvider, capacity: int = 500) -> None:
        self._provider = provider
        self._proposals: dict[UUID, EvolutionProposal] = {}
        self._capacity = capacity

    def _trim(self) -> None:
        # Выгружаем по ключу хранения, а не по id объекта: вызывающий код волен
        # положить запись под другим ключом, и pop по item.id тогда промахнётся.
        # Принятые предложения участвуют в активной политике промпта, поэтому
        # первыми уходят отклонённые и ещё не рассмотренные.
        while len(self._proposals) > self._capacity:
            key, _ = min(
                self._proposals.items(),
                key=lambda entry: (entry[1].status == "accepted", entry[1].created_at),
            )
            self._proposals.pop(key)

    async def propose(self, feedback: FeedbackRequest) -> EvolutionProposal:
        draft = await self._provider.complete_model(
            EVOLUTION_SYSTEM,
            to_prompt_json(feedback),
            EvolutionDraft,
        )
        proposal = EvolutionProposal(
            source_query_id=feedback.query_id,
            kind=draft.kind,
            title=draft.title,
            change=draft.change,
            impact=draft.impact,
        )
        self._proposals[proposal.id] = proposal
        self._trim()
        return proposal

    def review(self, proposal_id: UUID, accepted: bool) -> EvolutionProposal:
        """Меняет статус предложения.

        A/B-ворота проверяет вызывающий слой по серверному состоянию
        (`services/durable_state.py`, таблица `nk_experiments`): эксперименты
        перестали быть словарём процесса, и вороти по локальной копии
        навсегда заперли бы принятие — ни один прогноз не был бы «пройден».
        """
        current = self._proposals[proposal_id]
        updated = current.model_copy(update={"status": "accepted" if accepted else "rejected"})
        self._proposals[proposal_id] = updated
        return updated

    def restore(self, proposal: EvolutionProposal) -> None:
        """Восстанавливает зеркало из серверного состояния при подъёме сервиса.

        Хранилище — `services/durable_state.py` (`nk_evolution_proposals`), а этот
        словарь нужен только чтобы посчитать активную политику промпта; молча
        потерять принятые предложения после рестарта — значит изменить поведение
        агента без всякой причины.
        """
        self._proposals[proposal.id] = proposal
        self._trim()

    def active_policy(self) -> str:
        # Порядок вставки dict'ом сохранялся, поэтому две идентичные очереди
        # принятых предложений давали разный системный промпт.
        accepted = sorted(
            (
                proposal
                for proposal in self._proposals.values()
                if proposal.status == "accepted" and proposal.kind in {"prompt", "rule"}
            ),
            key=lambda proposal: (proposal.created_at, str(proposal.id)),
        )
        return "\n\n".join(proposal.change for proposal in accepted)
