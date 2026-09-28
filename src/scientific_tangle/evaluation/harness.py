"""Измерения качества Научного Клубка: ответы, retrieval и сквозной конвейер.

Пороги измерений
----------------

«Стандарт качества 0.8+» не должен жить устным обещанием: ниже перечислен каждый
порог, константа, которая его задаёт, и то, кто его сегодня реально проверяет.

- ``PASS_TOLERANCE`` = 0.999 — нижняя граница ``citation_coverage`` и
  ``numeric_support`` в ``EvaluationMetrics.passed`` (``EvaluationHarness.evaluate``).
  Проверяется как флаг в ответе ``/api/v1/query`` и в списке ``/api/v1/evaluations``;
  в CI не гейтится.
- ``OVERALL_FLOOR`` = 0.8 — тот самый «0.8+»: композит ``overall`` всё в том же
  ``passed``. Формула композита сохранена прежней, когда судьи нет (это закреплено
  тестом ``tests/test_benchmark_metrics.py``). Автоматического CI-гейта нет: флаг
  виден в интерфейсе оценочного контура и разбирается вручную.
- ``ANSWER_CORRECTNESS_FLOOR`` = 0.8 — детерминированная точность ответа
  (``grade_answer_correctness``). Измеряется только для gold-кейсов, у которых есть
  ``expected_answer`` с подтверждённым источником; в остальных случаях метрика
  равна ``None``, а не 0.0.
- ``JUDGE_QUALITY_FLOOR`` = 0.8 — качество по LLM-судье (``JudgeProtocol``).
  Гейтит ``passed`` только когда судья явно внедрён; без ключа GigaChat в CI его
  нет, и метрика остаётся ``None``.
- Retrieval-бенчмарк (``benchmark_retrieval.passed``): ``RECALL_AT_3_FLOOR`` = 0.6
  и ``MRR_FLOOR`` = 0.5 плюс инварианты корректности замера. Проверяется флагом
  ``passed`` эндпоинта ``/api/v1/benchmark/retrieval``, решение — за экспертом.
- Сквозной конвейер (``benchmark_pipeline.passed``):
  ``PIPELINE_SOURCE_RECALL_FLOOR`` = 0.6, ``citation_coverage >= PASS_TOLERANCE`` и
  ``PIPELINE_P95_LATENCY_MS_FLOOR`` = 120 000 мс. Проверяется флагом ``passed``
  эндпоинта ``/api/v1/benchmark/pipeline``; вручную, так как требует живого LLM.
- CLI ``run_benchmark.py`` (пороги объявлены там же): ``recall@10 >= 0.90``,
  ``p95 <= 3 c``, гибрид лучше лексического базового по MRR и падение headline-метрик
  относительно эталона не больше ``MAX_RELATIVE_REGRESSION`` = 5 %. Это единственный
  порог, который сегодня гейтится автоматически — job ``benchmark`` в
  ``.gitlab-ci.yml`` (``allow_failure: false``). Раннер без корпуса получает код 2:
  «измерение не выполнено», а не зелёный отчёт.
- Точность ответа и судья в CLI не измеряются: ``run_benchmark.py`` — это прогон
  retrieval, а не качества генерации. Кейсов с подтверждённым ``expected_answer``
  сегодня ноль (корпус вне Git), поэтому метрика точности вакуумна и обязан
  отображаться как ``None``, а не как 0.0 или 1.0.

Композитная оценка больше не включает self-reported confidence модели:
«точная» уверенность — это утверждение самой модели, а не измерение поддержки
вывода доказательством. То же правило действует и для судьи:
``mean_finding_confidence`` не входит в ``overall`` ни в одном из режимов.
"""

from __future__ import annotations

import json
import math
import re
from collections import deque
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from pydantic import BaseModel, Field

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

