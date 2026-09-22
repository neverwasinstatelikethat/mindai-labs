from __future__ import annotations

import json
import math
import re
from collections import deque
from functools import lru_cache
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
from scientific_tangle.services.agent_metrics import AgentMetricsRegistry, agent_metrics
from scientific_tangle.services.knowledge import KnowledgeBase

# Композитная оценка больше не включает self-reported confidence модели:
# «точная» уверенность — это утверждение самой модели, а не измерение поддержки
# вывода доказательством.
PASS_TOLERANCE = 0.999
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


class EvaluationHarness:
    """Оценка качества ответов и retrieval.

    Буферы прогонов ограничены: прежний ``list`` рос без потолка на каждый запрос,
    а ``/evaluations`` — это бесконечная утечка памяти в процессе.
    """

    def __init__(
        self,
        metrics: AgentMetricsRegistry | None = None,
        max_runs: int = 200,
    ) -> None:
        self._runs: deque[EvaluationRun] = deque(maxlen=max_runs)
        self._metrics = metrics or agent_metrics

    def evaluate(self, answer: AnswerPayload) -> EvaluationRun:
        findings = answer.findings
        total = len(findings)
        cited = sum(bool(finding.evidence) for finding in findings)
        citation_coverage = cited / total if total else 0.0

        numeric_findings = [finding for finding in findings if _NUMBER.search(finding.statement)]
        numeric_supported = sum(bool(finding.evidence) for finding in numeric_findings)
        numeric_support = numeric_supported / len(numeric_findings) if numeric_findings else 1.0
        unsupported = (
            sum(1 for finding in findings if not _numbers_grounded(finding)) / total
            if total
            else 0.0
        )
        mean_confidence = sum(finding.confidence for finding in findings) / total if total else 0.0
        metrics = EvaluationMetrics(
            citation_coverage=round(citation_coverage, 3),
            numeric_support=round(numeric_support, 3),
            unsupported_claim_ratio=round(unsupported, 3),
            mean_finding_confidence=round(mean_confidence, 3),
            overall=round((citation_coverage + numeric_support + (1.0 - unsupported)) / 3, 3),
        )
        run = EvaluationRun(
            query_id=answer.query_id,
            metrics=metrics,
            passed=(
                metrics.citation_coverage >= PASS_TOLERANCE
                and metrics.numeric_support >= PASS_TOLERANCE
                and metrics.unsupported_claim_ratio <= 1 - PASS_TOLERANCE
                and metrics.overall >= 0.8
            ),
        )
        self._runs.append(run)
        return run

    def list_runs(self) -> list[EvaluationRun]:
        return list(reversed(self._runs))

    @staticmethod
    @lru_cache(maxsize=1)
    def gold_cases() -> list[GoldCase]:
        """Gold-кейсы корпуса. Манифест читается один раз за процесс."""
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
        available_titles: set[str] = set()
        for finding in knowledge.all_findings():
            available_titles.update(evidence.source_title for evidence in finding.evidence)
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
        # Кейс засчитываем только если его ожидаемый источник лежит в текущем
        # корпусе: на in-memory бэкенде источники gold-кейсов отсутствуют, и
        # recall=0 там означает «измерять нечего», а не «поиск плохой».
        scored_cases = sum(
            1 for _, expected in hybrid_rankings if expected <= available_titles
        )
        # Инварианты корректности замера. Прежний набор назывался «утечками» и
        # включал условие «gold-кейсов больше, чем top_k», которое истинно всегда,
        # — из-за чего benchmark проваливался независимо от качества поиска.
        validity_checks = {
            "expected_sources_present_in_corpus": scored_cases == len(gold_cases),
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
            scored_cases=scored_cases,
            corpus_documents=corpus_documents,
            top_k=top_k,
            hybrid=hybrid,
            lexical_baseline=lexical,
            cases=case_results,
            validity_checks=validity_checks,
            passed=(
                all(validity_checks.values()) and hybrid.recall_at_3 >= 0.6 and hybrid.mrr >= 0.5
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
            passed=(
                metrics.source_recall >= 0.6
                and metrics.citation_coverage >= PASS_TOLERANCE
                and metrics.p95_latency_ms <= 120_000
            ),
        )

    async def evaluate_workflow(
        self,
        workflow: Any,
        cases: list[GoldCase],
    ) -> tuple[PipelineVariantMetrics, list[PipelineCaseResult]]:
        """Кейсы выполняются последовательно, каждый со своим окном метрик.

        Прежний замер брал снимок глобального реестра до и после параллельного
        gather: при сравнении вариантов A/B в одном процессе (или при любом другом
        трафике) токены и repair-повторы чужих запросов попадали в метрики
        варианта. Последовательное окно на кейс атрибутирует каждый счётчик
        своему кейсу, поэтому сравнение вариантов остаётся честным.
        """
        outcomes: list[dict[str, Any]] = []
        for case in cases:
            before = self._metrics.snapshot()
            outcome = await self._run_case(workflow, case)
            after = self._metrics.snapshot()
            outcome["tokens"] = max(
                after.total_prompt_tokens
                + after.total_completion_tokens
                - before.total_prompt_tokens
                - before.total_completion_tokens,
                0,
            )
            # Повторы считаются по обращениям к модели (schema-repair), а не по
            # агентам: у агента нет retry-политики, и метрика «повторов на кейс»
            # из агрегата узлов всегда была бы нулём. Варианты A/B сравнивают
            # именно это число — вариант промпта, который чаще требует починки
            # ответа, хуже.
            outcome["retries"] = max(
                sum(item.retries for item in after.llm)
                - sum(item.retries for item in before.llm),
                0,
            )
            outcomes.append(outcome)

        recalls: list[float] = []
        coverages: list[float] = []
        latencies: list[float] = []
        results: list[PipelineCaseResult] = []
        passed_count = 0
        tokens = 0
        retries = 0
        for outcome in outcomes:
            recalls.append(outcome["recall"])
            coverages.append(outcome["citation_coverage"])
            latencies.append(outcome["latency_ms"])
            passed_count += int(outcome["passed"])
            tokens += outcome["tokens"]
            retries += outcome["retries"]
            results.append(
                PipelineCaseResult(
                    case_id=outcome["case_id"],
                    question=outcome["question"],
                    expected_sources=outcome["expected_sources"],
                    retrieved_sources=outcome["retrieved_sources"],
                    latency_ms=outcome["latency_ms"],
                    degradation_reasons=outcome["degradation_reasons"],
                    passed=outcome["passed"],
                )
            )
        count = max(len(cases), 1)
        ordered = sorted(latencies)
        return (
            PipelineVariantMetrics(
                source_recall=round(sum(recalls) / count, 3),
                citation_coverage=round(sum(coverages) / count, 3),
                pass_rate=round(passed_count / count, 3),
                average_latency_ms=round(sum(latencies) / count, 1),
                p95_latency_ms=(
                    round(ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)], 1)
                    if ordered
                    else 0.0
                ),
                retries_per_case=round(retries / count, 3),
                total_tokens=tokens,
            ),
            results,
        )

    @staticmethod
    async def _run_case(workflow: Any, case: GoldCase) -> dict[str, Any]:
        started = perf_counter()
        answer = await workflow.run(
            QueryRequest(question=case.question, language=case.language, mode="hybrid")
        )
        latency_ms = (perf_counter() - started) * 1000
        sources = EvaluationHarness._source_titles(answer.findings)
        expected = set(case.expected_source_titles)
        recall = len(set(sources) & expected) / max(len(expected), 1)
        citation_coverage = sum(bool(item.evidence) for item in answer.findings) / max(
            len(answer.findings), 1
        )
        return {
            "case_id": case.id,
            "question": case.question,
            "expected_sources": sorted(expected),
            "retrieved_sources": sources,
            "recall": recall,
            "citation_coverage": citation_coverage,
            "latency_ms": round(latency_ms, 1),
            "degradation_reasons": answer.degradation_reasons,
            "passed": recall == 1 and citation_coverage == 1 and not answer.degradation_reasons,
        }

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


