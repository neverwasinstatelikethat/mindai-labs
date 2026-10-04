"""Боевые проверки хранилищ на реальных драйверах — чтение, по умолчанию без записи.

Cypher-обход, оконное чтение из Elasticsearch и ACL-срез в fake-эмуляции не
доказываются: фейковый драйвер моделирует то, что в него заложил автор теста.
Здесь те же пути исполняются настоящим neo4j-драйвером и настоящим ES-клиентом.

Запуск выключен, пока не заданы оба адреса, чтобы обычный `pytest` не зависел от
поднятого контура:

    LIVE_NEO4J_URI=bolt://127.0.0.1:44917 LIVE_NEO4J_PASSWORD=... \\
    LIVE_ELASTICSEARCH_URL=http://127.0.0.1:42733 pytest tests/test_live_stores.py

Запись в хранилище включается только одним словом — `LIVE_SEED=throwaway` — и
только когда корпус пуст: у storage-слоя нет пространства имён на прогон (индексы
заданы модульными константами, отдельной Neo4j-базы в community-редакции нет), а
удаления документа в продукте нет, значит засеянное из рабочего корпуса аналитиков
нечем убрать. Для одноразового контейнера в CI рецепт ровно такой: поднять
свежие Elasticsearch и Neo4j (с пустым volume), дать им прогрееться и запустить
модуль с `LIVE_SEED=throwaway`. Без засевки на пустом контуре тесты НЕ зелёные, а
пропущенные с причиной: сличить два нуля — не измерение. Порог и его таблица
решений живут в `tests/live_support.py`, офлайн проверяются в
`tests/test_live_support.py`.

Ветка записи статуса (`set_finding_status`) остаётся вне этих тестов: её проверяли
живым контуром отдельно, переключением реальной находки в `disputed` с возвратом к
исходному значению (см. docs/architecture-review/review.md).
"""

from __future__ import annotations

import os

import pytest

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import RetrievalPlan
from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import QueryPlan
from scientific_tangle.services.infrastructure import Neo4jElasticsearchKnowledgeBase
from tests.live_support import corpus_gate, seed_corpus

LIVE_NEO4J_URI = os.environ.get("LIVE_NEO4J_URI", "")
LIVE_ELASTICSEARCH_URL = os.environ.get("LIVE_ELASTICSEARCH_URL", "")
# Разрешает запись в хранилище ровно одним словом: пустой боевой прогон иначе был
# бы «зелёным ничем», а случайная единица в CI-переменной не должна пачкать базу
# аналитиков (см. tests/live_support.py).
LIVE_SEED = os.environ.get("LIVE_SEED", "")

pytestmark = pytest.mark.skipif(
    not (LIVE_NEO4J_URI and LIVE_ELASTICSEARCH_URL),
    reason="боевой контур не задан: LIVE_NEO4J_URI и LIVE_ELASTICSEARCH_URL",
)


@pytest.fixture(scope="module")
def knowledge():
    settings = Settings(
        knowledge_backend="neo4j",
        neo4j_uri=LIVE_NEO4J_URI,
        neo4j_username=os.environ.get("LIVE_NEO4J_USER", "neo4j"),
        neo4j_password=os.environ.get("LIVE_NEO4J_PASSWORD", ""),
        elasticsearch_url=LIVE_ELASTICSEARCH_URL,
    )
    store = Neo4jElasticsearchKnowledgeBase(settings)
    store.warmup()
    gate = corpus_gate(
        documents=store.document_count(),
        findings=len(store.all_findings(ALL_CLASSES)),
        seed_mode=LIVE_SEED,
    )
    if gate.decision == "seed":
        # `ingest`, а не структурный импорт: одноразовому контуру нужны находки,
        # сущности и пара с непересекающимися диапазонами — без обращения к модели.
        for document, extraction in seed_corpus():
            store.ingest(document, extraction)
    elif gate.decision == "skip":
        store.close()
        pytest.skip(gate.reason)
    yield store
    store.close()


def _plan(query: str, *, global_context: bool = False) -> RetrievalPlan:
    return RetrievalPlan(
        lexical_query=query,
        semantic_query=query,
        entity_names=[],
        relation_types=["HAS_PROPERTY"],
        max_hops=3,
        use_global_context=global_context,
    )


ALL_CLASSES = {DataClass.PUBLIC, DataClass.INTERNAL, DataClass.RESTRICTED}


def test_live_corpus_is_not_a_vacuum(knowledge) -> None:
    """Порог сработал: прогон идёт по непустому корпусу, а не сличает два нуля.

    Отдельная проверка нужна затем, чтобы ослабление порога в будущем стало
    падением, а не тихой зелёной строчкой в отчёте.
    """
    assert knowledge.document_count() > 0
    assert knowledge.all_findings(ALL_CLASSES)


