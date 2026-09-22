"""Регрессии на пункты, которые закрывал оркестратор, а не выделенные зоны:
матрица сравнения, отчёт компиляции корпуса, выборка прелоада, хранилище эволюции."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from scientific_tangle.domain.contracts import (
    ComparisonRequest,
    DocumentFragment,
    DocumentRequest,
    EvolutionExperiment,
    EvolutionProposal,
    Finding,
    PipelineVariantMetrics,
    StructuralDocumentReceipt,
)
from scientific_tangle.domain.models import EvidenceLocator, NumericObservation
from scientific_tangle.services.agent_metrics import AgentMetricsRegistry
from scientific_tangle.services.comparison import ComparisonService
from scientific_tangle.services.corpus import CorpusCompiler
from scientific_tangle.services.document_parser import UnsupportedDocumentError
from scientific_tangle.services.evolution import EvolutionService
from scientific_tangle.services.preload import PreloadService

DOC_ID = UUID(int=7)


def _obs(value: float, operator: str, unit: str = "%") -> NumericObservation:
    return NumericObservation(
        property_name="recovery",
        operator=operator,  # type: ignore[arg-type]
        value=value if operator != "between" else None,
        min_value=80.0 if operator == "between" else None,
        max_value=90.0 if operator == "between" else None,
        unit=unit,
        normalized_value=value if operator != "between" else None,
        normalized_min=80.0 if operator == "between" else None,
        normalized_max=90.0 if operator == "between" else None,
        normalized_unit=unit,
        raw_text=f"{value} {unit}",
    )


def _finding(subject: str, value: float, operator: str, statement: str = "Показатель") -> Finding:
    return Finding(
        id=f"finding-{subject}-{value}-{operator}",
        statement=statement,
        confidence=0.8,
        subject=subject,
        predicate="HAS_RECOVERY",
        observations=[_obs(value, operator)],
        evidence=[EvidenceLocator(document_id=DOC_ID, source_title="ОИП", page=1, quote="95 %")],
    )


def test_operator_survives_into_comparison_cell() -> None:
    """«≥95» и «95» — разные утверждения; оператор теряться в таблице не вправе."""
    table = ComparisonService().compare(
        [_finding("ro", 95.0, "gte")],
        ComparisonRequest(question="сравнить", entities=["ro"], dimensions=["recovery"]),
    )
    assert table.rows[0].cells["recovery"].value == "≥95.0"


def test_disagreement_between_sources_is_visible_in_cell() -> None:
    findings = [_finding("ro", 95.0, "gte"), _finding("ro", 82.0, "gte")]
    table = ComparisonService().compare(
        findings,
        ComparisonRequest(question="сравнить", entities=["ro"], dimensions=["recovery"]),
    )
    cell = table.rows[0].cells["recovery"]
    assert cell.value is not None
    assert "≥95.0" in cell.value and "≥82.0" in cell.value


def test_requested_dimension_stays_a_column_without_data() -> None:
    table = ComparisonService().compare(
        [_finding("ro", 95.0, "gte")],
        ComparisonRequest(question="сравнить", entities=["ro"], dimensions=["energy_ratio"]),
    )
    assert "energy_ratio" in table.headers
    assert table.rows[0].cells["energy_ratio"].value is None


def test_entity_filter_matches_whole_tokens_only() -> None:
    """Подстрока тянула Fe2O3 в столбец Fe — сравнение разных веществ."""
    findings = [_finding("Fe2O3", 95.0, "gte"), _finding("Fe", 90.0, "gte")]
    table = ComparisonService().compare(
        findings,
        ComparisonRequest(question="сравнить", entities=["Fe"], dimensions=["recovery"]),
    )
    assert [row.item for row in table.rows] == ["Fe"]


class _BrokenKnowledge:
    """index_document падает по-infraструктуре: это отказ пайплайна, а не OCR-бэклог."""

    def index_document(self, document: Any, source_path: str) -> Any:  # pragma: no cover
        raise RuntimeError("neo4j unavailable")


def _compile(tmp_path: Path, knowledge: Any, limit: int = 10) -> Any:
    return CorpusCompiler(knowledge, tmp_path, max_file_bytes=10_000).compile(limit)


def test_infrastructure_failure_is_not_counted_as_ocr_backlog(tmp_path: Path) -> None:
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4\n")
    report = _compile(tmp_path, _BrokenKnowledge())
    assert report.ocr_required == 0
    assert report.failed == 1


def _blank_pdf() -> bytes:
    """Настоящий PDF без текстового слоя — единственный честный сигнал «нужен OCR»."""
    import fitz

    document = fitz.open()
    document.new_page()
    payload = document.tobytes()
    document.close()
    return payload


def test_ocr_backlog_counts_only_undersized_text_layer(tmp_path: Path) -> None:
    class _TextlessKnowledge:
        def index_document(self, document: Any, source_path: str) -> Any:  # noqa: ARG002
            raise UnsupportedDocumentError("Не удалось извлечь достаточно текста")

    (tmp_path / "scan.pdf").write_bytes(_blank_pdf())
    (tmp_path / "note.txt").write_text("текст документа " * 5, encoding="utf-8")
    report = _compile(tmp_path, _TextlessKnowledge())
    assert report.ocr_required == 1
    # Тот же текст ошибки на не-pdf — отказ пайплайна, в OCR-бэклог он не идёт.
    assert report.failed == 1


def test_coverage_denominator_excludes_uningestable_files(tmp_path: Path) -> None:
    class _OkKnowledge:
        def index_document(self, document: Any, source_path: str) -> Any:
            return StructuralDocumentReceipt(
                document_id=uuid4(), checksum="c", status="created", chunks=1
            )

    (tmp_path / "ok.txt").write_text("текст документа " * 5, encoding="utf-8")
    (tmp_path / "raw.rgb").write_text("не поддерживается", encoding="utf-8")
    report = _compile(tmp_path, _OkKnowledge())
    assert report.skipped_unsupported == 1
    assert report.coverage == 1.0


def test_sampling_survives_single_fragment_limit() -> None:
    document = DocumentRequest(
        title="Отчёт",
        text="исходный текст документа достаточно длинный",
        fragments=[
            DocumentFragment(text=f"фрагмент {index}", page=index + 1) for index in range(5)
        ],
    )
    assert len(PreloadService._sample_document(document, 1).fragments) == 1
    assert len(PreloadService._sample_document(document, 3).fragments) == 3
    assert len(PreloadService._sample_document(document, 99).fragments) == 5


def _proposal(created_at: datetime, status: str = "proposed") -> EvolutionProposal:
    return EvolutionProposal(
        id=uuid4(),
        source_query_id=uuid4(),
        kind="prompt",
        title="t",
        change=f"c-{created_at.isoformat()}",
        status=status,  # type: ignore[arg-type]
        impact=[],
        created_at=created_at,
    )


def _experiment(created_at: datetime) -> EvolutionExperiment:
    metrics = PipelineVariantMetrics(
        source_recall=1.0, citation_coverage=1.0, pass_rate=1.0, average_latency_ms=1.0
    )
    return EvolutionExperiment(
        id=uuid4(),
        proposal_id=uuid4(),
        cases=1,
        baseline=metrics,
        candidate=metrics,
        delta_pass_rate=0.0,
        decision="reject",
        created_at=created_at,
    )


def test_active_policy_is_insertion_order_independent() -> None:
    """Две идентичные очереди принятых правок не должны давать разные промпты."""
    service = EvolutionService(provider=None)  # type: ignore[arg-type]
    now = datetime.now(UTC)
    older = _proposal(now - timedelta(hours=1), status="accepted")
    newer = _proposal(now, status="accepted")
    for items in ([older, newer], [newer, older]):
        service._proposals = {item.id: item for item in items}
        assert service.active_policy() == "\n\n".join(
            [older.change, newer.change]
        ), "порядок должен быть по created_at, а не по вставке"


def test_evolution_stores_are_bounded_and_keep_accepted() -> None:
    service = EvolutionService(provider=None, capacity=3)  # type: ignore[arg-type]
    now = datetime.now(UTC)
    accepted = _proposal(now - timedelta(days=5), status="accepted")
    service._proposals[accepted.id] = accepted
    for index in range(6):
        service._proposals[uuid4()] = _proposal(now + timedelta(minutes=index))
        service._trim()
    assert len(service._proposals) <= 3
    assert accepted.id in service._proposals, "принятая правка не выгружается первой"


class _StateWithExperiments:
    """Минимальный дубль серверного состояния для A/B-вороти."""

    def __init__(self, experiments: list[Any]) -> None:
        self._experiments = experiments

    async def recent_experiments(self, *, limit: int = 100) -> list[Any]:
        return self._experiments[:limit]


class _DepsWithState:
    def __init__(self, state: _StateWithExperiments) -> None:
        self.state = state


@pytest.mark.asyncio
async def test_ab_gate_reads_server_state_not_service_memory() -> None:
    """Принятие возможно, только если вороти смотрят в `nk_experiments`.

    Словарь `EvolutionService` эксперименты больше не получает: проверка по нему
    запирала expert-контур на 409 навсегда.
    """
    from scientific_tangle.api.app import _ab_gate_passed

    now = datetime.now(UTC)
    promoted = _experiment(now).model_copy(update={"decision": "promote"})
    rejected = _experiment(now)
    deps = _DepsWithState(_StateWithExperiments([promoted, rejected]))
    assert await _ab_gate_passed(deps, promoted.proposal_id)  # type: ignore[arg-type]
    assert not await _ab_gate_passed(deps, rejected.proposal_id)  # type: ignore[arg-type]
    empty = _DepsWithState(_StateWithExperiments([]))
    assert not await _ab_gate_passed(empty, promoted.proposal_id)  # type: ignore[arg-type]


def test_claim_history_survives_a_restart_of_the_process() -> None:
    """Версии тезиса живут в самих записях, а не только в словаре процесса.

    `_version_chains` — память процесса: после перезапуска в neo4j-контуре
    находки возвращаются из индексов, и история обязана восстановиться по
    `superseded_by`, иначе аналитик увидит одну версию и не узнает о правке.
    """
    from scientific_tangle.domain.contracts import (
        DocumentRequest,
        ExtractedClaim,
        ExtractionResult,
    )
    from scientific_tangle.services.knowledge import InMemoryKnowledgeBase

    knowledge = InMemoryKnowledgeBase()
    knowledge.ingest(
        DocumentRequest(title="Отчёт", text="Задержание солей 98 % при 25 °C."),
        ExtractionResult(
            entities=[],
            claims=[
                ExtractedClaim(
                    subject="Мембрана",
                    predicate="HAS_PROPERTY",
                    object="Мембрана",
                    statement="Задержание солей 98 %.",
                    confidence=0.8,
                    evidence_quote="Задержание солей 98 %",
                )
            ],
        ),
    )
    original = knowledge.all_findings()[0].id
    knowledge.supersede_finding(original, "Задержание солей 99 %.", 0.9)

    versions = knowledge.claim_history(original)
    assert [item.version for item in versions] == [1, 2]

    knowledge._version_chains = {}  # имитация перезапуска: каталог есть, цепочек нет
    restored = knowledge.claim_history(original)
    assert [item.version for item in restored] == [1, 2]
    assert {item.id for item in restored} == {item.id for item in versions}


def test_supersede_edge_connects_existing_graph_nodes() -> None:
    """Концы SUPERSEDES — id узлов графа: у находок каталога префикс finding-,
    которого в графе нет, и такое ребро висит в воздухе, а в Neo4j ещё и
    молча не создаётся на MERGE концов."""
    from scientific_tangle.domain.contracts import (
        DocumentRequest,
        ExtractedClaim,
        ExtractionResult,
    )
    from scientific_tangle.services.knowledge import InMemoryKnowledgeBase

    knowledge = InMemoryKnowledgeBase()
    knowledge.ingest(
        DocumentRequest(title="Отчёт", text="Задержание солей 98 % при 25 °C."),
        ExtractionResult(
            entities=[],
            claims=[
                ExtractedClaim(
                    subject="Мембрана",
                    predicate="HAS_PROPERTY",
                    object="Мембрана",
                    statement="Задержание солей 98 %.",
                    confidence=0.8,
                    evidence_quote="Задержание солей 98 %",
                )
            ],
        ),
    )
    original = knowledge.all_findings()[0].id
    knowledge.supersede_finding(original, "Задержание солей 99 %.", 0.9)

    edge = next(
        item for item in knowledge.full_graph().edges if item.relation == "SUPERSEDES"
    )
    node_ids = {node.id for node in knowledge.full_graph().nodes}
    assert edge.source in node_ids, "новая версия не представлена узлом графа"
    assert edge.target in node_ids, "ребро ссылается на несуществующий узел"


def test_prompt_payload_keeps_cyrillic_readable() -> None:
    """Экранирование model_dump_json раздувало промпт в разы — проверяем формат."""
    payload = json.dumps(
        {"statement": "Обратный осмос"}, ensure_ascii=False, default=str
    )
    assert "Обратный осмос" in payload and "\\u" not in payload


def test_point_observation_rejects_stray_bounds() -> None:
    """observation_bounds берёт min/max первым: случайно заполненные границы у `eq`
    превращают точку в интервал, и конфликт считается уже по другим числам."""
    with pytest.raises(ValidationError, match="принадлежат between"):
        NumericObservation(
            property_name="recovery",
            operator="eq",
            value=85.0,
            normalized_value=85.0,
            min_value=80.0,
            max_value=90.0,
            unit="%",
            normalized_unit="%",
            raw_text="85 %",
        )


def test_range_observation_rejects_point_fields() -> None:
    with pytest.raises(ValidationError, match="between задаётся границами"):
        NumericObservation(
            property_name="recovery",
            operator="between",
            value=85.0,
            min_value=80.0,
            max_value=90.0,
            normalized_min=80.0,
            normalized_max=90.0,
            unit="%",
            normalized_unit="%",
            raw_text="80–90 %",
        )


def test_range_rejects_inverted_normalized_bounds() -> None:
    """Пересчёт в обратную шкалу разворачивает границы — их тоже надо сверять."""
    with pytest.raises(ValidationError, match="не может превышать"):
        NumericObservation(
            property_name="recovery",
            operator="between",
            min_value=80.0,
            max_value=90.0,
            normalized_min=95.0,
            normalized_max=85.0,
            unit="%",
            normalized_unit="%",
            raw_text="80–90 %",
        )


def test_both_producer_shapes_still_validate() -> None:
    """Сиды knowledge.py строят ровно эти две формы — инвариант их не ломает."""
    assert _obs(85.0, "eq").value == 85.0
    between = _obs(0.0, "between")
    assert between.min_value == 80.0 and between.value is None


@pytest.mark.asyncio
async def test_proposal_queue_and_active_policy_survive_a_restart() -> None:
    """Перезапуск не имеет права молча менять поведение агента.

    Предложения держатся в `nk_evolution_proposals`, активная политика промпта
    выводится из принятых — значит после подъёма она обязана восстановиться.
    """
    from scientific_tangle.domain.contracts import EvolutionProposal
    from scientific_tangle.services.durable_state import InMemoryDurableState

    state = InMemoryDurableState()
    older = EvolutionProposal(
        source_query_id=uuid4(),
        kind="prompt",
        title="Ссылаться на лист",
        change="Цитируй лист.",
        impact=["middle"],
    )
    newer = older.model_copy(
        update={"id": uuid4(), "title": "Единицы", "change": "Единицы обязательны."}
    )
    await state.record_proposal(older)
    await state.record_proposal(newer.model_copy(update={"status": "accepted"}))

    restored = await state.recent_proposals()
    assert [item.id for item in restored] == [newer.id, older.id]

    service = EvolutionService(provider=None)  # type: ignore[arg-type]
    for proposal in restored:
        service.restore(proposal)
    assert service.active_policy() == "Единицы обязательны."


def test_repair_attempts_are_counted_where_they_actually_happen() -> None:
    """Повтор принадлежит обращению к модели, а не агенту.

    Раньше дашборд показывал «повторов» в строке агента, хотя счётчик не
    incrementился ни одним вызовом: отображаемое число обязано иметь источник.
    """
    registry = AgentMetricsRegistry()
    registry.observe("reasoner", 120.0, True)
    registry.observe_llm("ReasoningResult", 120.0, success=True)
    registry.observe_llm_retry("ReasoningResult")
    snapshot = registry.snapshot()
    assert [item.retries for item in snapshot.llm] == [1]
    assert not hasattr(snapshot.agents[0], "retries")


def test_checkpoint_thread_belongs_to_the_account_not_to_the_client() -> None:
    """Один и тот же клиентский ``thread_id`` у двух аккаунтов — две разные ветки.

    Иначе follow-up чужого исследования попал бы в промпт второго аналитика, а
    ветка первого была бы перезаписана.
    """
    from scientific_tangle.api.app import _thread_scoped
    from scientific_tangle.domain.contracts import QueryRequest
    from scientific_tangle.services.accounts import Account

    thread = UUID(int=42)
    query = QueryRequest(question="Задержка солей мембраной?", thread_id=thread)
    first = Account(
        id="acc-1",
        email="a@example.test",
        display_name="А",
        password_hash="x",
        review_enabled=False,
        created_at=datetime.now(UTC),
    )
    second = Account(
        id="acc-2",
        email="b@example.test",
        display_name="Б",
        password_hash="x",
        review_enabled=False,
        created_at=datetime.now(UTC),
    )

    assert _thread_scoped(first, query).thread_id == _thread_scoped(first, query).thread_id
    assert _thread_scoped(first, query).thread_id != _thread_scoped(second, query).thread_id
    assert _thread_scoped(first, query).question == query.question
