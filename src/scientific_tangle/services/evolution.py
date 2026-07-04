from __future__ import annotations

from uuid import UUID

from scientific_tangle.domain.contracts import (
    EvolutionDraft,
    EvolutionExperiment,
    EvolutionProposal,
    FeedbackRequest,
)
from scientific_tangle.services.provider import ModelProvider

EVOLUTION_SYSTEM = """Ты Self-Evolve Agent платформы MindAI.
По экспертной обратной связи предложи ровно одно безопасное улучшение: prompt, rule, alias
или gold_case. Не изменяй trusted graph и production prompt напрямую. Сформулируй проверяемое
изменение и перечисли затронутые regression areas.
"""


class EvolutionService:
    """LLM формирует proposal, человек решает, публиковать ли его."""

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider
        self._proposals: dict[UUID, EvolutionProposal] = {}
        self._experiments: dict[UUID, EvolutionExperiment] = {}

    async def propose(self, feedback: FeedbackRequest) -> EvolutionProposal:
        draft = await self._provider.complete_model(
            EVOLUTION_SYSTEM,
            feedback.model_dump_json(indent=2),
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
        return proposal

    def list_proposals(self) -> list[EvolutionProposal]:
        return sorted(self._proposals.values(), key=lambda item: item.created_at, reverse=True)

    def review(self, proposal_id: UUID, accepted: bool) -> EvolutionProposal:
        current = self._proposals[proposal_id]
        if accepted and not any(
            item.proposal_id == proposal_id and item.decision == "promote"
            for item in self._experiments.values()
        ):
            raise ValueError("Proposal не прошёл regression A/B gate")
        updated = current.model_copy(update={"status": "accepted" if accepted else "rejected"})
        self._proposals[proposal_id] = updated
        return updated

    def get(self, proposal_id: UUID) -> EvolutionProposal:
        return self._proposals[proposal_id]

    def add_experiment(self, experiment: EvolutionExperiment) -> EvolutionExperiment:
        self._experiments[experiment.id] = experiment
        return experiment

    def list_experiments(self) -> list[EvolutionExperiment]:
        return sorted(self._experiments.values(), key=lambda item: item.created_at, reverse=True)

    def active_policy(self) -> str:
        return "\n\n".join(
            proposal.change
            for proposal in self._proposals.values()
            if proposal.status == "accepted" and proposal.kind in {"prompt", "rule"}
        )
