"""Гонки импорта: параллельные прогоны одного и того же документа.

Внешний слой вызывает хранилище через ``asyncio.to_thread``, и потоков столько,
сколько аналитиков загрузили файл в одну секунду. Проверки собраны здесь, а не в
``test_ingestion.py``/``test_retrieval_persistence.py``, потому что им нужен свой
способ запуска: барьер перед общей стартовой точкой и наблюдатель за состоянием
процесса во время записи.

Два дефекта, которые тесты держат:

* TOCTOU дедупа — «проверили, что находки не извлечены», написали позже; оба
  потока проходили проверку и гнали ``_seed.ingest``/``_write_graph`` заново.
* Мутации каталога, графа и кэшей из рабочих потоков без замка — испорченный
  каталог (дубли узлов, потерянные находки) и ``RuntimeError`` на обходе словаря,
  в который одновременно пишут.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest

from scientific_tangle.domain.contracts import DocumentRequest
from scientific_tangle.services import infrastructure
from scientific_tangle.services.infrastructure import CHUNK_INDEX, FINDING_INDEX
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from tests.test_retrieval_persistence import build_backend, extraction, structural_document

THREADS = 8


def run_in_threads(action: Callable[[int], Any], workers: int = THREADS) -> list[Any]:
    """Один старт по барьеру: без него потоки расходились бы по очереди.

    barrier нужен ровно один — иначе проверяющий видел бы последовательные
    импорты и проходил бы мимо гонки, ради которой написан тест.
    """
    gate = threading.Barrier(workers)
    results: list[Any] = [None] * workers
    errors: list[BaseException] = []

    def body(index: int) -> None:
        gate.wait()
        try:
            results[index] = action(index)
        except BaseException as error:  # noqa: BLE001 - сбой потока обязан попасть в проверку
            errors.append(error)

    runners = [threading.Thread(target=body, args=(index,)) for index in range(workers)]
    for runner in runners:
        runner.start()
    for runner in runners:
        runner.join(timeout=60)

    assert not errors, f"рабочие потоки упали: {errors!r}"
    assert all(not runner.is_alive() for runner in runners), "импорт не завершился за 60 с"
    return results


def semantic_document(seed: int) -> DocumentRequest:
    return structural_document(f"Текст документа номер {seed} для параллельного импорта.")


def claim_findings(knowledge: InMemoryKnowledgeBase) -> list[str]:
    return [key for key in knowledge._findings if key.startswith("finding-claim-")]


# ─ 1. memory-контур: один документ пишется ровно один раз ──────────────────


def test_parallel_ingest_of_the_same_document_writes_it_once() -> None:
    knowledge = InMemoryKnowledgeBase()
    document = semantic_document(1)
    before = knowledge._graph_epoch

    receipts = run_in_threads(lambda index: knowledge.ingest(document, extraction()))

    created = [item for item in receipts if item.status == "created"]
    assert len(created) == 1, f"документ записан {len(created)} раз: дедуп пропускает гонку"
    assert all(item.status == "duplicate" for item in receipts if item not in created)
    assert claim_findings(knowledge), "единственный успех обязан дать находки"
    # Каталог не должен ни дублироваться, ни терять записи: ровно по одному ключу
    # на находку и ровно один шаг поколения графа на весь прогон.
    assert len(set(knowledge._findings)) == len(knowledge._findings)
    node_ids = [node.id for node in knowledge._graph.nodes]
    assert len(set(node_ids)) == len(node_ids), "узлы графа записаны дважды"
    edge_ids = [edge.id for edge in knowledge._graph.edges]
    assert len(set(edge_ids)) == len(edge_ids), "рёбра графа записаны дважды"
    assert knowledge._graph_epoch == before + 1


def test_parallel_structural_import_of_the_same_document_writes_chunks_once() -> None:
    knowledge = InMemoryKnowledgeBase()
    document = semantic_document(2)

    receipts = run_in_threads(lambda index: knowledge.index_document(document, "/data/x.docx"))

    created = [item for item in receipts if item.status == "created"]
    assert len(created) == 1
    assert {item.chunks for item in created} == {created[0].chunks}
    chunk_ids = [key for key in knowledge._findings if key.startswith("chunk-")]
    assert len(set(chunk_ids)) == len(chunk_ids), "чанки одного документа задвоены"
    node_ids = [node.id for node in knowledge._graph.nodes]
    assert len(set(node_ids)) == len(node_ids)


def test_parallel_ingest_of_different_documents_keeps_everything() -> None:
    """Разные документы не теряют находок: каждый поток пишет свою часть корпуса."""
    knowledge = InMemoryKnowledgeBase()

    run_in_threads(lambda index: knowledge.ingest(semantic_document(index), extraction()))

    documents = {
        str(evidence.document_id)
        for finding in knowledge.all_findings()
        for evidence in finding.evidence
    }
    assert len(documents) >= 2
    assert claim_findings(knowledge)
    node_ids = [node.id for node in knowledge._graph.nodes]
    assert len(set(node_ids)) == len(node_ids), "параллельные импорты задвоили узлы"


def test_graph_epoch_is_monotonic_and_never_lost() -> None:
    """Поколение графа растёт на каждую запись и никогда не откатывается.

    ``_graph_epoch`` входит в ключ кэша сводок сообществ: потерянный инкремент
    означал бы, что новый профиль сообществ отдаётся по старому графу, а счётчик
    «монотонен» ловит это напрямую.
    """
    knowledge = InMemoryKnowledgeBase()
    start_epoch = knowledge._graph_epoch
    samples: list[int] = []
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            samples.append(knowledge._graph_epoch)

    watcher = threading.Thread(target=sampler)
    watcher.start()
    try:
        run_in_threads(
            lambda index: knowledge.ingest(semantic_document(10 + index), extraction())
        )
    finally:
        stop.set()
        watcher.join(timeout=10)

    assert samples, "наблюдатель не успел ничего прочитать"
    assert all(later >= earlier for earlier, later in zip(samples, samples[1:], strict=False)), (
        "поколение графа пошло назад"
    )
    assert knowledge._graph_epoch == start_epoch + THREADS, (
        "часть приращений поколения потеряна"
    )


def test_reads_during_parallel_writes_never_teardown_the_catalog() -> None:
    """Чтение каталога, графа и выдачи одновременно с записью не роняет процесс.

    Прежний обход ``self._findings`` в потоке-читателе против вставки в потоке-
    писателе поднимал ``RuntimeError: dictionary changed size during iteration`` —
    то есть 500 на запросе, который к записи отношения не имеет.
    """
    knowledge = InMemoryKnowledgeBase()
    errors: list[BaseException] = []
    stop = threading.Event()
    writes = 4

    def reader() -> None:
        while not stop.is_set():
            try:
                knowledge.all_findings()
                knowledge.full_graph()
                knowledge.rank_findings("обратный осмос", 3, "hybrid")
                knowledge.corpus_stats()
            except BaseException as error:  # noqa: BLE001 - сбой потока идёт в проверку
                errors.append(error)
                return

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for reader_thread in readers:
        reader_thread.start()
    try:
        run_in_threads(
            lambda index: knowledge.ingest(semantic_document(20 + index), extraction()),
            workers=writes,
        )
    finally:
        stop.set()
        for reader_thread in readers:
            reader_thread.join(timeout=10)

    assert not errors, f"чтение во время записи упало: {errors!r}"
    assert claim_findings(knowledge)


def test_parallel_supersede_of_the_same_finding_creates_one_version() -> None:
    """Два эксперта на одной карточке: вторая попытка отклонена, а не записана."""
    knowledge = InMemoryKnowledgeBase()
    knowledge.ingest(semantic_document(30), extraction())
    target = claim_findings(knowledge)[0]

    def attempt(index: int) -> Any:
        try:
            return ("ok", knowledge.supersede_finding(target, f"Уточнение {index}.", 0.8))
        except ValueError as error:
            return ("rejected", str(error))

    results = run_in_threads(attempt, workers=4)

    accepted = [item for item in results if item[0] == "ok"]
    assert len(accepted) == 1
    assert [edge.id for edge in knowledge._graph.edges if edge.relation == "SUPERSEDES"] == [
        f"supersedes-{accepted[0][1].id}"
    ]
    assert list(knowledge._findings).count(f"{target}-v2") == 1


# ─ 2. рабочий контур: дедуп по Neo4j закрывается замком на документ ────────


def test_neo4j_parallel_semantic_ingest_writes_the_document_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_backend(monkeypatch)
    document = semantic_document(40)
    epochs: list[int] = []
    stop = threading.Event()

    def sampler() -> None:
        while not stop.is_set():
            epochs.append(harness.knowledge.graph_epoch)

    watcher = threading.Thread(target=sampler)
    watcher.start()
    try:
        receipts = run_in_threads(
            lambda index: harness.knowledge.ingest(document, extraction())
        )
    finally:
        stop.set()
        watcher.join(timeout=10)

    assert sum(item.status == "created" for item in receipts) == 1
    written = harness.driver.issued_containing("SET d.semantic_extracted = true")
    assert len(written) == 1, "граф размечен дважды: дедуп пропущен вторым потоком"
    indexed = [action["_id"] for action in harness.helpers.actions(FINDING_INDEX)]
    assert len(indexed) == len(set(indexed)), "находки записаны в индекс дважды"
    assert harness.knowledge.graph_epoch == 1
    assert all(later >= earlier for earlier, later in zip(epochs, epochs[1:], strict=False))


def test_neo4j_parallel_structural_import_writes_chunks_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_backend(monkeypatch)
    document = semantic_document(41)

    receipts = run_in_threads(
        lambda index: harness.knowledge.index_document(document, "/data/sources/x.docx")
    )

    assert sum(item.status == "created" for item in receipts) == 1
    published = [action["_id"] for action in harness.helpers.actions(CHUNK_INDEX)]
    assert len(published) == len(set(published)), "чанки ушли в индекс дважды"
    chunk_ids = [finding.id for finding in harness.knowledge.all_findings()]
    assert len(chunk_ids) == len(set(chunk_ids))
    assert harness.knowledge.graph_epoch == 1


def test_neo4j_parallel_distinct_imports_keep_the_catalog_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Разные документы импортируются одновременно и не затирают каталоги друг друга.

    Присваивание ``self._findings = <старый снимок>`` на одном из путей теряло
    находки уже завершившегося импорта: каталог процесса обязан остаться
    надмножеством всех записанных находок.
    """
    harness = build_backend(monkeypatch)

    def body(index: int) -> Any:
        document = semantic_document(50 + index)
        structural = harness.knowledge.index_document(document, f"/data/sources/{index}.docx")
        semantic = harness.knowledge.ingest(document, extraction())
        return structural.status, semantic.status

    run_in_threads(body, workers=4)

    catalog = harness.knowledge._snapshot_findings()
    assert catalog, "каталог процесса пуст после параллельной загрузки"
    assert len(catalog) == len(set(catalog))
    listed = {finding.id for finding in harness.knowledge.all_findings()}
    assert set(catalog) >= listed - {
        key for key, item in catalog.items() if item.superseded_by is not None
    }
    assert harness.knowledge.graph_epoch >= 4