def test_windowed_read_agrees_with_the_full_catalog(knowledge) -> None:
    """Окно выдачи — запрос к индексу, а не срез над списком процесса.

    Порядок окна обязан совпадать с порядком тех же записей в полном каталоге:
    иначе пагинация интерфейса показывает смесь из разных проходов, а
    `X-Total-Count` расходится с содержимым.
    """
    allowed = {DataClass.PUBLIC, DataClass.INTERNAL}
    catalog = knowledge.all_findings(allowed)
    window = knowledge.findings_window(limit=2, offset=0, allowed_data_classes=allowed)
    assert len(window.findings) <= 2
    assert window.total == len(catalog)
    assert (window.offset, window.limit) == (0, 2)
    catalog_order = [finding.id for finding in catalog]
    positions = [catalog_order.index(item.id) for item in window.findings]
    assert positions == sorted(positions)


def test_second_window_does_not_repeat_the_first(knowledge) -> None:
    allowed = {DataClass.PUBLIC, DataClass.INTERNAL}
    first = knowledge.findings_window(limit=2, offset=0, allowed_data_classes=allowed)
    second = knowledge.findings_window(limit=2, offset=2, allowed_data_classes=allowed)
    assert not {item.id for item in first.findings} & {item.id for item in second.findings}


def test_filters_are_executed_by_the_index_not_by_slicing_the_page(knowledge) -> None:
    """`status` и `subject` на боевом Elasticsearch отсекаются запросом.

    Сверка с каталогом процесса: предикаты обязаны совпадать по составу, иначе
    страница списка и экран «N расхождений» разойдутся с `/corpus/stats` — ровно та
    ловушка, из-за которой окно и каталог раньше спорили.
    """
    allowed = {DataClass.PUBLIC, DataClass.INTERNAL}
    catalog = knowledge.all_findings(allowed)
    for status in ("disputed", "consensus", "hypothesis"):
        expected = {item.id for item in catalog if item.status == status}
        window = knowledge.findings_window(limit=500, allowed_data_classes=allowed, status=status)
        assert {item.id for item in window.findings} == expected, status
        assert window.total == len(expected), status
    subjects = sorted({item.subject for item in catalog if item.subject})
    for subject in subjects[:3]:
        needle = subject[:4].lower()
        expected = {
            item.id for item in catalog if needle in (item.subject or "").lower()
        }
        window = knowledge.findings_window(limit=500, allowed_data_classes=allowed, subject=needle)
        assert {item.id for item in window.findings} == expected, needle


def test_retrieval_runs_the_real_cypher_and_respects_acl(knowledge) -> None:
    """Обход по настоящему Neo4j: якоря, рёбра и срез прав исполняет драйвер."""
    question = QueryPlan(question="задержание солей мембраной", language="ru", mode="hybrid")
    public_only = knowledge.retrieve(question, _plan("задержание солей"), {DataClass.PUBLIC})
    everything = knowledge.retrieve(question, _plan("задержание солей"), ALL_CLASSES)
    assert all(finding.data_class == DataClass.PUBLIC for finding in public_only.findings)
    # Узкий срез не может дать больше доказательств, чем широкий.
    assert len(public_only.findings) <= len(everything.findings)


def test_global_mode_reads_communities_from_the_real_graph(knowledge) -> None:
    question = QueryPlan(
        question="сравнение технологий обессоливания", language="ru", mode="global"
    )
    context = knowledge.retrieve(question, _plan("обессоливание", global_context=True), None)
    # На малом корпусе сообществ может не быть — но это различимый пустой ответ,
    # а не сбой обхода: сводки читаются с настоящего графа.
    assert isinstance(context.community_summaries, list)


def test_corpus_stats_come_from_the_same_graph_as_the_snapshot(knowledge) -> None:
    stats = knowledge.corpus_stats()
    graph = knowledge.full_graph(ALL_CLASSES)
    claims = sum(1 for node in graph.nodes if node.type.value == "claim")
    publications = sum(1 for node in graph.nodes if node.type.value == "publication")
    assert stats.claims == claims
    assert stats.documents == publications
    assert stats.semantic_documents <= stats.documents


def test_graph_snapshot_is_a_subgraph_not_a_pair_of_unrelated_slices(knowledge) -> None:
    """Снимок полного графа обязан быть подграфом: рёбра только между показанными узлами.

    Пока узлы резались по алфавиту метки, а рёбра независимо по `rel.id`, два лимита
    пересекались случайно, и обходу доставалось 120 узлов из 600 (замер 2 октября на
    1501 узле). Алфавитный префикс к тому же смещал витрину к одной области: имена не
    из начала алфавита в снимок не попадали, даже будучи центрами связей.
    """
    graph = knowledge.full_graph(ALL_CLASSES)
    ids = {node.id for node in graph.nodes}
    for edge in graph.edges:
        assert edge.source in ids and edge.target in ids, (
            f"ребро {edge.relation} ведёт на узел вне среза: снимок не подграф"
        )
    connected = {edge.source for edge in graph.edges} | {edge.target for edge in graph.edges}
    assert connected, "в непустом корпусе не одной связи — срез сломан"
    # Порядок отсечения: снимок держит связное ядро, а не набор имён.
    assert len(graph.communities) <= len(graph.nodes)
