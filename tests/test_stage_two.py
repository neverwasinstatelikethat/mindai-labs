from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import EntityResolutionProposal
from scientific_tangle.evaluation.harness import EvaluationHarness
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from scientific_tangle.services.resolution import EntityResolutionWorkbench


def test_multilingual_gold_retrieval_recall() -> None:
    benchmark = EvaluationHarness().benchmark_retrieval(InMemoryKnowledgeBase())

    assert benchmark.gold_cases == 10
    assert benchmark.top_k == 3
    assert benchmark.leakage_checks["corpus_larger_than_top_k"] is True
    assert benchmark.hybrid.recall_at_3 < 1
    assert not benchmark.passed


def test_entity_merge_is_reviewable_and_reversible() -> None:
    workbench = EntityResolutionWorkbench(Settings(knowledge_backend="memory"))
    workbench.register(
        [
            EntityResolutionProposal(
                mention="reverse osmosis",
                canonical_name="Обратный осмос",
                action="link",
                confidence=0.96,
                rationale="Переводной эквивалент.",
            )
        ]
    )
    proposal = workbench.list_proposals()[0]

    assert workbench.review(proposal.id, "accept").status == "accepted"
    assert workbench.review(proposal.id, "revert").status == "reverted"