def test_in_memory_backend_import_survives_repeated_identical_calls() -> None:
    """Повторный импорт того же текста — duplicate, а не половины состояний.

    Откат при отказе обязан освобождать дедуп-ключ: иначе сбой превращается в
    «документ уже есть», и второй прогон не состоится никогда.
    """
    knowledge = InMemoryKnowledgeBase()
    document = semantic_document(60)
    original = InMemoryKnowledgeBase._add_extraction
    calls: list[int] = []

    def broken(self: InMemoryKnowledgeBase, *args: Any, **kwargs: Any) -> None:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("извлечение прервано")
        return original(self, *args, **kwargs)

    InMemoryKnowledgeBase._add_extraction = broken  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="извлечение прервано"):
            knowledge.ingest(document, extraction())
    finally:
        InMemoryKnowledgeBase._add_extraction = original  # type: ignore[method-assignment]

    assert knowledge.ingest(document, extraction()).status == "created"
    assert knowledge.ingest(document, extraction()).status == "duplicate"
    node_ids = [node.id for node in knowledge._graph.nodes]
    assert len(set(node_ids)) == len(node_ids)


def test_infrastructure_module_does_not_import_provider_at_load_time() -> None:
    """Проверка «нет цикла импорта»: knowledge тянет provider только внутри вызова.

    ``services/provider.py`` не импортирует knowledge-модуль, но круг через
    ``api.app`` возник бы на первом же import-е, если бы сброс кэша был на уровне
    модуля. Тест держит это как контракт, а не как случайность текущего файла.
    """
    assert "scientific_tangle.services.provider" not in getattr(
        infrastructure, "__dict__", {}
    ), "provider импортирован в модуль infrastructure верхним уровнем"
    import scientific_tangle.services.knowledge as knowledge_module

    assert not any(
        name == "provider" and value is not None
        for name, value in vars(knowledge_module).items()
        if name == "provider"
    ), "provider импортирован в модуль knowledge верхним уровнем"