PASS_TOLERANCE = 0.999
OVERALL_FLOOR = 0.8
ANSWER_CORRECTNESS_FLOOR = 0.8
JUDGE_QUALITY_FLOOR = 0.8
RECALL_AT_3_FLOOR = 0.6
MRR_FLOOR = 0.5
PIPELINE_SOURCE_RECALL_FLOOR = 0.6
PIPELINE_P95_LATENCY_MS_FLOOR = 120_000
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_WORD = re.compile(r"[а-яёa-z0-9]+")
# Служебные слова: без них точность ожидаемого ответа не разбавляется «общими»
# токенами, которые есть в любом связном тексте.
_STOPWORDS = frozenset(
    {
        "которые",
        "который",
        "которая",
        "которое",
        "какие",
        "какой",
        "какая",
        "какое",
        "этом",
        "этого",
        "этот",
        "эта",
        "это",
        "для",
        "как",
        "что",
        "чем",
        "или",
        "иначе",
        "будет",
        "были",
        "был",
        "быть",
        "есть",
        "между",
        "among",
        "which",
        "their",
        "from",
        "into",
        "about",
        "with",
        "that",
        "this",
        "than",
        "were",
        "been",
    }
)


class GoldCaseWithAnswer(GoldCase):
    """Gold-кейс с необязательным ожидаемым ответом.

    ``expected_answer`` заполняется ТОЛЬКО вместе с ``expected_answer_source`` —
    указанием файла корпуса и цитатой, откуда этот ответ взят. Без ссылки на
    источник метрика точности не измеряется: правдоподобный, но непроверенный
    ожидаемый ответ измерял бы не качество продукта, а фантазию автора кейса.
    """

    expected_answer: str | None = None
    expected_answer_source: str | None = None


class JudgeVerdict(BaseModel):
    """Вердикт LLM-судьи: качество 0..1 и короткое обоснование по-русски."""

    quality: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)


class JudgeProtocol(Protocol):
    """Соглашение внешнего судьи: (вопрос, ответ, контекст доказательств) → вердикт.

    Судья синхронный — ``evaluate`` вызывается из потока пула (``asyncio.to_thread``).
    Продуктовой реализации намеренно нет: она требует живой ключ GigaChat, а без
    него гейтить ею нельзя.
    """

    def __call__(self, question: str, answer: str, context: list[str]) -> JudgeVerdict: ...


class CorrectnessVerdict(BaseModel):
    """Детерминированная точность ответа относительно ожидаемого утверждения."""

    correctness: float = Field(ge=0, le=1)
    number_support: float = Field(ge=0, le=1)
    claim_support: float = Field(ge=0, le=1)
    missing_numbers: list[str] = Field(default_factory=list)
    missing_claims: list[str] = Field(default_factory=list)
    verified_against: str


class AnswerAssessment(BaseModel):
    """Результат измерения ответа: базовые метрики плюс необязательные метрики.

    ``answer_correctness`` и ``judge_quality`` равны ``None``, когда измерение
    невозможно (нет проверенного ожидаемого ответа / нет судьи), и это честно
    «не измеряли», а не ноль. ``skips`` объясняет каждую пропущенную метрику.
    """

    run: EvaluationRun
    correctness: CorrectnessVerdict | None = None
    judge: JudgeVerdict | None = None
    judge_error: str | None = None
    skips: list[str] = Field(default_factory=list)

    @property
    def answer_correctness(self) -> float | None:
        return None if self.correctness is None else self.correctness.correctness

    @property
    def judge_quality(self) -> float | None:
        return None if self.judge is None else self.judge.quality