def _canonical_numbers(text: str) -> set[str]:
    """Числа текста в форме сравнения: запятая — десятичный разделитель, пробел —
    разделитель тысяч («10 000» и «10000» — одно число)."""
    numbers: set[str] = set()
    without_thousands = re.sub(r"(?<=\d) (?=\d{3}(?:\D|$))", "", text)
    for raw in _NUMBER.findall(without_thousands):
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            continue
        numbers.add(str(int(value)) if value.is_integer() else str(value))
    return numbers


def _numbers_grounded(finding: Finding) -> bool:
    """Проверяет числовую fidelity: каждое число statement подтверждено доказательством.

    Семантика совпадает с guardrail рабочего процесса (``agents/workflow.py``):
    доказательны не только цитаты, но и числовые наблюдения тезиса — их
    ``raw_text`` и границы. Отказать им в доказательности значило бы штрафовать
    тезис за число, которое Improver добавил из формализованного условия, а не
    выдумал. Сравнение по значению, а не по строке: «70» подтверждается и
    записью «70,0».
    """
    supported = _canonical_numbers(" ".join(e.quote for e in finding.evidence))
    supported |= _canonical_numbers(
        " ".join(observation.raw_text for observation in finding.observations)
    )
    for observation in finding.observations:
        supported |= {
            str(int(value)) if value.is_integer() else str(value)
            for value in (
                observation.value,
                observation.min_value,
                observation.max_value,
                observation.normalized_value,
                observation.normalized_min,
                observation.normalized_max,
            )
            if value is not None
        }
    return _canonical_numbers(finding.statement) <= supported
