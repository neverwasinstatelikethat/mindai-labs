from __future__ import annotations

import json
import math
import re
from pathlib import Path
from time import perf_counter
from typing import Any

from scientific_tangle.domain.contracts import (
    AnswerPayload,
    EvaluationMetrics,
    EvaluationRun,
    Finding,
    GoldCase,
    PipelineBenchmark,
    PipelineCaseResult,
    PipelineVariantMetrics,
    QueryRequest,
    RankingMetrics,
    RetrievalBenchmark,
    RetrievalCaseResult,
)
from scientific_tangle.services.knowledge import KnowledgeBase


class EvaluationHarness:
    def __init__(self) -> None:
        self._runs: list[EvaluationRun] = []

    def evaluate(self, answer: AnswerPayload) -> EvaluationRun:
        total_findings = len(answer.findings)
        cited = sum(bool(finding.evidence) for finding in answer.findings)
        citation_coverage = cited / total_findings if total_findings else 0

        numeric_statements = [
            finding for finding in answer.findings if re.search(r"\d", finding.statement)
        ]
        numeric_supported = sum(bool(finding.evidence) for finding in numeric_statements)
        numeric_support = numeric_supported / len(numeric_statements) if numeric_statements else 1.0
        evidence_precision = sum(finding.confidence for finding in answer.findings) / max(
            total_findings, 1
        )
        overall = (citation_coverage + numeric_support + evidence_precision) / 3
        metrics = EvaluationMetrics(
            citation_coverage=round(citation_coverage, 3),
            numeric_support=round(numeric_support, 3),
            evidence_precision=round(evidence_precision, 3),
            overall=round(overall, 3),
        )
        run = EvaluationRun(
            query_id=answer.query_id,
            metrics=metrics,
            passed=(
                metrics.citation_coverage == 1
                and metrics.numeric_support == 1
                and metrics.overall >= 0.8
            ),
        )
        self._runs.append(run)
        return run

    def list_runs(self) -> list[EvaluationRun]:
        return list(reversed(self._runs))

    @staticmethod
    def gold_cases() -> list[GoldCase]:
        path = Path(__file__).parents[1] / "preload_manifest.json"
        cases: list[GoldCase] = []
        for index, item in enumerate(json.loads(path.read_text("utf-8")), start=1):
            source_path = str(item["path"])
            cases.append(
                GoldCase(
                    id=f"real-corpus-{index:02d}",
                    language=item.get("language", "ru"),
                    question=item["gold_question"],
                    source_path=source_path,
                    expected_source_titles=[Path(source_path).stem],
                )
            )
        return cases

    def benchmark_retrieval(self, knowledge: KnowledgeBase) -> RetrievalBenchmark:
        top_k = 3
        gold_cases = self.gold_cases()
        hybrid_rankings: list[tuple[list[str], set[str]]] = []
        lexical_rankings: list[tuple[list[str], set[str]]] = []
        case_results: list[RetrievalCaseResult] = []
        for case in gold_cases:
            expected = set(case.expected_source_titles)
            hybrid_sources = self._source_titles(
                knowledge.rank_findings(case.question, top_k, "hybrid")
            )
            lexical_sources = self._source_titles(
                knowledge.rank_findings(case.question, top_k, "lexical")
            )
            hybrid_rankings.append((hybrid_sources, expected))
            lexical_rankings.append((lexical_sources, expected))
            first_rank = next(
                (rank for rank, source in enumerate(hybrid_sources, start=1) if source in expected),
                None,
            )
            case_results.append(
                RetrievalCaseResult(
                    case_id=case.id,
                    expected_sources=sorted(expected),
                    retrieved_sources=hybrid_sources,
                    reciprocal_rank=round(1 / first_rank, 3) if first_rank else 0,
                )
            )
        hybrid = self._aggregate_ranking_metrics(hybrid_rankings, top_k)
        lexical = self._aggregate_ranking_metrics(lexical_rankings, top_k)
        corpus_documents = knowledge.document_count()
        leakage_checks = {
            "gold_cases_exceed_top_k": len(gold_cases) > top_k,
            "corpus_larger_than_top_k": corpus_documents > top_k,
            "queries_do_not_copy_source_titles": all(
                all(
                    title.lower() not in case.question.lower()
                    for title in case.expected_source_titles
                )
                for case in gold_cases
            ),
            "lexical_baseline_is_reported": True,
        }
        return RetrievalBenchmark(
            gold_cases=len(gold_cases),
            corpus_documents=corpus_documents,
            top_k=top_k,
            hybrid=hybrid,
            lexical_baseline=lexical,
            cases=case_results,
            leakage_checks=leakage_checks,
            passed=(
                all(leakage_checks.values()) and hybrid.recall_at_3 >= 0.6 and hybrid.mrr >= 0.5
            ),
        )

    async def benchmark_pipeline(
        self,
        workflow: Any,
        knowledge: KnowledgeBase,
        max_cases: int = 3,
    ) -> PipelineBenchmark:
        cases = self.gold_cases()[:max_cases]
        metrics, results = await self.evaluate_workflow(workflow, cases)
        lexical_rankings = [
            (
                self._source_titles(knowledge.rank_findings(case.question, 3, "lexical")),
                set(case.expected_source_titles),
            )
            for case in cases
        ]
        lexical = self._aggregate_ranking_metrics(lexical_rankings, 3)
        return PipelineBenchmark(
            cases=len(cases),
            agentic_graphrag=metrics,
            lexical_baseline=lexical,
            results=results,
            passed=(metrics.source_recall >= 0.6 and metrics.citation_coverage == 1),
        )

    async def evaluate_workflow(
        self,
        workflow: Any,
        cases: list[GoldCase],
    ) -> tuple[PipelineVariantMetrics, list[PipelineCaseResult]]:
        recalls: list[float] = []
        citation_coverages: list[float] = []
        latencies: list[float] = []
        passed_count = 0
        results: list[PipelineCaseResult] = []
        for case in cases:
            started = perf_counter()
            answer = await workflow.run(
                QueryRequest(question=case.question, language=case.language, mode="hybrid")
            )
            latency_ms = (perf_counter() - started) * 1000
            sources = self._source_titles(answer.findings)
            expected = set(case.expected_source_titles)
            recall = len(set(sources) & expected) / max(len(expected), 1)
            citation_coverage = sum(bool(item.evidence) for item in answer.findings) / max(
                len(answer.findings), 1
            )
            passed = recall == 1 and citation_coverage == 1
            passed_count += passed
            recalls.append(recall)
            citation_coverages.append(citation_coverage)
            latencies.append(latency_ms)
            results.append(
                PipelineCaseResult(
                    case_id=case.id,
                    question=case.question,
                    expected_sources=sorted(expected),
                    retrieved_sources=sources,
                    latency_ms=round(latency_ms, 1),
                    passed=passed,
                )
            )
        count = max(len(cases), 1)
        return (
            PipelineVariantMetrics(
                source_recall=round(sum(recalls) / count, 3),
                citation_coverage=round(sum(citation_coverages) / count, 3),
                pass_rate=round(passed_count / count, 3),
                average_latency_ms=round(sum(latencies) / count, 1),
            ),
            results,
        )

    @staticmethod
    def _source_titles(findings: list[Finding]) -> list[str]:
        titles: list[str] = []
        for finding in findings:
            for evidence in finding.evidence:
                if evidence.source_title not in titles:
                    titles.append(evidence.source_title)
        return titles

    @staticmethod
    def _aggregate_ranking_metrics(
        rankings: list[tuple[list[str], set[str]]], top_k: int
    ) -> RankingMetrics:
        if not rankings:
            return RankingMetrics(recall_at_3=0, precision_at_3=0, mrr=0, ndcg_at_3=0)
        recalls: list[float] = []
        precisions: list[float] = []
        reciprocal_ranks: list[float] = []
        ndcgs: list[float] = []
        for retrieved, expected in rankings:
            relevant = [1 if source in expected else 0 for source in retrieved[:top_k]]
            recalls.append(sum(relevant) / max(len(expected), 1))
            precisions.append(sum(relevant) / top_k)
            first = next((index for index, value in enumerate(relevant, start=1) if value), None)
            reciprocal_ranks.append(1 / first if first else 0)
            dcg = sum(value / math.log2(index + 1) for index, value in enumerate(relevant, start=1))
            ideal_hits = min(len(expected), top_k)
            idcg = sum(1 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
            ndcgs.append(dcg / idcg if idcg else 0)
        return RankingMetrics(
            recall_at_3=round(sum(recalls) / len(recalls), 3),
            precision_at_3=round(sum(precisions) / len(precisions), 3),
            mrr=round(sum(reciprocal_ranks) / len(reciprocal_ranks), 3),
            ndcg_at_3=round(sum(ndcgs) / len(ndcgs), 3),
        )