class EvaluationHarness:
    """Оценка качества ответов и retrieval.

    Буферы прогонов ограничены: прежний ``list`` рос без потолка на каждый запрос,
    а ``/evaluations`` — это бесконечная утечка памяти в процессе.
    """

    def __init__(
        self,
        metrics: AgentMetricsRegistry | None = None,
        max_runs: int = 200,
        judge: JudgeProtocol | None = None,
    ) -> None:
        self._runs: deque[EvaluationRun] = deque(maxlen=max_runs)
        self._metrics = metrics or agent_metrics
        # Судья внедряется извне (экспертный прогон, офлайн-оценка). По умолчанию
        # его нет: продукт поднимается без ключа GigaChat, и метрика судьи тогда
        # отсутствует, а не обнуляется.
        self._judge = judge

    def evaluate(
        self,
        answer: AnswerPayload,
        *,
        case: GoldCase | None = None,
        judge: JudgeProtocol | None = None,
    ) -> EvaluationRun:
        """Базовый промер ответа: возвращает только ``EvaluationRun``.

        Необязательные метрики (точность по ожидаемому ответу, LLM-судья) видны в
        ``assess``; здесь они влияют на ``overall``/``passed`` ровно в том объёме,
        в каком реально измерены.
        """
        return self.assess(answer, case=case, judge=judge).run

    def assess(
        self,
        answer: AnswerPayload,
        *,
        case: GoldCase | None = None,
        judge: JudgeProtocol | None = None,
    ) -> AnswerAssessment:
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

        correctness = grade_answer_correctness(answer, case)
        judge_verdict, judge_error = self._call_judge(answer, judge)
        skips: list[str] = []
        if correctness is None:
            skips.append(correctness_skip_reason(case))
        if judge_verdict is None:
            skips.append(
                f"LLM-судья не измерен: {judge_error}"
                if judge_error
                else "LLM-судья не внедрён — метрика судьи отсутствует (не 0.0)."
            )

        grounded_ratio = 1.0 - unsupported
        # Композит без судьи оставлен прежним — иначе «0.8+» тихо сменил бы смысл.
        # Когда судья есть и измерен, его оценка становится четвёртым компонентом;
        # mean_finding_confidence не входит ни в одном из режимов.
        components = [citation_coverage, numeric_support, grounded_ratio]
        if judge_verdict is not None:
            components.append(judge_verdict.quality)
        overall = sum(components) / len(components)
        metrics = EvaluationMetrics(
            citation_coverage=round(citation_coverage, 3),
            numeric_support=round(numeric_support, 3),
            unsupported_claim_ratio=round(unsupported, 3),
            mean_finding_confidence=round(mean_confidence, 3),
            overall=round(overall, 3),
        )
        passed = (
            metrics.citation_coverage >= PASS_TOLERANCE
            and metrics.numeric_support >= PASS_TOLERANCE
            and metrics.unsupported_claim_ratio <= 1 - PASS_TOLERANCE
            and metrics.overall >= OVERALL_FLOOR
        )
        # Дополнительные гейты включаются только когда метрика измерена: непроверенный
        # кейс или отсутствие судьи не должны ни спасать, ни валить прогон молча.
        if correctness is not None:
            passed = passed and correctness.correctness >= ANSWER_CORRECTNESS_FLOOR
        if judge_verdict is not None:
            passed = passed and judge_verdict.quality >= JUDGE_QUALITY_FLOOR
        run = EvaluationRun(query_id=answer.query_id, metrics=metrics, passed=passed)
        self._runs.append(run)
        return AnswerAssessment(
            run=run,
            correctness=correctness,
            judge=judge_verdict,
            judge_error=judge_error,
            skips=skips,
        )

    def _call_judge(
        self,
        answer: AnswerPayload,
        judge: JudgeProtocol | None,
    ) -> tuple[JudgeVerdict | None, str | None]:
        """Зовёт судью; его сбой — это «не измерено», а не ноль в метрике."""
        active = judge or self._judge
        if active is None:
            return None, None
        context = [
            f"{evidence.source_title}: {evidence.quote}"
            for finding in answer.findings
            for evidence in finding.evidence
        ]
        try:
            verdict = active(answer.question, _answer_text(answer), context)
            if isinstance(verdict, JudgeVerdict):
                return verdict, None
            return JudgeVerdict.model_validate(verdict), None
        except Exception as exc:
            # Сбой судьи — это «метрика не измерена» с явной причиной: обнулять её
            # значило бы наказать прогон за чужую недоступность.
            return None, f"судья вернул ошибку {type(exc).__name__}: {exc}"

    def list_runs(self) -> list[EvaluationRun]:
        return list(reversed(self._runs))

    @staticmethod
    @lru_cache(maxsize=1)
    def gold_cases() -> list[GoldCase]:
        """Gold-кейсы корпуса. Манифест читается один раз за процесс.

        ``expected_answer`` берётся из манифеста, только если там же лежит
        ``expected_answer_source`` (файл корпуса и цитата). Сегодня ни один кейс
        так не подтверждён: папка «Источники информации/» вне Git и в этом
        checkout пуста, поэтому метрика точности для gold-кейсов не измеряется.
        """
        path = Path(__file__).parents[1] / "preload_manifest.json"
        cases: list[GoldCase] = []
        for index, item in enumerate(json.loads(path.read_text("utf-8")), start=1):
            source_path = str(item["path"])
            cases.append(
                GoldCaseWithAnswer(
                    id=f"real-corpus-{index:02d}",
                    language=item.get("language", "ru"),
                    question=item["gold_question"],
                    source_path=source_path,
                    expected_source_titles=[Path(source_path).stem],
                    expected_answer=item.get("expected_answer"),
                    expected_answer_source=item.get("expected_answer_source"),
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
                all(validity_checks.values())
                and hybrid.recall_at_3 >= RECALL_AT_3_FLOOR
                and hybrid.mrr >= MRR_FLOOR
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
                metrics.source_recall >= PIPELINE_SOURCE_RECALL_FLOOR
                and metrics.citation_coverage >= PASS_TOLERANCE
                and metrics.p95_latency_ms <= PIPELINE_P95_LATENCY_MS_FLOOR
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


def _answer_text(answer: AnswerPayload) -> str:
    """Текст ответа для сверки и для судьи: сводка, тезисы и цитаты доказательств."""
    parts = [answer.summary]
    parts.extend(finding.statement for finding in answer.findings)
    parts.extend(evidence.quote for finding in answer.findings for evidence in finding.evidence)
    return " ".join(part for part in parts if part)


def _term_prefixes(text: str) -> set[str]:
    """Значимые слова в виде префиксов по пять символов.

    Формы одного слова («шлак», «шлаками», «шлаковый») сходятся к одному префиксу,
    поэтому сверка формулировки не требует словаря словоформ и остаётся
    детерминированной на обычном stdlib.
    """
    prefixes: set[str] = set()
    for token in _WORD.findall(text.lower()):
        if len(token) < 4 or token in _STOPWORDS:
            continue
        prefixes.add(token[:5])
    return prefixes


def _expected_pair(case: GoldCase | None) -> tuple[str | None, str | None]:
    """(ожидаемый ответ, источник, им подтверждённый) либо пустая пара."""
    if not isinstance(case, GoldCaseWithAnswer):
        return None, None
    return case.expected_answer, case.expected_answer_source


def grade_answer_correctness(
    answer: AnswerPayload,
    case: GoldCase | None,
) -> CorrectnessVerdict | None:
    """Точность ответа к ожидаемому утверждению: числа + формулировка.

    Работает только для кейсов, у которых ``expected_answer`` подтверждён
    ``expected_answer_source``: правдоподобный, но непроверенный ожидаемый ответ
    измерял бы фантазию автора кейса, а не продукт. Остальные кейсы возвращают
    ``None`` — метрика для них вакуумна, и это честно отличается от нуля.

    Числа сверяются по значению через ``_canonical_numbers`` (тот же подход, что у
    ``_numbers_grounded``: «70» подтверждается записью «70,0», «10 000» = «10000»).
    """
    expected, source = _expected_pair(case)
    if not expected or not source:
        return None
    text = _answer_text(answer)
    needed_numbers = _canonical_numbers(expected)
    found_numbers = _canonical_numbers(text)
    needed_terms = _term_prefixes(expected)
    found_terms = _term_prefixes(text)
    if not needed_numbers and not needed_terms:
        return None
    number_support = (
        1.0
        if not needed_numbers
        else len(needed_numbers & found_numbers) / len(needed_numbers)
    )
    claim_support = (
        1.0 if not needed_terms else len(needed_terms & found_terms) / len(needed_terms)
    )
    return CorrectnessVerdict(
        correctness=round((number_support + claim_support) / 2, 3),
        number_support=round(number_support, 3),
        claim_support=round(claim_support, 3),
        missing_numbers=sorted(needed_numbers - found_numbers),
        missing_claims=sorted(needed_terms - found_terms),
        verified_against=source,
    )


def correctness_skip_reason(case: GoldCase | None) -> str:
    """Почему точность не измерена — пропуск обязан называть причину."""
    if case is None:
        return "Точность ответа не измерена: gold-кейс не передан."
    expected, source = _expected_pair(case)
    if not expected:
        return (
            f"Точность ответа не измерена: у кейса {case.id} нет expected_answer — "
            "метрика вакуумна, а не нулевая."
        )
    if not source:
        return (
            f"Точность ответа не измерена: expected_answer кейса {case.id} не подтверждён "
            "файлом корпуса и цитатой (expected_answer_source) — проверять нечем."
        )
    return f"Точность ответа кейса {case.id} не измерена: ожидаемое утверждение пусто."

