"""Измерения качества Научного Клубка: ответы, retrieval и сквозной конвейер.

Пороги измерений
----------------

«Стандарт качества 0.8+» не должен жить устным обещанием: ниже перечислен каждый
порог, константа, которая его задаёт, и то, кто его сегодня реально проверяет.

- ``PASS_TOLERANCE`` = 0.999 — нижняя граница ``citation_coverage`` и
  ``numeric_support`` в ``EvaluationMetrics.passed`` (``EvaluationHarness.evaluate``).
  Проверяется как флаг в ответе ``/api/v1/query`` и в списке ``/api/v1/evaluations``;
  в CI не гейтится. Граница прикладывается только к измеренной метрике: при нуле
  находок метрики нет, и ``passed`` становится ``False`` по названной причине, а не
  по «нарушению» границы пустого множества.
- ``CONTENT_MATCH_FLOOR`` = 0.999 и ``DIFFERENTIAL_RECALL_FLOOR`` = 0.6 — то же
  требование к ``citation_content_match`` (подтверждён ли тезис текстом цитаты) и к
  ``conflict_recall``/``gap_recall`` (названы ли ожиданием корпуса противоречия и
  пробелы). Обе включаются в композит и в ``passed`` только когда измерены: без
  корпуса дифференциаторные метрики не имеют значения и обязаны быть в ``skips``.
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
  Выборка — ``DEFAULT_PIPELINE_CASES`` = 10 gold-кейсов (раньше была отсечка 3):
  при ``n < MIN_SAMPLES_FOR_P95`` = 5 ``p95_latency_ms`` — не перцентиль, а
  наихудший наблюдаемый прогон, и это называется в ``notes`` вызывающего слоя.
- Полнота дифференциаторов (``conflict_recall``, ``gap_recall``):
  ``DIFFERENTIAL_RECALL_FLOOR`` = 0.6. Измеряется только по ожиданиям gold-кейса
  (``GoldCaseWithAnswer.expected_conflicts``/``expected_gaps``), а таких ожиданий
  сегодня ноль: корпус вне Git и пуст, поэтому метрика обязана быть в ``skips``
  с текстом «не измерено», а не в 0.0.
- CLI ``run_benchmark.py`` (пороги объявлены там же): ``recall@10 >= 0.90``,
  ``p95 <= 3 c``, гибрид лучше лексического базового по MRR и падение headline-метрик
  относительно эталона не больше ``MAX_RELATIVE_REGRESSION`` = 5 %. Гейтится
  автоматически двумя job'ами ``.gitlab-ci.yml``: ``benchmark:measure`` (прогон и
  пороги; запускается только на раннере с корпусом, и код 2 там валит пайплайн) и
  ``benchmark:baseline-gate`` (обязательная проверка того, что эталонный прогон
  вообще установлен). Пока эталона нет, второй job красный с текстом «ЭТАЛОН НЕ
  УСТАНОВЛЕН» — молча зелёным регрессионный контроль не притворяется.
- Точность ответа и судья в CLI не измеряются: ``run_benchmark.py`` — это прогон
  retrieval, а не качества генерации. Кейсов с подтверждённым ``expected_answer``
  сегодня ноль (корпус вне Git), поэтому метрика точности вакуумна и обязан
  отображаться как ``None``, а не как 0.0 или 1.0.

Композитная оценка больше не включает self-reported confidence модели:
«точная» уверенность — это утверждение самой модели, а не измерение поддержки
вывода доказательством. То же правило действует и для судьи:
``mean_finding_confidence`` не входит в ``overall`` ни в одном из режимов.

Метрики, которые нечего измерять, не имеют права получать долю за отсутствие
проверяемых случаев
--------------------------------------
Компонент композита существует только там, где есть что проверять. Пустой ответ
(``findings == []``) не обязан получать ``(0.0 + 1.0 + 1.0) / 3 = 0.667`` —
«все числовые тезисы подтверждены» при нуле тезисов и «доля неподтверждённых
равна нулю» при нуле выводов. Такие метрики помечаются как не измеренные
(``skips`` с названной причиной), ``overall`` не превышает правду (0.0), а
``passed`` обязан быть ``False``. То же правило распространяется на новые метрики:
``citation_content_match`` без находок, ``conflict_recall``/``gap_recall`` без
подтверждённых корпусом ожиданий, точность ответа без ``expected_answer_source`` и
судья без ключа — всё это «не измерено», а не 0.0 и не 1.0.

``citation_coverage`` считается по наличию цитаты, поэтому он не доказывает, что
тезис опирается именно на её текст: настоящую проверку содержимого делает
``citation_content_match`` (число тезиса и значимые слова обязаны присутствовать в
``EvidenceLocator.quote``, а локатор обязан иметь документ и адрес). Обе метрики
видны отдельно: расхождение между ними и есть «цитата есть, но она не про это».

Выборки
-------
``p95`` из трёх замеров — не перцентиль, а наихудшее наблюдение, и называть его p95
нельзя: ``MIN_SAMPLES_FOR_P95`` = 5 ниже этого порога метрика помечается как
недостаточная выборка, а числом становится наихудший наблюдаемый прогон
(консервативнее перцентиля, поэтому порог ``PIPELINE_P95_LATENCY_MS_FLOOR`` не
«случайно passes»). Отсечка по ``max_cases`` поднята с 3 до
``DEFAULT_PIPELINE_CASES`` = 10, и про неё сообщается явно.
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
from scientific_tangle.domain.models import EvidenceLocator
from scientific_tangle.evaluation.gold_cases import (
    GoldCaseReconciliation,
    reconcile_gold_cases,
)
from scientific_tangle.services.agent_metrics import AgentMetricsRegistry, agent_metrics
from scientific_tangle.services.knowledge import KnowledgeBase

PASS_TOLERANCE = 0.999
OVERALL_FLOOR = 0.8
ANSWER_CORRECTNESS_FLOOR = 0.8
JUDGE_QUALITY_FLOOR = 0.8
CONTENT_MATCH_FLOOR = 0.999
DIFFERENTIAL_RECALL_FLOOR = 0.6
RECALL_AT_3_FLOOR = 0.6
MRR_FLOOR = 0.5
PIPELINE_SOURCE_RECALL_FLOOR = 0.6
PIPELINE_P95_LATENCY_MS_FLOOR = 120_000
# Композит строится только по измеренным метрикам: отсутствие метрики означает
# «нечего проверять», а не «ноль» или «единица».
# Минимумы выборок: ниже них доля или перцентиль не считаются статистикой и
# обязаны называться явно, а не выдаваться за число, сравнимое с порогом.
MIN_STATISTICAL_SAMPLE = 5
MIN_SAMPLES_FOR_P95 = 5
DEFAULT_PIPELINE_CASES = 10
# Доля значимых слов тезиса, которую обязаны покрыть цитаты (E2), и такая же доля
# вместе с числами для формулировки противоречия или пробела (E3).
CONTENT_MATCH_TERM_FLOOR = 0.5
DIFFERENTIAL_TERM_FLOOR = 0.6
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
    """Gold-кейс с необязательным ожидаемым ответом и ожиданиями дифференциаторов.

    ``expected_answer`` заполняется ТОЛЬКО вместе с ``expected_answer_source`` —
    указанием файла корпуса и цитатой, откуда этот ответ взят. Без ссылки на
    источник метрика точности не измеряется: правдоподобный, но непроверенный
    ожидаемый ответ измерял бы не качество продукта, а фантазию автора кейса.

    ``expected_conflicts`` и ``expected_gaps`` — ожидания тех же двух
    дифференциаторов продукта (противоречия и пробелы) в той же форме: короткая
    сигнатура «числа + значимые слова», которую обязан назвать ответ. Пустой список
    означает «требование не подтверждено корпусом», то есть метрика не измерима, а
    не «противоречий в ответе быть не должно».
    """

    expected_answer: str | None = None
    expected_answer_source: str | None = None
    expected_conflicts: list[str] = Field(default_factory=list)
    expected_gaps: list[str] = Field(default_factory=list)


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


class ContentMatchVerdict(BaseModel):
    """Насколько тезисы подтверждены именно текстом цитаты, а не фактом её наличия.

    ``missing`` перечисляет id находок, чьё содержимое в цитатах не найдено: это
    главный diagnostic — «цитата приложена, но она не про этот тезис».
    """

    matched: int = Field(ge=0)
    total: int = Field(ge=0)
    ratio: float = Field(ge=0, le=1)
    missing: list[str] = Field(default_factory=list)


class DifferentialVerdict(BaseModel):
    """Полнота обнаружения противоречий и пробелов по ожиданиям gold-кейса.

    Сторона без подтверждённых корпусом ожиданий остаётся ``None``: пустой перечень
    ожиданий не означает «ни contradiction, ни gap искать не надо».
    """

    conflict_recall: float | None = Field(default=None, ge=0, le=1)
    gap_recall: float | None = Field(default=None, ge=0, le=1)
    expected_conflicts: int = Field(ge=0)
    expected_gaps: int = Field(ge=0)
    missed_conflicts: list[str] = Field(default_factory=list)
    missed_gaps: list[str] = Field(default_factory=list)


class LatencySampleReport(BaseModel):
    """Замер латентности вместе с размером выборки — без псевдо-per95 на трёх точках."""

    samples: int = Field(ge=0)
    average_ms: float = Field(ge=0)
    p95_ms: float = Field(ge=0)
    # False — когда p95 не является перцентилем: вместо него отдан наихудший
    # наблюдаемый прогон (консервативнее перцентиля, поэтому порогу он не «помогает»).
    is_percentile: bool
    note: str

    @property
    def sufficient(self) -> bool:
        return self.is_percentile


class AnswerAssessment(BaseModel):
    """Результат измерения ответа: базовые метрики плюс необязательные метрики.

    ``answer_correctness``, ``judge_quality``, ``citation_content_match``,
    ``conflict_recall`` и ``gap_recall`` равны ``None``, когда измерение невозможно
    (нет проверенного ожидаемого ответа / нет судьи / нет находок / ожидания не
    подтверждены корпусом), и это честно «не измеряли», а не ноль. ``skips``
    объясняет каждую пропущенную метрику, ``notes`` — всё, что снижает доверие к
    посчитанной цифре (малая выборка, отсечка по ``max_cases``).
    """

    run: EvaluationRun
    correctness: CorrectnessVerdict | None = None
    judge: JudgeVerdict | None = None
    judge_error: str | None = None
    content_match: ContentMatchVerdict | None = None
    differential: DifferentialVerdict | None = None
    skips: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # Размер выборки и число реально вошедших в композит компонент: «0.8+» без n
    # неотличимо от «0.8+» на одной находке.
    findings: int = Field(default=0, ge=0)
    components: int = Field(default=0, ge=0)

    @property
    def answer_correctness(self) -> float | None:
        return None if self.correctness is None else self.correctness.correctness

    @property
    def judge_quality(self) -> float | None:
        return None if self.judge is None else self.judge.quality

    @property
    def citation_content_match(self) -> float | None:
        return None if self.content_match is None else self.content_match.ratio

    @property
    def conflict_recall(self) -> float | None:
        return None if self.differential is None else self.differential.conflict_recall

    @property
    def gap_recall(self) -> float | None:
        return None if self.differential is None else self.differential.gap_recall


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
        # Полные вердикты (включая метрики, которым не нашлось места в контракте
        # EvaluationMetrics) держатся тем же ограниченным буфером: без них
        # `passed=False` осталось бы числом без объяснения.
        self._assessments: deque[AnswerAssessment] = deque(maxlen=max_runs)
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
        """Полный промер ответа: каждая метрика существует только если её было чем измерить.

        Композит собирается из измеренных компонент; при нуле находок измерять
        нечего, поэтому ``overall`` = 0.0 (не выше правды), ``passed`` = ``False``,
        а каждая базовая метрика попадает в ``skips`` с названной причиной — прежние
        ``(0.0 + 1.0 + 1.0) / 3 = 0.667`` давали пустому ответу долю за то, что
        проверять было нечего.
        """
        findings = answer.findings
        total = len(findings)
        skips: list[str] = []
        notes: list[str] = []

        citation_coverage: float | None = None
        numeric_support: float | None = None
        unsupported: float | None = None
        content_match: ContentMatchVerdict | None = None
        mean_confidence = sum(finding.confidence for finding in findings) / total if total else 0.0

        if total:
            cited = sum(bool(finding.evidence) for finding in findings)
            citation_coverage = cited / total
            numeric_findings = [
                finding for finding in findings if _NUMBER.search(finding.statement)
            ]
            # 1.0 при отсутствии числовых тезисов остаётся: метрика про «каждое
            # число подтверждено», а пустое множество чисел подтверждено целиком.
            # Настоящую проверку содержимого делает citation_content_match.
            numeric_support = (
                sum(bool(finding.evidence) for finding in numeric_findings) / len(numeric_findings)
                if numeric_findings
                else 1.0
            )
            unsupported = sum(1 for finding in findings if not _numbers_grounded(finding)) / total
            content_match = grade_citation_content_match(answer)
            if total < MIN_STATISTICAL_SAMPLE:
                notes.append(
                    f"доли посчитаны на n={total} < {MIN_STATISTICAL_SAMPLE}: это бинарные "
                    "отметки по каждой находке, а не статистика."
                )
        else:
            # Пустой ответ: ни одна доля не измерена. zero-значение в контракте —
            # это «не измерено» (причина названа ниже), а не «нарушение порога».
            skips.append(
                f"Метрики трассировки не измерены: в ответе {answer.query_id} ноль находок — "
                "цитировать и сверять содержимое нечего (не 1.0 за отсутствие проверок)."
            )

        correctness = grade_answer_correctness(answer, case)
        judge_verdict, judge_error = self._call_judge(answer, judge)
        differential = grade_differential_recall(answer, case)
        if correctness is None:
            skips.append(correctness_skip_reason(case))
        if judge_verdict is None:
            skips.append(
                f"LLM-судья не измерен: {judge_error}"
                if judge_error
                else "LLM-судья не внедрён — метрика судьи отсутствует (не 0.0)."
            )
        skips.extend(differential_skip_reasons(answer, case, differential))

        # Композит без судьи оставлен прежним — иначе «0.8+» тихо сменил бы смысл:
        # в него входят только измеренные метрики трассировки (доля цитат, числовая
        # поддержка, доля подтверждённых выводов, совпадение содержимого цитаты)
        # и, когда он измерен, судья. Точность ответа, как и раньше, в композит не
        # входит — она гейтит `passed` отдельным порогом. mean_finding_confidence
        # не входит ни в одном режиме.
        components: list[float] = []
        for candidate in (
            citation_coverage,
            numeric_support,
            None if unsupported is None else 1.0 - unsupported,
            None if content_match is None else content_match.ratio,
            None if judge_verdict is None else judge_verdict.quality,
            None if differential is None else differential.conflict_recall,
            None if differential is None else differential.gap_recall,
        ):
            if candidate is not None:
                components.append(candidate)
        overall = sum(components) / len(components) if components else 0.0

        metrics = EvaluationMetrics(
            citation_coverage=round(citation_coverage or 0.0, 3),
            numeric_support=round(numeric_support or 0.0, 3),
            unsupported_claim_ratio=round(unsupported or 0.0, 3),
            mean_finding_confidence=round(mean_confidence, 3),
            overall=round(overall, 3),
        )
        if total:
            passed = (
                metrics.citation_coverage >= PASS_TOLERANCE
                and metrics.numeric_support >= PASS_TOLERANCE
                and metrics.unsupported_claim_ratio <= 1 - PASS_TOLERANCE
                and metrics.overall >= OVERALL_FLOOR
            )
            # Дополнительные гейты включаются только когда метрика измерена:
            # непроверенный кейс или отсутствие судьи не должны ни спасать, ни
            # валить прогон молча.
            if content_match is not None:
                passed = passed and content_match.ratio >= CONTENT_MATCH_FLOOR
            if correctness is not None:
                passed = passed and correctness.correctness >= ANSWER_CORRECTNESS_FLOOR
            if judge_verdict is not None:
                passed = passed and judge_verdict.quality >= JUDGE_QUALITY_FLOOR
            if differential is not None:
                if differential.conflict_recall is not None:
                    passed = passed and differential.conflict_recall >= DIFFERENTIAL_RECALL_FLOOR
                if differential.gap_recall is not None:
                    passed = passed and differential.gap_recall >= DIFFERENTIAL_RECALL_FLOOR
        else:
            passed = False
            skips.append(
                "Прогон не принят: нечего принимать — ответ без находок не выполняет "
                "обещание трассировки тезиса к цитате."
            )

        run = EvaluationRun(query_id=answer.query_id, metrics=metrics, passed=passed)
        self._runs.append(run)
        assessment = AnswerAssessment(
            run=run,
            correctness=correctness,
            judge=judge_verdict,
            judge_error=judge_error,
            content_match=content_match,
            differential=differential,
            skips=skips,
            notes=notes,
            findings=total,
            components=len(components),
        )
        self._assessments.append(assessment)
        return assessment

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

    def list_assessments(self) -> list[AnswerAssessment]:
        """Полные вердикты последних прогонов: ``skips``, ``notes`` и новые метрики.

        ``EvaluationMetrics`` — контрактный ответ API, и новых полей в нём нет,
        поэтому ``citation_content_match``, ``conflict_recall`` и ``gap_recall``
        доступны отсюда; вызывающему слою остаётся показывать их рядом с ``passed``.
        """
        return list(reversed(self._assessments))

    @staticmethod
    @lru_cache(maxsize=1)
    def gold_cases() -> list[GoldCase]:
        """Gold-кейсы корпуса. Манифест читается один раз за процесс.

        ``expected_answer`` берётся из манифеста, только если там же лежит
        ``expected_answer_source`` (файл корпуса и цитата). Сегодня ни один кейс
        так не подтверждён: папка «Источники информации/» вне Git и в этом
        checkout пуста, поэтому метрика точности для gold-кейсов не измеряется.
        Ожидания противоречий и пробелов манифест выразить не может — поля такие
        есть только у ``GoldCaseWithAnswer`` и ``RealGoldCase``, и их заполнение
        требует корпуса.
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
                    expected_conflicts=list(item.get("expected_conflicts") or []),
                    expected_gaps=list(item.get("expected_gaps") or []),
                )
            )
        return cases

    @classmethod
    def reconcile_gold_cases(cls) -> GoldCaseReconciliation:
        """Сверка двух носителей gold-кейсов: манифеста корпуса и бенчмарка.

        Расхождение здесь — не косметика: recall по 10 кейсам манифеста и по 12
        кейсам ``REAL_GOLD_CASES`` — разные измерения, а сравнение с эталоном CI
        делает вид, что это одно и то же.
        """
        path = Path(__file__).parents[1] / "preload_manifest.json"
        items: list[dict[str, object]] = json.loads(path.read_text("utf-8"))
        return reconcile_gold_cases(items)

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
        max_cases: int = DEFAULT_PIPELINE_CASES,
        notes: list[str] | None = None,
    ) -> PipelineBenchmark:
        """Сквозной прогон gold-кейсов; ``notes`` — необязательный сборник оговорок.

        Потолок выборок поднят с 3 до ``DEFAULT_PIPELINE_CASES``: на трёх кейсах
        и ``p95``, и ``pass_rate`` остаются бинарными отметками, а не метриками.
        Список ``notes`` вызывающий слой может показать как деградацию замера —
        молча уменьшать выборку нельзя.
        """
        all_cases = self.gold_cases()
        cases = all_cases[:max_cases]
        if notes is not None and len(all_cases) > len(cases):
            notes.append(
                f"выборка ограничена max_cases={max_cases} из {len(all_cases)} gold-кейсов: "
                "метрики посчитаны по первому срезу, а не по всему набору."
            )
        metrics, results = await self.evaluate_workflow(workflow, cases, notes=notes)
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
        *,
        notes: list[str] | None = None,
    ) -> tuple[PipelineVariantMetrics, list[PipelineCaseResult]]:
        """Кейсы выполняются последовательно, каждый со своим окном метрик.

        Прежний замер брал снимок глобального реестра до и после параллельного
        gather: при сравнении вариантов A/B в одном процессе (или при любом другом
        трафике) токены и repair-повторы чужих запросов попадали в метрики
        варианта. Последовательное окно на кейс атрибутирует каждый счётчик
        своему кейсу, поэтому сравнение вариантов остаётся честным.

        ``p95_latency_ms`` при недостаточной выборке (< ``MIN_SAMPLES_FOR_P95``
        кейсов) — не перцентиль, а наихудший наблюдаемый прогон, и причина этого
        кладётся в ``notes``, если вызывающий слой их собирает.
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
        latency = latency_sample_report(latencies)
        if notes is not None and not latency.is_percentile:
            notes.append(latency.note)
        return (
            PipelineVariantMetrics(
                source_recall=round(sum(recalls) / count, 3),
                citation_coverage=round(sum(coverages) / count, 3),
                pass_rate=round(passed_count / count, 3),
                average_latency_ms=round(sum(latencies) / count, 1),
                p95_latency_ms=latency.p95_ms,
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


def _locator_addressed(evidence: EvidenceLocator) -> bool:
    """Локатор обязан называть документ и адрес, а не только нести текст цитаты.

    Продукт обещает трассировку «документ → фрагмент (страница, лист, диапазон
    ячеек)»: цитата без источника и без адреса проверяема читателем не лучше, чем
    её отсутствие.
    """
    if not evidence.source_title.strip():
        return False
    return (
        evidence.page is not None
        or bool(evidence.sheet)
        or bool(evidence.cell_range)
        or evidence.char_start is not None
    )


def _content_matches_quotes(statement: str, quotes: list[str]) -> bool:
    """Числа и значимые слова тезиса обязаны присутствовать в тексте цитат.

    Детерминированно и на том, что уже есть в контуре: числа сравниваются по
    значению (``_canonical_numbers`` — «70» подтверждается записью «70,0»,
    «10 000» равна «10000»), слова — по пятибуквенным префиксам (``_term_prefixes``),
    чтобы формы одного слова («шлак», «шлаками») сошлись без словаря словоформ и
    без новых зависимостей.
    """
    if not quotes:
        return False
    quote_text = " ".join(quotes)
    statement_numbers = _canonical_numbers(statement)
    if not statement_numbers <= _canonical_numbers(quote_text):
        return False
    needed_terms = _term_prefixes(statement)
    if not needed_terms:
        # Тезис без значимых слов обязан быть числовым, иначе проверять содержимое
        # нечем: пустое утверждение цитатой не подтверждается.
        return bool(statement_numbers)
    return (
        len(needed_terms & _term_prefixes(quote_text)) / len(needed_terms)
        >= CONTENT_MATCH_TERM_FLOOR
    )


def grade_citation_content_match(answer: AnswerPayload) -> ContentMatchVerdict | None:
    """Настоящая метрика трассировки: тезис сверен с текстом его цитаты.

    ``citation_coverage`` и ``numeric_support`` считают лишь *наличие* доказательств
    (``bool(finding.evidence)``), поэтому ответ с приложенной не к месту цитатой
    получает 1.0. Здесь же проверяется содержимое: число и значимые слова тезиса
    обязаны быть в ``EvidenceLocator.quote``, а локатор — называть документ и адрес.
    Метрика измерима только при ненулевом числе находок.
    """
    findings = answer.findings
    total = len(findings)
    if not total:
        return None
    missing = [
        finding.id
        for finding in findings
        if not _content_matches_quotes(
            finding.statement,
            [
                evidence.quote
                for evidence in finding.evidence
                if _locator_addressed(evidence) and evidence.quote.strip()
            ],
        )
    ]
    matched = total - len(missing)
    return ContentMatchVerdict(
        matched=matched,
        total=total,
        ratio=round(matched / total, 3),
        missing=missing,
    )


def differential_expectations(case: GoldCase | None) -> tuple[list[str], list[str]]:
    """(ожидаемые противоречия, ожидаемые пробелы) кейса; пустое = «не подтверждено»."""
    if not isinstance(case, GoldCaseWithAnswer):
        return [], []
    return list(case.expected_conflicts), list(case.expected_gaps)


def _signature_recalled(signature: str, lines: list[str]) -> bool:
    """Сигнатура ожидания найдена в перечисленном ответом противоречии/пробеле.

    Числа обязаны сойтись все (противоречие «70 против 95» не засчитывается по
    одному числу), значимые слова — не ниже ``DIFFERENTIAL_TERM_FLOOR``.
    """
    needed_numbers = _canonical_numbers(signature)
    needed_terms = _term_prefixes(signature)
    if not needed_numbers and not needed_terms:
        return False
    for line in lines:
        if needed_numbers and not needed_numbers <= _canonical_numbers(line):
            continue
        if needed_terms:
            found = _term_prefixes(line)
            if len(needed_terms & found) / len(needed_terms) < DIFFERENTIAL_TERM_FLOOR:
                continue
        return True
    return False


def grade_differential_recall(
    answer: AnswerPayload,
    case: GoldCase | None,
) -> DifferentialVerdict | None:
    """``conflict_recall`` и ``gap_recall`` — полнота продуктовых дифференциаторов.

    Ответ обязан не только трассировать тезисы: обнаружение противоречий и пробелов
    — то, чем продукт отличается от «чата с документами». Измеряется только по
    ожиданиям gold-кейса, подтверждённым корпусом; без них обе метрики остаются
    ``None`` (см. ``differential_skip_reasons``), а не 0.0 — иначе прогон без корпуса
    выглядел бы провалом детекции.
    """
    expected_conflicts, expected_gaps = differential_expectations(case)
    if not expected_conflicts and not expected_gaps:
        return None
    missed_conflicts = [
        signature
        for signature in expected_conflicts
        if not _signature_recalled(signature, list(answer.conflicts))
    ]
    missed_gaps = [
        signature
        for signature in expected_gaps
        if not _signature_recalled(signature, list(answer.knowledge_gaps))
    ]
    return DifferentialVerdict(
        conflict_recall=(
            round((len(expected_conflicts) - len(missed_conflicts)) / len(expected_conflicts), 3)
            if expected_conflicts
            else None
        ),
        gap_recall=(
            round((len(expected_gaps) - len(missed_gaps)) / len(expected_gaps), 3)
            if expected_gaps
            else None
        ),
        expected_conflicts=len(expected_conflicts),
        expected_gaps=len(expected_gaps),
        missed_conflicts=missed_conflicts,
        missed_gaps=missed_gaps,
    )


def differential_skip_reasons(
    answer: AnswerPayload,
    case: GoldCase | None,
    verdict: DifferentialVerdict | None,
) -> list[str]:
    """Почему противоречия и пробелы не измерены — пропуск обязан называть причину."""
    if verdict is not None:
        return []
    expected_conflicts, expected_gaps = differential_expectations(case)
    if case is None:
        return [
            "conflict_recall и gap_recall не измерены: gold-кейс не передан — "
            "ожиданий противоречий и пробелов нет."
        ]
    if not isinstance(case, GoldCaseWithAnswer):
        return [
            f"conflict_recall и gap_recall не измерены: кейс {case.id} — GoldCase без полей "
            "ожиданий; контракт GoldCase противоречий и пробелов не выражает."
        ]
    return [
        f"conflict_recall и gap_recall не измерены: у кейса {case.id} нет подтверждённых "
        f"корпусом ожиданий (противоречий — {len(expected_conflicts)}, пробелов — "
        f"{len(expected_gaps)}): корпус «Источники информации/» пуст, заполнять "
        "expect-* поля нечем, а без ожидания мерить нечего."
    ]


def latency_sample_report(latencies: list[float]) -> LatencySampleReport:
    """Перцентиль только когда выборки хватает; иначе — наихудшее наблюдение с оговоркой.

    ``p95`` из трёх замеров — это максимум под чужим именем: он и занижает хвост,
    и создаёт видимость измерения. Ниже ``MIN_SAMPLES_FOR_P95`` отдаём наихудший
    наблюдаемый прогон (он консервативнее любого перцентиля, поэтому порог
    латентности он не «проходит случайно») и явную отметку о выборке.
    """
    samples = len(latencies)
    average = sum(latencies) / samples if samples else 0.0
    if not samples:
        return LatencySampleReport(
            samples=0,
            average_ms=0.0,
            p95_ms=0.0,
            is_percentile=False,
            note="Латентность не измерена: ни одного завершённого кейса (недостаточная выборка).",
        )
    ordered = sorted(latencies)
    if samples < MIN_SAMPLES_FOR_P95:
        return LatencySampleReport(
            samples=samples,
            average_ms=round(average, 1),
            p95_ms=round(ordered[-1], 1),
            is_percentile=False,
            note=(
                f"p95 не измерен: недостаточная выборка n={samples} < {MIN_SAMPLES_FOR_P95} — "
                "в p95_latency_ms записан наихудший наблюдаемый прогон."
            ),
        )
    index = min(round((samples - 1) * 0.95), samples - 1)
    return LatencySampleReport(
        samples=samples,
        average_ms=round(average, 1),
        p95_ms=round(ordered[index], 1),
        is_percentile=True,
        note=f"p95 по n={samples} замеров.",
    )


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

