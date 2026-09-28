"""Метрики retrieval-бенчмарка: recall, hit и NDCG считаются по определениям.

Тесты нужны потому, что корпус (`Источники информации/`) на этой машине отсутствует,
и прогон `run_benchmark.py` даёт лишь «нет findings». Ошибку в формуле это не
поймало бы, а здесь она видна напрямую.

Вторая часть файла гейтит сам CLI (`main` возвращает код, а не печатает отчёт),
регрессионный контроль с эталоном и две необязательные метрики качества ответа —
точность по проверенному ожидаемому ответу и LLM-судью. Корпуса в CI нет, поэтому
CLI проверяется на двойнике корпуса из `.txt`-двойников: имена файлов дают те же
заголовки, что и `REAL_GOLD_CASES`.
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
from scientific_tangle.evaluation.gold_cases import REAL_GOLD_CASES
from scientific_tangle.evaluation.harness import (
    OVERALL_FLOOR,
    EvaluationHarness,
    GoldCaseWithAnswer,
    JudgeVerdict,
    grade_answer_correctness,
)
from scientific_tangle.evaluation.run_benchmark import TOP_K_VALUES, compute_case_metrics

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


def _answer(statements: list[str], *, confidence: float = 0.4) -> AnswerPayload:
    """Ответ, в котором каждое число тезиса подтверждён своей цитатой.

    Цитата равна тезису, поэтому базовая тройка метрик (полнота цитирования,
    числовая поддержка, доля неподтверждённых выводов) даёт ровно 1.0 — и любое
    отклонение `overall` объясняется только добавленной метрикой.
    """
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
                    quote=statement,
                )
            ],
        )
        for index, statement in enumerate(statements)
    ]
    return AnswerPayload.model_construct(
        query_id=uuid4(),
        question="Какие параметры влияют на обеднение шлаков?",
        summary=" ".join(statements),
        findings=findings,
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


def test_overall_formula_stays_legacy_when_no_judge() -> None:
    """Без судьи `overall` = среднее прежних трёх компонент: смысл «0.8+» не менялся."""
    answer = _answer(["Обеднение шлака даёт 70 % извлечения меди"])
    run = EvaluationHarness().evaluate(answer)
    assert run.metrics.citation_coverage == 1.0
    assert run.metrics.numeric_support == 1.0
    assert run.metrics.unsupported_claim_ratio == 0.0
    assert run.metrics.overall == round((1.0 + 1.0 + (1.0 - 0.0)) / 3, 3) == 1.0
    assert run.passed is True


def test_judge_becomes_fourth_component_and_gates_passed() -> None:
    """Судья добавляется в композит явно и гейтит `passed`, только когда измерен."""
    answer = _answer(["Обеднение шлака даёт 70 % извлечения меди"])

    strict = EvaluationHarness().assess(answer, judge=_StubJudge(quality=0.5))
    assert strict.judge is not None
    assert strict.judge_quality == 0.5
    assert strict.run.metrics.overall == round((1.0 + 1.0 + 1.0 + 0.5) / 4, 3)
    assert strict.run.passed is False

    ok = EvaluationHarness().assess(answer, judge=_StubJudge(quality=0.9))
    assert ok.run.metrics.overall == round((1.0 + 1.0 + 1.0 + 0.9) / 4, 3)
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
) -> GoldCaseWithAnswer:
    return GoldCaseWithAnswer(
        id="real-corpus-01",
        language="ru",
        question="Какие параметры влияют на обеднение шлаков?",
        source_path="Обзоры/Обеднение_шлаков.docx",
        expected_source_titles=["Обеднение_шлаков"],
        expected_answer=expected_answer,
        expected_answer_source=expected_answer_source,
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

