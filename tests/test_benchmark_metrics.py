"""Метрики retrieval-бенчмарка: recall, hit и NDCG считаются по определениям.

Тесты нужны потому, что корпус (`Источники информации/`) на этой машине отсутствует,
и прогон `run_benchmark.py` даёт лишь «нет findings». Ошибку в формуле это не
поймало бы, а здесь она видна напрямую.

Вторая часть файла гейтит сам CLI (`main` возвращает код, а не печатает отчёт),
регрессионный контроль с эталоном и метрики качества ответа: точность по
проверенному ожидаемому ответу, LLM-судью, совпадение содержимого цитаты
(`citation_content_match`) и полноту дифференциаторов (`conflict_recall`,
`gap_recall`). Корпуса в CI нет, поэтому CLI проверяется на двойнике корпуса из
`.txt`-двойников: имена файлов дают те же заголовки, что и `REAL_GOLD_CASES`.

Третья часть держит две инвариантности оценки, за которые продукт отвечает перед
аналитиком: пустой ответ не получает долю за то, что проверять было нечего, и
метрики, которые нечего измерять, попадают в `skips` с названной причиной, а не в
0.0 или 1.0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest

from scientific_tangle.domain.contracts import AnswerPayload, Finding, GoldCase
from scientific_tangle.domain.models import EvidenceLocator
from scientific_tangle.evaluation import run_benchmark as bench
from scientific_tangle.evaluation.gold_cases import (
    REAL_GOLD_CASES,
    RealGoldCase,
    reconcile_gold_cases,
)
from scientific_tangle.evaluation.harness import (
    MIN_SAMPLES_FOR_P95,
    OVERALL_FLOOR,
    EvaluationHarness,
    GoldCaseWithAnswer,
    JudgeVerdict,
    grade_answer_correctness,
    grade_citation_content_match,
    grade_differential_recall,
    latency_sample_report,
)
from scientific_tangle.evaluation.run_benchmark import (
    TOP_K_VALUES,
    aggregate,
    aggregate_samples,
    compute_case_metrics,
)

TOP_K = TOP_K_VALUES


def _f(source: str) -> Finding:
    """Находка, трассированная к одному именованному источнику."""
    return Finding(
        id=f"finding-{source}",
        statement=f"Тезис: {source}",
        confidence=0.8,
        evidence=[
            EvidenceLocator(
                document_id=uuid5(UUID(int=1), source),
                source_title=source,
                page=1,
                quote="Обратный осмос даёт 70 % обессоливания",
            )
        ],
    )


def test_recall_counts_missed_sources_not_only_the_first_hit() -> None:
    """Из двух ожидаемых найден один — это recall 0.5, а не 1.0 (прежний hit-rate)."""
    metrics = compute_case_metrics([_f("A")], {"A", "B"}, TOP_K)
    assert metrics["recall@3"] == 0.5
    assert metrics["hit@3"] == 1.0


def test_perfect_retrieval_scores_one_everywhere() -> None:
    metrics = compute_case_metrics([_f("A"), _f("B"), _f("C")], {"A", "B", "C"}, TOP_K)
    assert metrics["recall@3"] == 1.0
    assert metrics["precision@3"] == 1.0
    assert metrics["mrr"] == 1.0
    assert metrics["ndcg@3"] == 1.0


def test_ndcg_penalizes_sources_that_never_appeared() -> None:
    """Идеал берётся по известным релевантным: один найденный из трёх не может быть 1.0."""
    metrics = compute_case_metrics([_f("A"), _f("X"), _f("Y")], {"A", "B", "C"}, TOP_K)
    assert metrics["hit@3"] == 1.0
    assert metrics["ndcg@3"] < 0.6
    assert metrics["recall@3"] == 1 / 3


def test_mrr_uses_first_relevant_rank() -> None:
    metrics = compute_case_metrics([_f("X"), _f("A"), _f("B")], {"A", "B"}, TOP_K)
    assert metrics["mrr"] == 0.5
    assert metrics["precision@3"] == 2 / 3


def test_nothing_retrieved_is_zero_not_an_error() -> None:
    metrics = compute_case_metrics([], {"A", "B"}, TOP_K)
    assert metrics["recall@3"] == 0.0
    assert metrics["hit@3"] == 0.0
    assert metrics["mrr"] == 0.0
    assert metrics["ndcg@3"] == 0.0


def test_empty_expectation_does_not_divide_by_zero() -> None:
    metrics = compute_case_metrics([_f("A")], set(), TOP_K)
    assert metrics["recall@3"] == 0.0
    assert metrics["ndcg@3"] == 0.0


# ── Композит качества ответа: формула закреплена, судья внедряется ──────


def _answer(
    statements: list[str],
    *,
    confidence: float = 0.4,
    quotes: list[str] | None = None,
) -> AnswerPayload:
    """Ответ, где цитата по умолчанию равна тезису (базовая четвёрка метрик = 1.0).

    Цитата равна тезису, поэтому ``citation_coverage``, ``numeric_support``,
    ``unsupported_claim_ratio`` и ``citation_content_match`` дают ровно 1.0 — и
    любое отклонение `overall` объясняется только добавленной метрикой. `quotes`
    позволяет подложить цитату «не про это» и увидеть расхождение двух мер.
    """
    pairs = list(
        zip(statements, quotes if quotes is not None else statements, strict=True)
    )
    findings = [
        Finding(
            id=f"finding-{index}",
            statement=statement,
            confidence=confidence,
            evidence=[
                EvidenceLocator(
                    document_id=uuid5(UUID(int=2), statement),
                    source_title="Обеднение_шлаков",
                    page=1,
                    quote=quote,
                )
            ],
        )
        for index, (statement, quote) in enumerate(pairs)
    ]
    return AnswerPayload.model_construct(
        query_id=uuid4(),
        question="Какие параметры влияют на обеднение шлаков?",
        summary=" ".join(statements),
        findings=findings,
        conflicts=[],
        knowledge_gaps=[],
        degradation_reasons=[],
    )


class _StubJudge:
    """Внедряемый судья: считает контекст и возвращает вердикт (или падает)."""

    def __init__(self, quality: float = 0.95, error: Exception | None = None) -> None:
        self.quality = quality
        self.error = error
        self.calls: list[tuple[str, str, list[str]]] = []

    def __call__(self, question: str, answer: str, context: list[str]) -> JudgeVerdict:
        self.calls.append((question, answer, context))
        if self.error is not None:
            raise self.error
        return JudgeVerdict(quality=self.quality, rationale="Числа опираются на цитаты корпуса.")


def test_overall_is_the_mean_of_measured_components_without_judge() -> None:
    """Без судьи композит = среднее измеренных метрик трассировки; смысл «0.8+» тот же.

    Четвёртая компонента — ``citation_content_match``: на ответе, где цитата равна
    тезису, она равна 1.0 и число не меняется, но знаменатель теперь честный
    (было бы «три метрики из четырёх измеренных»).
    """
    answer = _answer(["Обеднение шлака даёт 70 % извлечения меди"])
    assessment = EvaluationHarness().assess(answer)
    run = assessment.run
    assert run.metrics.citation_coverage == 1.0
    assert run.metrics.numeric_support == 1.0
    assert run.metrics.unsupported_claim_ratio == 0.0
    assert assessment.citation_content_match == 1.0
    assert assessment.components == 4
    assert run.metrics.overall == round((1.0 + 1.0 + 1.0 + 1.0) / 4, 3) == 1.0
    assert run.passed is True


def test_empty_answer_is_not_credited_for_having_nothing_to_check() -> None:
    """Пустой ответ не получает 0.667 за вакуум: метрики не измерены, прогон не принят.

    ``numeric_support`` и ``unsupported_claim_ratio`` при нуле находок были 1.0 и
    0.0 — «все числа подтверждены», «неподтверждённых нет», потому что проверять
    было нечего. Теперь это ``skips`` с названной причиной, ``overall`` не выше
    правды (0.0) и ``passed=False``.
    """
    empty = AnswerPayload.model_construct(
        query_id=uuid4(),
        question="Какие параметры влияют на обеднение шлаков?",
        summary="",
        findings=[],
        conflicts=[],
        knowledge_gaps=[],
        degradation_reasons=[],
    )
    assessment = EvaluationHarness().assess(empty)

    assert assessment.findings == 0
    assert assessment.components == 0
    assert assessment.run.metrics.overall == 0.0
    assert assessment.run.metrics.citation_coverage == 0.0
    assert assessment.run.metrics.numeric_support == 0.0
    assert assessment.citation_content_match is None
    assert assessment.run.passed is False
    # Причина названа, а не выведена из молчаливого нуля.
    assert any("ноль находок" in note for note in assessment.skips)
    assert any("нечего принимать" in note for note in assessment.skips)


def test_unrelated_quote_keeps_coverage_but_loses_content_match() -> None:
    """Цитата «не про это» даёт 1.0 по наличию и 0.0 по содержимому — обе метрики видны.

    Обещание продукта — тезис трассирован к ЦИТАТЕ, поэтому ``citation_coverage``
    (``bool(finding.evidence)``) больше не может быть единственной мерой: иначе
    ответ с приложенной невпопад цитатой проходил бы порог 0.999.
    """
    finding = Finding(
        id="finding-1",
        statement="Обеднение шлака даёт 70 % извлечения меди",
        confidence=0.9,
        evidence=[
            EvidenceLocator(
                document_id=uuid5(UUID(int=3), "Обеднение_шлаков"),
                source_title="Обеднение_шлаков",
                page=4,
                quote="Шлак состоит из силикатов и флюорита",
            )
        ],
    )
    answer = AnswerPayload.model_construct(
        query_id=uuid4(),
        question="Какие параметры влияют на обеднение шлаков?",
        summary="Обеднение шлака даёт 70 % извлечения меди",
        findings=[finding],
        conflicts=[],
        knowledge_gaps=[],
        degradation_reasons=[],
    )
    assessment = EvaluationHarness().assess(answer)

    assert assessment.run.metrics.citation_coverage == 1.0
    assert assessment.citation_content_match == 0.0
    assert assessment.content_match is not None
    assert assessment.content_match.missing == ["finding-1"]
    assert assessment.run.passed is False
    # Непроверенное содержимое цитаты тянет композит вниз даже при полной «наличной»
    # трассировке: (1.0 + 1.0 + 0.0 + 0.0) / 4.
    assert assessment.run.metrics.overall == 0.5


def test_content_match_normalizes_number_forms_and_thousand_separators() -> None:
    """Числа сверяются по значению: «70» подтверждается «70,0», «10 000» равно «10000»."""
    verdict = grade_citation_content_match(
        _answer(
            [
                "Расход 10 000 м3/ч при температуре 70,0 °C",
            ],
            quotes=["расход раствора 10000 м3/ч, температура 70 °C"],
        )
    )
    assert verdict is not None
    assert verdict.ratio == 1.0
    assert verdict.missing == []


def test_content_match_requires_document_and_address_in_locator() -> None:
    """Локатор без имени источника и без адреса — не трассировка, даже с цитатой."""
    finding = Finding(
        id="finding-blank",
        statement="Обеднение шлака даёт 70 % извлечения меди",
        confidence=0.9,
        evidence=[
            EvidenceLocator(
                document_id=uuid4(),
                source_title="   ",
                sheet="Лист1",
                quote="Обеднение шлака даёт 70 % извлечения меди",
            )
        ],
    )
    verdict = grade_citation_content_match(
        AnswerPayload.model_construct(
            query_id=uuid4(),
            question="вопрос",
            summary="",
            findings=[finding],
            conflicts=[],
            knowledge_gaps=[],
            degradation_reasons=[],
        )
    )
    assert verdict is not None
    assert verdict.ratio == 0.0


def test_content_match_skipped_for_empty_answer() -> None:
    assert grade_citation_content_match(_answer([])) is None


def test_judge_is_an_extra_component_and_gates_passed() -> None:
    """Судья добавляется в композит явно и гейтит `passed`, только когда измерен."""
    answer = _answer(["Обеднение шлака даёт 70 % извлечения меди"])

    strict = EvaluationHarness().assess(answer, judge=_StubJudge(quality=0.5))
    assert strict.judge is not None
    assert strict.judge_quality == 0.5
    # Компоненты: цитаты, числа, подтверждённость выводов, содержимое цитат, судья.
    assert strict.components == 5
    assert strict.run.metrics.overall == round((1.0 + 1.0 + 1.0 + 1.0 + 0.5) / 5, 3)
    assert strict.run.passed is False

    ok = EvaluationHarness().assess(answer, judge=_StubJudge(quality=0.9))
    assert ok.run.metrics.overall == round((1.0 + 1.0 + 1.0 + 1.0 + 0.9) / 5, 3)
    assert ok.run.passed is True
    assert ok.run.metrics.overall >= OVERALL_FLOOR


def test_judge_receives_question_answer_and_evidence_context() -> None:
    judge = _StubJudge()
    EvaluationHarness().assess(_answer(["Обеднение шлака даёт 70 % извлечения меди"]), judge=judge)
    question, answer_text, context = judge.calls[0]
    assert question.startswith("Какие параметры")
    assert "70 %" in answer_text
    assert context == ["Обеднение_шлаков: Обеднение шлака даёт 70 % извлечения меди"]


def test_broken_judge_is_not_measured_instead_of_zero() -> None:
    """Сбой судьи — отсутствие метрики с причиной, а не 0.0, винящий прогон."""
    answer = _answer(["Обеднение шлака даёт 70 % извлечения меди"])
    broken = _StubJudge(error=RuntimeError("нет ключа"))
    assessment = EvaluationHarness().assess(answer, judge=broken)
    assert assessment.judge is None
    assert assessment.judge_quality is None
    assert assessment.judge_error is not None and "нет ключа" in assessment.judge_error
    # Композит остался прежним: неотработавший судья не роняет и не спасает оценку.
    assert assessment.run.metrics.overall == 1.0
    assert any("не измерен" in note for note in assessment.skips)


def test_harness_without_judge_leaves_metric_absent() -> None:
    assessment = EvaluationHarness().assess(_answer(["Обеднение шлака даёт 70 % извлечения меди"]))
    assert assessment.judge is None
    assert assessment.judge_quality is None
    assert any("не внедрён" in note for note in assessment.skips)


def test_injected_harness_judge_is_used_without_per_call_argument() -> None:
    judge = _StubJudge(quality=0.3)
    assessment = EvaluationHarness(judge=judge).assess(
        _answer(["Обеднение шлака даёт 70 % извлечения меди"])
    )
    assert judge.calls
    assert assessment.judge is not None and assessment.judge.quality == 0.3
    assert assessment.run.passed is False


def test_self_reported_confidence_never_enters_composite() -> None:
    """Уверенность модели исключена из композита и без судьи, и с судьёй.

    Она по-прежнему отдельно видна в `mean_finding_confidence` — но на `overall`
    и `passed` не влияет ни при каком режиме.
    """
    low = _answer(["Обеднение шлака даёт 70 % извлечения меди"], confidence=0.05)
    high = _answer(["Обеднение шлака даёт 70 % извлечения меди"], confidence=0.99)
    low_run = EvaluationHarness().evaluate(low)
    high_run = EvaluationHarness().evaluate(high)
    assert low_run.metrics.mean_finding_confidence == 0.05
    assert high_run.metrics.mean_finding_confidence == 0.99
    assert low_run.metrics.overall == high_run.metrics.overall
    assert low_run.passed == high_run.passed

    judge = _StubJudge(quality=0.9)
    assert (
        EvaluationHarness().assess(low, judge=judge).run.metrics.overall
        == EvaluationHarness().assess(high, judge=judge).run.metrics.overall
    )


# ── Точность ответа: метрика живёт только у проверенных кейсов ──────────


def _case(
    expected_answer: str | None = None,
    expected_answer_source: str | None = None,
    expected_conflicts: list[str] | None = None,
    expected_gaps: list[str] | None = None,
) -> GoldCaseWithAnswer:
    return GoldCaseWithAnswer(
        id="real-corpus-01",
        language="ru",
        question="Какие параметры влияют на обеднение шлаков?",
        source_path="Обзоры/Обеднение_шлаков.docx",
        expected_source_titles=["Обеднение_шлаков"],
        expected_answer=expected_answer,
        expected_answer_source=expected_answer_source,
        expected_conflicts=expected_conflicts or [],
        expected_gaps=expected_gaps or [],
    )


def test_correctness_absent_for_case_without_expected_answer() -> None:
    answer = _answer(["Обеднение шлака даёт 70 % извлечения меди"])
    assessment = EvaluationHarness().assess(answer, case=_case())
    assert assessment.answer_correctness is None
    assert any("нет expected_answer" in note for note in assessment.skips)


def test_correctness_refuses_expected_answer_without_source() -> None:
    """Ожидаемый ответ без ссылки на первоисточник не измеряется: иначе метрика
    подтверждала бы выдумку, а не корпус."""
    answer = _answer(["Обеднение шлака даёт 70 % извлечения меди"])
    case = _case(expected_answer="Обеднение шлака даёт 70 % извлечения меди")
    assert grade_answer_correctness(answer, case) is None
    assessment = EvaluationHarness().assess(answer, case=case)
    assert assessment.answer_correctness is None
    assert any("не подтверждён" in note for note in assessment.skips)
    assert assessment.run.passed is True


def test_correctness_plain_goldcase_has_no_answer_field() -> None:
    answer = _answer(["Обеднение шлака даёт 70 % извлечения меди"])
    plain = GoldCase(
        id="c",
        language="ru",
        question="вопрос",
        source_path="Обзоры/Обеднение_шлаков.docx",
        expected_source_titles=["Обеднение_шлаков"],
    )
    assert grade_answer_correctness(answer, plain) is None


def test_correctness_scores_numbers_and_claims_of_expected_statement() -> None:
    expected = "Обеднение шлака даёт 70 % извлечения меди при 1350 °C"
    case = _case(
        expected_answer=expected,
        expected_answer_source="Обеднение_шлаков.docx, лист 3: «…при 1350 °C…»",
    )
    exact = _answer([expected])
    verdict = grade_answer_correctness(exact, case)
    assert verdict is not None
    assert verdict.correctness == 1.0
    assert verdict.missing_numbers == []

    dropped = _answer(["Обеднение шлака даёт 70 % извлечения меди"])
    partial = grade_answer_correctness(dropped, case)
    assert partial is not None
    assert partial.number_support == 0.5
    assert "1350" in partial.missing_numbers
    assert partial.correctness < 0.8

    assessment = EvaluationHarness().assess(dropped, case=case)
    assert assessment.run.passed is False
    assert assessment.correctness is not None


# ── Дифференциаторы: противоречия и пробелы ─────────────────────────────


def _differential_answer(
    conflicts: list[str],
    gaps: list[str],
) -> AnswerPayload:
    """Ответ с названными противоречиями и пробелами (тезисы те же, что в `_answer`)."""
    answer = _answer(["Обеднение шлака даёт 70 % извлечения меди"])
    return answer.model_copy(update={"conflicts": conflicts, "knowledge_gaps": gaps})


def test_conflict_and_gap_recall_measured_from_expectations() -> None:
    """Полнота дифференциаторов считается по ожиданиям кейса и наказывается за промах."""
    case = _case(
        expected_conflicts=[
            "извлечение меди 70 % при обжиге против 95 % при электролизе",
            "содержание серы 0.3 % против 0.5 %",
        ],
        expected_gaps=["нет данных по расходу реагента для сподумена"],
    )
    answer = _differential_answer(
        conflicts=["Обжиг даёт извлечение меди 70 %, электролиз — 95 %"],
        gaps=["Расход реагента для сподумена в корпусе не приведён"],
    )

    verdict = grade_differential_recall(answer, case)
    assert verdict is not None
    assert verdict.conflict_recall == 0.5
    assert verdict.gap_recall == 1.0
    assert verdict.missed_conflicts == ["содержание серы 0.3 % против 0.5 %"]

    assessment = EvaluationHarness().assess(answer, case=case)
    assert assessment.conflict_recall == 0.5
    assert assessment.gap_recall == 1.0
    # Промах по противоречиям валит прогон, даже когда трассировка тезисов идеальная.
    assert assessment.run.passed is False


def test_conflict_and_gap_recall_are_skipped_without_corpus_expectations() -> None:
    """Без корпуса дифференциаторы не измеримы: skips с «не измерено», а не 0.0.

    Иначе пустой «Источники информации/» валил бы любой прогон за «не найденные
    противоречия», которых никто не обещал, и метрика стала бы шумом.
    """
    answer = _answer(["Обеднение шлака даёт 70 % извлечения меди"])
    assessment = EvaluationHarness().assess(answer, case=_case())

    assert assessment.conflict_recall is None
    assert assessment.gap_recall is None
    assert grade_differential_recall(answer, _case()) is None
    assert any(
        "conflict_recall" in note and "не измерены" in note for note in assessment.skips
    )
    # Пропуск метрики не меняет композит: прогон по-прежнему принят.
    assert assessment.run.passed is True


def test_differential_recall_for_plain_goldcase_is_not_measured() -> None:
    """Контрактный ``GoldCase`` ожиданий не выражает — метрика обязана быть пропущена."""
    plain = GoldCase(
        id="plain-1",
        language="ru",
        question="вопрос",
        source_path="Обзоры/Обеднение_шлаков.docx",
        expected_source_titles=["Обеднение_шлаков"],
    )
    answer = _answer(["Обеднение шлака даёт 70 % извлечения меди"])
    assert grade_differential_recall(answer, plain) is None
    notes = EvaluationHarness().assess(answer, case=plain).skips
    assert any("контракт GoldCase" in note for note in notes)


def test_differential_verdict_gates_passed_only_when_measured() -> None:
    """Пустые ожидания не могут «спасти» прогон и не могут его валить молча."""
    case = _case(expected_conflicts=["температура 1350 °C против 1280 °C"])
    silent = _differential_answer(conflicts=[], gaps=[])
    assessment = EvaluationHarness().assess(silent, case=case)
    assert assessment.conflict_recall == 0.0
    assert assessment.run.passed is False
    assert assessment.gap_recall is None  # пробелов не ждали — метрики нет


# ── Два носителя gold-кейсов: сверка наборов ────────────────────────────


def test_reconcile_gold_cases_names_divergence() -> None:
    """Сверка называет расхождение, а не выбирает «первый попавшийся» эталон."""
    manifest = [
        {"path": "Обзоры/Обеднение_шлаков.docx", "gold_question": "Вопрос А?"},
        {"path": "Обзоры/Очистка от Fe 2020.docx", "gold_question": "Вопрос Б?"},
    ]
    real = [
        # совпадение по источнику и вопросу
        RealGoldCase(query="Вопрос А?", source_documents=["Обеднение_шлаков"], category="c"),
        # тот же источник, другой вопрос
        RealGoldCase(
            query="Как удаляют Fe?",
            source_documents=["Очистка от Fe 2020"],
            category="c",
        ),
        # источник, которого в манифесте нет
        RealGoldCase(query="Новый вопрос?", source_documents=["Сподумен"], category="c"),
    ]

    report = reconcile_gold_cases(manifest, real)
    # Оба источника манифеста покрыты кейсами, поэтому matched — про источники,
    # а не про пары «файл + точная формулировка».
    assert report.matched == 2
    assert report.manifest_total == 2
    assert report.benchmark_total == 3
    assert report.balanced is False
    assert "Обеднение_шлаков" in report.matched_sources
    assert report.manifest_only == (), "источник с другим вопросом всё ещё покрыт"
    assert "Новый вопрос?" in report.benchmark_only
    assert any("Очистка от Fe 2020" in line for line in report.describe())


def test_second_question_to_same_source_is_not_an_orphan() -> None:
    """Вопросов у файла может быть больше одного — сверка не должна это штрафовать.

    Манифест хранит по одному вопросу на документ, бенчмарк задаёт к тому же
    файлу второй. Прежняя паровка 1:1 по формулировке выдавала за это сразу две
    противоположные диагностики: «источник не покрыт» и «кейс без пары в
    манифесте». Проверка держит контракт: покрыт — значит покрыт, а лишние
    вопросы видны по числам, не по ложным сиротам.
    """
    manifest = [{"path": "Обзоры/A.docx", "gold_question": "Вопрос манифеста?"}]
    real = [
        RealGoldCase(query="Вопрос манифеста?", source_documents=["A"], category="c"),
        RealGoldCase(query="Второй вопрос к A?", source_documents=["A"], category="c"),
    ]

    report = reconcile_gold_cases(manifest, real)

    assert report.matched == 1
    assert report.manifest_only == ()
    assert report.benchmark_only == ()
    assert report.question_mismatches == ()
    assert report.balanced is True
    assert report.benchmark_total == 2, "числа всё равно показывают два вопроса"


def test_reconcile_gold_cases_accepts_equal_sets() -> None:
    """Совпадающие наборы не порождают диагностик: гейт не должен кричать впустую."""
    manifest = [{"path": "Обзоры/A.docx", "gold_question": "Вопрос?"}]
    real = [RealGoldCase(query="Вопрос?", source_documents=["A"], category="c")]
    report = reconcile_gold_cases(manifest, real)
    assert report.balanced is True
    assert report.describe() == [
        "наборы gold-кейсов совпадают: 1 источник(ов), манифест 1, бенчмарк 1"
    ]


def test_baseline_writer_output_passes_the_ci_gate(tmp_path: Path) -> None:
    """Файл эталона обязан проходить проверки job'а `benchmark:baseline-gate`.

    Гейт принимает только прогон версии 1, только принятый (`accepted is true`) и
    только с непустой плоской картой `headline`. Если писатель сменит схему —
    переименует `headline`, перестанет проставлять `accepted`, отдаст строку вместо
    числа, — первый настоящий прогон на корпусе упрётся в «сравнить не с чем», и
    узнаем мы об этом ровно тогда, когда корпуса под рукой уже не будет. Поэтому
    контракт писателя и гейта проверяется здесь, синтетическим прогоном без
    притязаний на качество продукта.
    """
    from scientific_tangle.evaluation.run_benchmark import (
        PAYLOAD_VERSION,
        build_payload,
        load_baseline,
        write_payload,
    )

    payload = build_payload(
        hybrid={"recall@10": 0.95, "mrr": 0.8},
        lexical={"recall@10": 0.6, "mrr": 0.5},
        case_details=[{"query": "Вопрос?", "hybrid_recall": 1.0}],
        latencies=[0.1, 0.2],
        doc_count=1,
        findings_count=2,
        source_root=tmp_path,
        criteria={},
        regressions=None,
        baseline_path=None,
        max_relative=0.05,
    )
    # `main()` проставляет этот признак перед записью эталона.
    payload["accepted"] = True
    path = tmp_path / "baseline.json"
    write_payload(path, payload)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1 == PAYLOAD_VERSION
    assert data["accepted"] is True
    headline = data["headline"]
    assert headline and all(isinstance(value, (int, float)) for value in headline.values())
    assert data["corpus"]["gold_cases"] == 1
    assert data["corpus"]["documents"] == 1
    # Тот же читатель, что работает при сверке регрессий, обязан принять файл.
    assert load_baseline(path)["headline"] == headline

    # Проверка с зубами: чужая форма эталона обязана отвергаться, иначе гейт
    # принимает любой JSON и «сравнить не с чем» превращается в «падений нет».
    no_headline = dict(data, headline={})
    path.write_text(json.dumps(no_headline, ensure_ascii=False), "utf-8")
    with pytest.raises(ValueError, match="headline"):
        load_baseline(path)

    other_version = dict(data, version=PAYLOAD_VERSION + 1)
    path.write_text(json.dumps(other_version, ensure_ascii=False), "utf-8")
    with pytest.raises(ValueError, match="версии"):
        load_baseline(path)

    # Пустое пересечение измеряемых метрик и эталона обязано читаться как
    # «сверка не выполнена», а не как «падений нет».
    from scientific_tangle.evaluation.run_benchmark import compare_with_baseline

    assert compare_with_baseline({"hybrid_new": 0.9}, {"headline": {"hybrid_old": 0.9}}, 0.05, {})


def test_run_with_nothing_checked_is_not_accepted() -> None:
    """Ноль проверенных критериев — это «не измеряли», а не «приёмка подтверждена».

    Итог принимает `all()` по проверенным критериям, а на пустом списке `all()`
    истинен: прогон без единой проверки получил бы статус принятого и был записан
    эталоном, после чего все последующие сравнения шли бы против него.
    """
    from scientific_tangle.evaluation.run_benchmark import acceptance_passed

    unchecked = {"recall": {"passed": True, "checked": False}}
    assert acceptance_passed(unchecked) is False
    assert acceptance_passed({}) is False
    assert acceptance_passed({"p95": {"passed": True, "checked": False}}) is False
    # Один проверенный критерий возвращает смысл: он и есть измерение.
    assert acceptance_passed({"p95": {"passed": True, "checked": True}}) is True
    assert acceptance_passed({"p95": {"passed": False, "checked": True}}) is False


def test_real_gold_cases_diverge_from_manifest_and_say_so() -> None:
    """Актуальное расхождение двух носителей зафиксировано тестом, а не забыто.

    В манифесте 10 кейсов, в ``REAL_GOLD_CASES`` — 12, и формулировка вопроса про
    хлорное выщелачивание различается. Это значит, что «recall по gold-кейсам» в
    CLI и в ``/api/v1/benchmark/retrieval`` считаются по разным наборам; тест
    ловит любое новое расхождение (добавили кейс, поменяли вопрос) и требует
    назвать его, а не делать вид, что эталон один.
    """
    report = EvaluationHarness.reconcile_gold_cases()

    assert report.manifest_total == len(EvaluationHarness.gold_cases()) == 10
    assert report.benchmark_total == len(REAL_GOLD_CASES) == 12
    # Известное расхождение одно: у хлорного выщелачивания бенчмарк спрашивает
    # «…никеля?», а манифест — без уточнения. Два «лишних» кейса при этом
    # задают вторые вопросы к уже покрытым файлам, а не висят сиротами.
    assert report.matched == 10
    assert report.manifest_only == ()
    assert report.benchmark_only == ()
    assert report.balanced is False
    assert len(report.question_mismatches) == 1
    assert any("хлорного выщелачивания" in line for line in report.describe())
    # Каждый заголовок бенчмарка обязан существовать в манифесте: новый источник без
    # файла корпуса — это не gold-кейс, а невыполнимое измерение.
    manifest_titles = {
        Path(str(item["path"])).stem
        for item in json.loads(
            (Path(__file__).parents[1] / "src" / "scientific_tangle" / "preload_manifest.json")
            .read_text("utf-8")
        )
    }
    assert {title for case in REAL_GOLD_CASES for title in case.source_documents} == (
        manifest_titles
    )


# ── Выборка: n у каждого числа и честный p95 ───────────────────────────


def test_latency_report_refuses_pseudo_percentile_on_three_samples() -> None:
    """p95 из трёх замеров — не перцентиль: отдаётся наихудший прогон с оговоркой."""
    report = latency_sample_report([100.0, 200.0, 900.0])

    assert report.samples == 3
    assert report.is_percentile is False
    assert report.p95_ms == 900.0
    assert "недостаточная выборка" in report.note

    enough = latency_sample_report([100.0, 200.0, 300.0, 400.0, 900.0])
    assert enough.samples == 5
    assert enough.is_percentile is True
    assert enough.p95_ms == 900.0
    assert enough.note == "p95 по n=5 замеров."

    nothing = latency_sample_report([])
    assert nothing.samples == 0
    assert nothing.p95_ms == 0.0
    assert "ни одного" in nothing.note


def test_run_benchmark_latency_marks_small_sample() -> None:
    """CLI той же мерой честит малую выборку: p95 не выдаётся за перцентиль."""
    small = bench.latency_stats([0.1, 0.2, 0.9])
    assert small.samples == 3
    assert small.percentile_measured is False
    assert small.p95 == pytest.approx(0.9)
    assert "недостаточная выборка" in small.note

    big = bench.latency_stats([float(value) / 10 for value in range(MIN_SAMPLES_FOR_P95 + 3)])
    assert big.percentile_measured is True
    assert big.samples == MIN_SAMPLES_FOR_P95 + 3


def test_aggregate_reports_the_sample_behind_every_mean() -> None:
    """Среднее по переменному числу кейсов: знаменатель — свои кейсы, и n виден."""
    case_metrics = [
        {"recall@10": 1.0, "mrr": 1.0},
        {"recall@10": 0.0, "mrr": 0.5},
        {"recall@10": 1.0},
    ]
    assert aggregate(case_metrics) == {"mrr": 0.75, "recall@10": 0.667}
    assert aggregate_samples(case_metrics) == {"recall@10": 3, "mrr": 2}
    assert aggregate([]) == {}


# ── CLI-гейт: коды выхода, эталон и сырые метрики ──────────────────────


def _fake_corpus(tmp_path: Path) -> tuple[Path, Path]:
    """Двойник корпуса: по .txt-файлу на каждый gold-заголовок + свой манифест.

    Заголовок документа = имя файла без расширения, поэтому stems совпадают с
    `source_documents` из `REAL_GOLD_CASES` и retrieval-бенчмарк считает по-настоящему.
    """
    titles = sorted({title for case in REAL_GOLD_CASES for title in case.source_documents})
    sources = tmp_path / "sources"
    manifest: list[dict[str, str]] = []
    for index, title in enumerate(titles):
        relative = f"Обзоры/{title}.txt"
        path = sources / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "Обедение шлаков при плавке конвертора зависит от основности шлака и "
            "температуры 1350 °C. Выщелачивание никеля и кобальта из раствора даёт "
            f"сульфат кобальта с извлечением {90 + index} %. Обработка сподумена "
            "и электроэкстракция меди в холодном климате. " * 2,
            encoding="utf-8",
        )
        manifest.append(
            {"path": relative, "language": "ru", "gold_question": REAL_GOLD_CASES[0].query}
        )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return sources, manifest_path


def _run(sources: Path, manifest: Path, *extra: str) -> int:
    return bench.main(
        [
            "--source-root",
            str(sources),
            "--manifest",
            str(manifest),
            *extra,
        ]
    )


_PERMISSIVE = (
    "--min-recall-at-10",
    "0",
    "--max-p95-seconds",
    "600",
    "--no-require-hybrid-beats-lexical",
)


def test_cli_exits_2_and_hides_report_without_corpus(tmp_path: Path, capsys) -> None:
    """Недоступный корпус — код 2 и явное «измерение не выполнено», без отчёта."""
    _, manifest = _fake_corpus(tmp_path)
    json_out = tmp_path / "bench.json"
    code = _run(tmp_path / "нет-корпуса", manifest, "--json-out", str(json_out))
    printed = capsys.readouterr().out
    assert code == bench.EXIT_NOT_MEASURED == 2
    assert "ИЗМЕРЕНИЕ НЕ ВЫПОЛНЕНО" in printed
    assert "КРИТЕРИИ ПРИЁМКИ" not in printed
    assert not json_out.exists()


def test_cli_exits_2_when_manifest_is_missing(tmp_path: Path, capsys) -> None:
    sources, _ = _fake_corpus(tmp_path)
    code = _run(sources, tmp_path / "нет-манифеста.json")
    assert code == bench.EXIT_NOT_MEASURED
    assert "манифест" in capsys.readouterr().out


def test_cli_exits_2_when_corpus_is_partial(tmp_path: Path, capsys) -> None:
    """Половина корпуса — тоже «не измеряли»: recall по урезанному корпусу
    нельзя сравнивать ни с порогом, ни с эталоном."""
    sources, manifest = _fake_corpus(tmp_path)
    victim = sources / f"Обзоры/{REAL_GOLD_CASES[0].source_documents[0]}.txt"
    victim.unlink()
    code = _run(sources, manifest, *_PERMISSIVE)
    printed = capsys.readouterr().out
    assert code == bench.EXIT_NOT_MEASURED
    assert "корпус неполный" in printed
    assert "ИЗМЕРЕНИЕ НЕ ВЫПОЛНЕНО" in printed


def test_cli_exits_0_and_writes_raw_metrics(tmp_path: Path) -> None:
    sources, manifest = _fake_corpus(tmp_path)
    json_out = tmp_path / "reports" / "bench.json"
    code = _run(sources, manifest, *_PERMISSIVE, "--json-out", str(json_out))
    assert code == bench.EXIT_OK == 0
    payload: dict[str, Any] = json.loads(json_out.read_text("utf-8"))
    assert payload["version"] == bench.PAYLOAD_VERSION
    assert payload["accepted"] is True
    assert payload["corpus"]["gold_cases"] == len(REAL_GOLD_CASES)
    assert payload["corpus"]["findings"] > 0
    assert payload["metrics"]["hybrid"]["recall@10"] >= 0.0
    assert payload["headline"]["p95_seconds"] >= 0.0
    assert payload["regression"]["checked"] is False
    # Честность: ни один кейс не имеет ожидаемого ответа, подтверждённого корпусом.
    assert payload["corpus"]["answer_correctness_cases"] == 0


def test_cli_exits_1_when_acceptance_threshold_is_not_met(tmp_path: Path, capsys) -> None:
    sources, manifest = _fake_corpus(tmp_path)
    code = _run(sources, manifest, "--min-recall-at-10", "1.01")
    printed = capsys.readouterr().out
    assert code == bench.EXIT_NOT_ACCEPTED == 1
    assert "приёмка НЕ подтверждена" in printed
    assert "✗" in printed


def test_cli_baseline_round_trip_has_no_regression(tmp_path: Path) -> None:
    sources, manifest = _fake_corpus(tmp_path)
    first = tmp_path / "first.json"
    baseline = tmp_path / "baseline.json"
    assert _run(sources, manifest, *_PERMISSIVE, "--json-out", str(first),
                "--write-baseline", str(baseline)) == bench.EXIT_OK
    stored = json.loads(baseline.read_text("utf-8"))
    # Латентность от прогона к прогону отличается, поэтому в эталоне её завышают:
    # сравниваем детерминированные quality-метрики.
    for key in bench.LOWER_IS_BETTER:
        stored["headline"][key] = 1e6
    baseline_edited = tmp_path / "baseline-edited.json"
    baseline_edited.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")

    second = tmp_path / "second.json"
    code = _run(sources, manifest, *_PERMISSIVE, "--json-out", str(second),
                "--baseline", str(baseline_edited))
    assert code == bench.EXIT_OK
    payload = json.loads(second.read_text("utf-8"))
    assert payload["regression"]["checked"] is True
    assert payload["regression"]["regressions"] == []


def test_cli_exits_1_on_regression_against_baseline(tmp_path: Path, capsys) -> None:
    sources, manifest = _fake_corpus(tmp_path)
    first = tmp_path / "first.json"
    assert _run(sources, manifest, *_PERMISSIVE, "--json-out", str(first)) == bench.EXIT_OK
    inflated = json.loads(first.read_text("utf-8"))
    current = float(inflated["headline"]["hybrid_recall@10"])
    # Эталон завышен так, что падение больше 5 % при любом текущем значении:
    # current < (3·current + 0.1)·0.95 выполнено всегда.
    inflated["headline"]["hybrid_recall@10"] = current * 3 + 0.1
    stale = tmp_path / "baseline-inflated.json"
    stale.write_text(json.dumps(inflated, ensure_ascii=False), encoding="utf-8")

    second = tmp_path / "second.json"
    code = _run(
        sources,
        manifest,
        *_PERMISSIVE,
        "--json-out",
        str(second),
        "--baseline",
        str(stale),
        "--max-regression",
        "0.05",
    )
    printed = capsys.readouterr().out
    payload = json.loads(second.read_text("utf-8"))
    assert code == bench.EXIT_NOT_ACCEPTED
    assert payload["regression"]["checked"] is True
    assert any("hybrid_recall@10" in item for item in payload["regression"]["regressions"])
    assert "РЕГРЕССИОННЫЙ КОНТРОЛЬ" in printed
    assert "приёмка НЕ подтверждена" in printed


def test_cli_does_not_record_baseline_of_failed_run(tmp_path: Path, capsys) -> None:
    """Проваленный прогон не становится эталоном: это зафиксировало бы деградацию."""
    sources, manifest = _fake_corpus(tmp_path)
    baseline = tmp_path / "baseline.json"
    code = _run(sources, manifest, "--min-recall-at-10", "1.01",
                "--write-baseline", str(baseline))
    assert code == bench.EXIT_NOT_ACCEPTED
    assert not baseline.exists()
    assert "Эталон НЕ записан" in capsys.readouterr().out


def test_cli_exits_2_when_baseline_is_unreadable(tmp_path: Path, capsys) -> None:
    sources, manifest = _fake_corpus(tmp_path)
    broken = tmp_path / "baseline.json"
    broken.write_text("{ не json", encoding="utf-8")
    out = tmp_path / "bench.json"
    code = _run(sources, manifest, *_PERMISSIVE, "--json-out", str(out),
                "--baseline", str(broken))
    assert code == bench.EXIT_NOT_MEASURED
    assert "РЕГРЕССИОННЫЙ КОНТРОЛЬ НЕ ВЫПОЛНЕН" in capsys.readouterr().out
    # Измерение retrieval состоялось — сырые метрики остаются в архиве.
    assert json.loads(out.read_text("utf-8"))["regression"]["checked"] is False


def test_compare_with_baseline_flags_only_real_drops() -> None:
    baseline: dict[str, Any] = {
        "headline": {
            "hybrid_recall@10": 1.0,
            "hybrid_precision@3": 0.0,
            "p95_seconds": 1.0,
        },
        "cases": [
            {"query": "прошёл и пропал", "passed": True},
            {"query": "прошёл и снова прошёл", "passed": True},
            {"query": "и раньше не проходил", "passed": False},
        ],
    }
    regressions = bench.compare_with_baseline(
        {
            "hybrid_recall@10": 0.90,  # −10 % — ниже порога 5 %
            "hybrid_precision@3": 0.5,  # рост с нуля — не сравнивается
            "p95_seconds": 1.04,  # +4 % — в допуске
        },
        baseline,
        bench.MAX_RELATIVE_REGRESSION,
        {
            "прошёл и пропал": False,
            "прошёл и снова прошёл": True,
            "и раньше не проходил": False,
        },
    )
    assert any("hybrid_recall@10" in item for item in regressions)
    assert not any("hybrid_precision@3" in item for item in regressions)
    assert not any("p95_seconds" in item for item in regressions)
    assert any("прошёл и пропал" in item for item in regressions)
    assert not any("прошёл и снова прошёл" in item for item in regressions)


def test_compare_with_baseline_flags_latency_growth() -> None:
    baseline: dict[str, Any] = {"headline": {"p95_seconds": 1.0}, "cases": []}
    regressions = bench.compare_with_baseline(
        {"p95_seconds": 1.2}, baseline, bench.MAX_RELATIVE_REGRESSION, {}
    )
    assert any("p95_seconds" in item for item in regressions)


def test_load_baseline_rejects_foreign_shape(tmp_path: Path) -> None:
    wrong_version = tmp_path / "b.json"
    wrong_version.write_text(json.dumps({"version": 99, "headline": {}}), encoding="utf-8")
    with pytest.raises(ValueError):
        bench.load_baseline(wrong_version)
    no_headline = tmp_path / "c.json"
    no_headline.write_text(json.dumps({"version": bench.PAYLOAD_VERSION}), encoding="utf-8")
    with pytest.raises(ValueError):
        bench.load_baseline(no_headline)
    missing = tmp_path / "нет.json"
    with pytest.raises(ValueError):
        bench.load_baseline(missing)


def test_cli_distinguishes_bad_flag_from_missing_corpus(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Неверно собранная команда и отсутствующий корпус читаются разными кодами.

    argparse при своей ошибке выходит с кодом 2, а 2 в этом CLI значит «измерение
    не выполнено». CI на опечатке в имени флага советовал проверять монтирование
    /data/sources, то есть уводил разбор не туда: диагноз «нет корпуса» дешевле
    настоящего отказа, и его никто не перепроверял.
    """
    assert bench.main(["--no-such-flag"]) == bench.EXIT_USAGE
    assert "usage:" in capsys.readouterr().err

    empty_corpus = tmp_path / "корпус"
    empty_corpus.mkdir()
    absent_manifest = tmp_path / "манифест-не-существует.json"
    assert (
        bench.main(
            ["--source-root", str(empty_corpus), "--manifest", str(absent_manifest)]
        )
        == bench.EXIT_NOT_MEASURED
    )


def test_cli_help_reports_all_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    """Справка не попадает в ветку ошибки разбора: SystemExit(0) остаётся успехом.

    Список кодов в описании должен совпадать с тем, что разбирает CI, иначе job
    будет трактовать код, которого человек из справки не знает.
    """
    assert bench.main(["--help"]) == bench.EXIT_OK
    printed = capsys.readouterr().out
    assert "3 — команда собрана неверно" in printed
    assert "2 — измерение не выполнено" in printed

