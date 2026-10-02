"""Кооперативная отмена retrieval: снятый прогон не начинает платные фазы.

Проверяется обещание продукта, а не формулировка заметок. У аналитика, который
закрыл вкладку, и у запроса, у которого вышел дедлайн, не оплачивается векторная
ветка, не считается обход графа и не строится полный граф под сигнал ранжирования;
при этом уже собранное доходит до ответа с явной отметкой о неполноте. Отдельно
держится равенство формулировок memory- и neo4j-контура: разные слова об одном и
том же срезе означали бы разные ответы на один план.
"""

from __future__ import annotations

import asyncio
import threading
from time import monotonic, sleep
from typing import Any

import pytest

from scientific_tangle.agents.tools import ResearchToolExecutor, RunBudget
from scientific_tangle.domain.contracts import GraphSnapshot, RetrievalPlan
from scientific_tangle.domain.models import QueryPlan
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase, RetrievalContext
from scientific_tangle.services.retrieval_semantics import AbortCheck, abort_note
from tests.test_retrieval_persistence import FakeEmbeddings, build_backend
from tests.test_workflow import planning_bundle

# Маркер запроса якорей обхода: пока он не выдан, Neo4j не видел ни одного
# обращения от снятого прогона.
ANCHOR_MARKER = "MATCH (anchor:Entity)"
# Маркер одного уровня BFS — то, что обязан не начать прерванный на середине обход.
EXPAND_MARKER = "UNWIND $frontier AS fid"


def _plans() -> tuple[QueryPlan, RetrievalPlan]:
    """План, в котором есть все платные фазы: вектор, локальный граф, сводки.

    Якорь взят из seed-контента memory-адаптера, чтобы проверка «полный прогон
    действительно строит граф» не зависела от того, угадан ли запрос корпуса.
    """
    query_plan = QueryPlan(question="задержание солей мембраной", language="ru", mode="hybrid")
    retrieval_plan = RetrievalPlan(
        lexical_query="задержание солей",
        semantic_query="задержание солей мембраной",
        entity_names=["шахтная вода"],
        relation_types=["HAS_PROPERTY"],
        max_hops=4,
        use_global_context=True,
        use_community_context=True,
        use_local_graph=True,
    )
    return query_plan, retrieval_plan


def test_cancelled_run_starts_no_paid_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отмена до начала retrieval — ни эмбеддингов, ни драйвера, ни полного графа.

    Лексическая ветка остаётся: она уже в единственном обращении к индексу и ничего
    не стоит за его пределами. Пропущенные фазы названы в деградации, иначе
    неполная выдача выглядела бы как «доказательств нет».
    """
    embeddings = FakeEmbeddings()
    harness = build_backend(monkeypatch, embeddings=embeddings)
    harness.es.hits = {}
    query_plan, retrieval_plan = _plans()

    context = harness.knowledge.retrieve(query_plan, retrieval_plan, None, abort=lambda: True)

    assert embeddings.queries == []
    assert harness.driver.issued_containing(ANCHOR_MARKER) == []
    assert harness.driver.issued_containing(EXPAND_MARKER) == []
    reasons = " ".join(context.degradation_reasons)
    for phase in ("векторная ветка", "обход графа", "сводки сообществ", "ранжировании"):
        assert phase in reasons


def test_abort_flipping_midflight_cuts_the_traversal_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Флаг, поднятый во время прогона, обрывает обход на границе уровня.

    Начатый Cypher отменить нельзя, но следующий уровень прогон не оплачивает —
    именно это и отличает кооперативную отмену от проверки только при
    диспетчеризации.
    """
    state = {"calls": 0}

    def abort() -> bool:
        # Первая граница — ещё не отмена (лексика и якоря работают), дальше — да.
        state["calls"] += 1
        return state["calls"] > 1

    harness = build_backend(monkeypatch, graph_nodes=[{"id": "n-1", "label": "Мембрана"}])
    harness.es.hits = {}
    query_plan, retrieval_plan = _plans()

    context = harness.knowledge.retrieve(query_plan, retrieval_plan, None, abort=abort)

    assert len(harness.driver.issued_containing(EXPAND_MARKER)) < retrieval_plan.max_hops
    assert "обход графа" in " ".join(context.degradation_reasons)


def test_memory_contour_names_the_same_skipped_phases() -> None:
    """Оба бэкенда рапортуют пропущенные фазы одними словами и одинаково пустят граф."""
    knowledge = InMemoryKnowledgeBase()
    query_plan, retrieval_plan = _plans()

    live = knowledge.retrieve(query_plan, retrieval_plan)
    aborted = knowledge.retrieve(query_plan, retrieval_plan, abort=lambda: True)

    assert any(abort_note("обход графа") == note for note in aborted.degradation_reasons)
    assert any(abort_note("сводки сообществ") == note for note in aborted.degradation_reasons)
    assert aborted.graph.nodes == []
    assert aborted.community_summaries == []
    # Полоса не «пусто, потому что корпуса нет»: тот же план без отмены граф строит,
    # и заметок о пропущенных фазах в его деградации нет.
    assert live.graph.nodes != []
    assert live.community_summaries != []
    skipped = {abort_note("обход графа"), abort_note("сводки сообществ")}
    assert not skipped & set(live.degradation_reasons)


def test_run_budget_reports_stop_to_worker_threads() -> None:
    """Снятие с прогона делает исчерпанным и резерв, и нулевой остаток.

    Потолок приёма и очередь модели смотрят на `exhausted()`, retrieval на границе
    фаз — на `aborted` без резерва: оба обязаны видеть поднятый флаг, иначе поток
    продолжил бы работу над ответом, который никто не ждёт.
    """
    budget = RunBudget(deadline_at=monotonic() + 120)

    assert not budget.exhausted()
    assert not budget.aborted

    budget.stop()

    assert budget.exhausted()
    assert budget.aborted


class _BlockingKnowledge:
    """Retrieval, который висит внутри вызова до отмены: так проверяется поток.

    Никакой поддельная хранилище не воспроизведёт главную трудность — worker-поток
    с ``to_thread`` нельзя выдернуть из середины, поэтому ждём реально до сигнала.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.abort_seen = False

    def retrieve(
        self,
        plan: Any,
        retrieval_plan: Any,
        allowed_data_classes: Any = None,
        *,
        abort: AbortCheck | None = None,
    ) -> RetrievalContext:
        self.entered.set()
        limit = monotonic() + 5.0
        while monotonic() < limit and not (abort is not None and abort()):
            sleep(0.01)
        self.abort_seen = abort is not None and abort()
        self.finished.set()
        return RetrievalContext(
            findings=[],
            graph=GraphSnapshot(nodes=[], edges=[], communities=[]),
            community_summaries=[],
            no_evidence=True,
        )


@pytest.mark.asyncio
async def test_cancelling_a_run_signals_the_retrieval_thread() -> None:
    """Отмена задачи прогона долетает до потока, в котором идёт retrieval.

    Раньше `await` снимался, а поток досчитывал план до конца и держал поток пула
    чтения: теперь флаг поднят на отмене, и поток выходит на первой же границе фаз.
    """
    knowledge = _BlockingKnowledge()
    executor = ResearchToolExecutor(knowledge)
    bundle = planning_bundle()
    budget = RunBudget(deadline_at=monotonic() + 60)
    task = asyncio.create_task(
        executor.execute(bundle.action_plan, bundle.query_plan, None, budget=budget)
    )

    await asyncio.wait_for(asyncio.to_thread(knowledge.entered.wait), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(asyncio.to_thread(knowledge.finished.wait), timeout=5.0)

    assert knowledge.abort_seen
    assert budget.aborted
