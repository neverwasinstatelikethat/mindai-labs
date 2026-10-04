"""Реальные gold-кейсы для benchmark оценки retrieval на документах корпуса.

Каждый кейс основан на фактическом содержании документа из папки
«Источники информации/Обзоры». Запросы сформулированы на русском языке
и соответствуют вопросам, которые задаёт исследователь-металлург.

Два носителя одного набора gold-кейсов и почему они сверяются явно
---------------------------------------------------------------
Оценка читает кейсы из двух мест: ``EvaluationHarness.gold_cases()`` берёт
``preload_manifest.json`` (путь файла корпуса + эталонный вопрос), а retrieval-бенчмарк
``run_benchmark.py`` — ``REAL_GOLD_CASES`` ниже (заголовки источников и категория).
Наборы уже разошлись: в манифесте 10 кейсов, здесь 12, а формулировка вопроса про
хлорное выщелачивание в них разная. Молча жить с двумя эталонами нельзя: метрика
recall зависит от того, какой из них попал в прогон. Пока ``preload_manifest.json``
не вынесен в единый источник (файл лежит вне этой зоны), расхождение не устраняется,
а называется: ``reconcile_gold_cases`` сравнивает наборы и возвращает конкретные
строки-диагностики, печатает их и бенчмарк, и тест
``tests/test_benchmark_metrics.py``.

Поле ``expected_answer`` — необязательный ожидаемый ответ для метрики точности
(``scientific_tangle.evaluation.harness.grade_answer_correctness``). Заполнять его
разрешено только вместе с ``expected_answer_source``: имя файла корпуса и цитата,
откуда этот ответ взят. Без ссылки на первоисточник метрика точности не
измеряется — правдоподобный, но непроверенный ожидаемый ответ измерял бы не
качество продукта, а фантазию автора кейса.

``expected_conflicts`` и ``expected_gaps`` — ожидания продуктовых дифференциаторов
(обнаружение противоречий и пробелов). Заполняются тем же правилом: только то, что
прочитано в документе корпуса. Ни одно ожидание сегодня не заполнено — папка
«Источники информации/» вне Git и пуста в этом checkout, поэтому метрики
``conflict_recall`` и ``gap_recall`` обязаны попадать в ``skips`` как «не измерено»,
а не в 0.0.

Ни один кейс сегодня не заполнен: папка «Источники информации/» вне Git и пуста
в этом checkout, сопоставить формулировку с текстом документа нечем, поэтому
метрика точности для этих кейсов вакуумна (0 из 12).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class RealGoldCase:
    """Один gold-кейс для retrieval-бенчмарка."""

    query: str
    source_documents: list[str]
    category: str
    # Ожидаемый ответ и источник, которым он подтверждён, — оба None, пока кейс
    # не сверен с текстом документа: пустая пара означает «не измеряли».
    expected_answer: str | None = None
    expected_answer_source: str | None = None
    # Ожидания дифференциаторов: противоречие или пробел, которые обязан назвать
    # ответ. Пустые списки = «требование не подтверждено корпусом» = метрика
    # не измерима, а не «противоречий быть не должно».
    expected_conflicts: tuple[str, ...] = ()
    expected_gaps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GoldCaseReconciliation:
    """Результат сверки манифеста корпуса и ``REAL_GOLD_CASES``.

    Пустой отчёт означает «наборы совпадают»; любой непустой — конкретные
    формулировки расхождения, чтобы никто не читал разный recall двух эталонов
    как один и тот же замер.
    """

    manifest_total: int
    benchmark_total: int
    matched: int
    matched_sources: tuple[str, ...] = ()
    manifest_only: tuple[str, ...] = ()
    benchmark_only: tuple[str, ...] = ()
    question_mismatches: tuple[str, ...] = ()

    @property
    def balanced(self) -> bool:
        """Наборы совпадают по составу источников и по формулировкам вопросов."""
        return not (self.manifest_only or self.benchmark_only or self.question_mismatches)

    def describe(self) -> list[str]:
        """Диагностика расхождений словами — она и есть цель сверки."""
        lines: list[str] = []
        for source in self.manifest_only:
            lines.append(
                f"источник есть в манифесте корпуса, но не покрыт кейсом бенчмарка: {source}"
            )
        for query in self.benchmark_only:
            lines.append(f"кейс бенчмарка без пары в манифесте корпуса: {query}")
        for mismatch in self.question_mismatches:
            lines.append(f"разная формулировка эталонного вопроса: {mismatch}")
        if not lines:
            lines.append(
                f"наборы gold-кейсов совпадают: {self.matched} источник(ов), "
                f"манифест {self.manifest_total}, бенчмарк {self.benchmark_total}"
            )
        return lines


def _normalize_question(text: str) -> str:
    """Вопрос для сверки: регистр и лишние пробелы не считаются расхождением."""
    return " ".join(text.strip().lower().split())


def reconcile_gold_cases(
    manifest_items: Iterable[Mapping[str, Any]],
    real: Sequence[RealGoldCase] | None = None,
) -> GoldCaseReconciliation:
    """Сверяет записи манифеста прелоада с ``REAL_GOLD_CASES``.

    Источник считается покрытым, если хотя бы один кейс бенчмарка ссылается на
    его заголовок файла (``Path(path).stem`` == ``source_documents[0]``): второй
    вопрос к тому же документу — это не сирота. Отдельно называются три разных
    дефекта: источник манифеста без единого кейса, кейс об источнике, которого в
    манифесте нет, и «источник покрыт, но вопрос манифеста не задаёт никто» —
    потому что recall по разным вопросам это уже разные измерения, а сравнение с
    одним эталоном CI этого не различает.
    """
    cases = list(real if real is not None else REAL_GOLD_CASES)
    manifest_by_source: dict[str, str] = {}
    for item in manifest_items:
        manifest_by_source[Path(str(item["path"])).stem] = str(item["gold_question"])

    # Источник вправе нести несколько gold-вопросов: манифест хранит по одному
    # вопросу на файл корпуса, бенчмарк добавляет к тому же документу вторую
    # формулировку. Парит 1:1 по точной формулировке такой кейс становился
    # «сиротой без пары в манифесте», а тот же файл — «источником, не покрытым
    # кейсом»: две противоположные диагностики об одном и том же документе.
    queries_by_source: dict[str, list[str]] = {}
    for case in cases:
        source = case.source_documents[0] if case.source_documents else ""
        queries_by_source.setdefault(source, []).append(case.query)

    matched: list[str] = [source for source in manifest_by_source if source in queries_by_source]
    manifest_only: list[str] = [
        source for source in manifest_by_source if source not in queries_by_source
    ]
    # Сиротой считается кейс, источника которого в манифесте нет вовсе: такой
    # замер невыполним на этом корпусе, и это надо видеть.
    benchmark_only: list[str] = [
        query
        for source, queries in queries_by_source.items()
        if source not in manifest_by_source
        for query in queries
    ]
    # Разная формулировка — расхождение только тогда, когда ни один вопрос
    # бенчмарка не совпадает с вопросом манифеста: иначе оба набора измеряют
    # один и тот же ответ, просто у бенчмарка есть дополнительные вопросы.
    question_mismatches: list[str] = [
        f"{source}: манифест — «{manifest_by_source[source]}», "
        f"бенчмарк — «{queries_by_source[source][0]}»"
        for source in matched
        if not any(
            _normalize_question(query) == _normalize_question(manifest_by_source[source])
            for query in queries_by_source[source]
        )
    ]

    return GoldCaseReconciliation(
        manifest_total=len(manifest_by_source),
        benchmark_total=len(cases),
        matched=len(matched),
        matched_sources=tuple(matched),
        manifest_only=tuple(manifest_only),
        benchmark_only=tuple(benchmark_only),
        question_mismatches=tuple(question_mismatches),
    )


# ── Заголовки документов (совпадают с Path(filename).stem из парсера) ──

SLAG = "Обеднение_шлаков"
CU_EW = "ОИП-05-2019 Параметры Cu EW"
HEAP_LEACH = "ТИ-5-2017. Кучное выщелачивание в условиях холодного климата"
CHLOR_LEACH = "Хлорное выщелачивание ОИП 02-2024"
FE_REMOVAL = "Очистка от Fe 2020"
NI_CO_SULFATE = "ОИП-01-2022 Обзор существующих технологий получения сульфатов никеля и кобальта"
CU_NI_MATTE = "Обзор пеработка медно-никелевых штейнов (обжиг-выщелачивание) фул"
LI_PROD = "ОИП-06-2022 Технологии производства лития из рудного сырья"
PB_REMOVAL = "ОИП-04-2022 Удаление свинца"
CYAN_PGM = "Цианидное выщелачивание МПГ"


REAL_GOLD_CASES: list[RealGoldCase] = [
    # 1. Обеднение шлаков
    RealGoldCase(
        query="Какие параметры влияют на обеднение металлургических шлаков?",
        source_documents=[SLAG],
        category="slag_cleaning",
    ),
    # 2. Электроэкстракция меди
    RealGoldCase(
        query="Какие параметры определяют электроэкстракцию меди?",
        source_documents=[CU_EW],
        category="electrowinning",
    ),
    # 3. Кучное выщелачивание в холодном климате
    RealGoldCase(
        query="Как холодный климат влияет на кучное выщелачивание?",
        source_documents=[HEAP_LEACH],
        category="heap_leaching",
    ),
    # 4. Хлорное выщелачивание никеля
    RealGoldCase(
        query="Какие технологические схемы применяют для хлорного выщелачивания никеля?",
        source_documents=[CHLOR_LEACH],
        category="chloride_leaching",
    ),
    # 5. Удаление железа из растворов
    RealGoldCase(
        query="Какими способами удаляют железо из технологических растворов?",
        source_documents=[FE_REMOVAL],
        category="iron_removal",
    ),
    # 6. Сульфаты никеля и кобальта
    RealGoldCase(
        query="Какие технологии используют для получения сульфатов никеля и кобальта?",
        source_documents=[NI_CO_SULFATE],
        category="sulfate_production",
    ),
    # 7. Переработка медно-никелевых штейнов
    RealGoldCase(
        query="Как перерабатывают медно-никелевые штейны обжигом и выщелачиванием?",
        source_documents=[CU_NI_MATTE],
        category="matte_processing",
    ),
    # 8. Производство лития из рудного сырья
    RealGoldCase(
        query="Какие технологии применяют для производства лития из рудного сырья?",
        source_documents=[LI_PROD],
        category="lithium_production",
    ),
    # 9. Удаление свинца из растворов
    RealGoldCase(
        query="Какими методами удаляют свинец из металлургических потоков?",
        source_documents=[PB_REMOVAL],
        category="lead_removal",
    ),
    # 10. Цианидное выщелачивание МПГ
    RealGoldCase(
        query="Как цианидное выщелачивание применяют для металлов платиновой группы?",
        source_documents=[CYAN_PGM],
        category="pgm_leaching",
    ),
    # 11. Дополнительный: соединения железа при осаждении
    RealGoldCase(
        query="В виде каких соединений осаждают железо из технологических растворов?",
        source_documents=[FE_REMOVAL],
        category="iron_removal",
    ),
    # 12. Дополнительный: переработка сподумена
    RealGoldCase(
        query="Какие способы переработки сподумена существуют?",
        source_documents=[LI_PROD],
        category="lithium_production",
    ),
]

__all__ = [
    "GoldCaseReconciliation",
    "REAL_GOLD_CASES",
    "RealGoldCase",
    "reconcile_gold_cases",
]
