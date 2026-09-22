"""Рабочий контур склейки сущностей: решение по ``id``, след эксперта и переиспользование.

Три свойства, без которых склейка не является операцией над графом:

* мерж — по реальному ``id`` узла. ``label`` описателен: по нему совпадают
  «Мембрана» из закрытого пилота и «Мембрана» из открытого обзора, а ``id``
  различает. Прежний ``MERGE (source:Entity {label: $source})`` создавал фантомные
  узлы вместо связи существующих.
* принятое решение идемпотентно: повторный ``accept`` того же предложения не
  плодит рёбра и не переводит запись в другое состояние.
* ``ALIAS_OF`` читается обратно (``alias_map``) и участвует в разборе следующих
  импортов, иначе эксперт разклеивает то, что модель склеила по-своему.

Предложения регистрируются детерминированным ``id`` (uuid5 от пары «алиас →
канон»): повторный импорт того же документа обновляет запись, а не плодит
дубли в неограниченной очереди.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid5

from neo4j import Driver, GraphDatabase, Session

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    EntityMergeProposal,
    EntityResolutionProposal,
)

logger = logging.getLogger(__name__)

# Пространство имён предложений склейки: одинаковая пара «алиас → канон» обязана
# давать одинаковый id на любом импорте и в любом процессе.
PROPOSAL_NAMESPACE = UUID("3f0d1c1a-6d59-4a4b-8f0e-2c1b9d3f4a55")

_ALIAS_EDGE = """
MATCH (source:Entity {id: $source_id})
MATCH (target:Entity {id: $target_id})
MERGE (source)-[r:ALIAS_OF {proposal_id: $id}]->(target)
SET r.confidence = $confidence, r.active = true, r.proposal_id = $id
"""


def proposal_id_for(source: str, target: str) -> UUID:
    """Детерминированный id предложения пары алиас → канон."""
    return uuid5(PROPOSAL_NAMESPACE, f"{source.casefold()}->{target.casefold()}")


class EntityResolutionWorkbench:
    """Хранилище предложений склейки и исполнение принятых решений."""

    def __init__(self, settings: Settings) -> None:
        self._proposals: dict[UUID, EntityMergeProposal] = {}
        self._driver: Driver | None = None
        if settings.knowledge_backend == "neo4j":
            self._driver = GraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_username, settings.neo4j_password),
            )

    def close(self) -> None:
        """Гасит драйвер Neo4j.

        Без этого пул переживает приложение: тесты и перезапуски процесса
        копили соединения, а закрытие драйвера — единственный способ их отдать.
        """
        driver, self._driver = self._driver, None
        if driver is not None:
            driver.close()

    def register(self, resolutions: list[EntityResolutionProposal]) -> None:
        for resolution in resolutions:
            if resolution.action != "link":
                continue
            proposal = EntityMergeProposal(
                id=proposal_id_for(resolution.mention, resolution.canonical_name),
                source=resolution.mention,
                target=resolution.canonical_name,
                confidence=resolution.confidence,
                rationale=resolution.rationale,
            )
            existing = self._proposals.get(proposal.id)
            # Повторная регистрация принятого решения не должна откатывать статус:
            # импорт того же файла возвращает предложение в «proposed» без
            # пересмотра экспертом, и след в графе теряется.
            if existing is not None and existing.status == "accepted":
                self._proposals[proposal.id] = existing.model_copy(
                    update={"confidence": proposal.confidence, "rationale": proposal.rationale}
                )
                continue
            self._proposals[proposal.id] = proposal
            if self._driver:
                with self._driver.session() as session:
                    session.run(
                        """
                        MERGE (p:ResolutionProposal {id: $id})
                        SET p.source = $source, p.target = $target,
                            p.confidence = $confidence, p.rationale = $rationale,
                            p.status = $status, p.created_at = $created_at
                        """,
                        **self._proposals[proposal.id].model_dump(mode="json"),
                    )

    def list_proposals(self) -> list[EntityMergeProposal]:
        if not self._driver:
            return sorted(self._proposals.values(), key=lambda item: item.created_at, reverse=True)
        with self._driver.session() as session:
            rows = session.run(
                "MATCH (p:ResolutionProposal) RETURN properties(p) AS proposal "
                "ORDER BY p.created_at DESC"
            )
            return [EntityMergeProposal.model_validate(row["proposal"]) for row in rows]

    def get(self, proposal_id: UUID) -> EntityMergeProposal:
        current = self._proposals.get(proposal_id)
        if current is not None:
            return current
        if self._driver is None:
            raise KeyError(proposal_id)
        with self._driver.session() as session:
            row = session.run(
                "MATCH (p:ResolutionProposal {id: $id}) RETURN properties(p) AS proposal",
                id=str(proposal_id),
            ).single()
        if row is None:
            raise KeyError(proposal_id)
        proposal = EntityMergeProposal.model_validate(row["proposal"])
        self._proposals[proposal.id] = proposal
        return proposal

    def review(
        self, proposal_id: UUID, action: str, *, reviewer_id: str | None = None
    ) -> EntityMergeProposal:
        """Принятие/откат решения. Повтор вызова не меняет состояние дважды.

        ``ValueError`` — узлы пары не найдены в графе: склеивать по имени вместо
        идентификатора здесь сознательно не умеют, иначе в графе появляются
        сущности, которых никто не импортировал.
        """
        current = self.get(proposal_id)
        allowed = {
            "accept": "accepted",
            "reject": "rejected",
            "revert": "reverted",
        }
        status = allowed[action]
        if current.status == status:
            # Идемпотентность: повторный accept того же предложения — то же
            # состояние и то же ребро, а не новое значение reviewed_at/reviewer.
            return current
        source_id, target_id = current.source_id, current.target_id
        if self._driver is not None and action == "accept":
            source_id, target_id = self._resolve_ids(current)
        updated = current.model_copy(
            update={
                "status": status,
                "reviewed_at": datetime.now(UTC),
                "reviewer_id": reviewer_id or current.reviewer_id,
                "source_id": source_id,
                "target_id": target_id,
            }
        )
        self._proposals[proposal_id] = updated
        if self._driver is None:
            return updated
        with self._driver.session() as session:
            session.run(
                "MATCH (p:ResolutionProposal {id: $id}) SET p.status = $status",
                id=str(proposal_id),
                status=status,
            )
            if action == "accept":
                session.run(
                    _ALIAS_EDGE,
                    id=str(proposal_id),
                    source_id=source_id,
                    target_id=target_id,
                    confidence=updated.confidence,
                )
            elif action == "revert":
                session.run(
                    "MATCH ()-[r:ALIAS_OF {proposal_id: $id}]->() SET r.active = false",
                    id=str(proposal_id),
                )
        return updated

    def _resolve_ids(self, proposal: EntityMergeProposal) -> tuple[str, str]:
        """Находит ``id`` узлов пары; отсутствие узла — ошибка, а не создание сущности.

        Вызывается только при наличии драйвера (см. ``review``): в контуре без
        Neo4j предложение принимается как запись решения, узлы там проверяет сам
        адаптер знаний.
        """
        if proposal.source_id and proposal.target_id:
            return proposal.source_id, proposal.target_id
        driver = self._driver
        if driver is None:
            raise ValueError("Нет соединения с графом для проверки id сущностей")
        with driver.session() as session:
            source_id = self._entity_id(session, proposal.source)
            target_id = self._entity_id(session, proposal.target)
        if not source_id or not target_id:
            raise ValueError(
                "Сущности предложения не найдены в графе по id: "
                f"{proposal.source!r} -> {proposal.target!r}"
            )
        return source_id, target_id

    @staticmethod
    def _entity_id(session: Session, name: str) -> str | None:
        """Идентификатор сущности по ``id`` либо ``label``: совпадение имени — только
        способ найти узел, но не ключ склейки."""
        rows = session.run(
            "MATCH (e:Entity) WHERE e.id = $name OR e.label = $name RETURN e.id AS id LIMIT 1",
            name=name,
        )
        row = rows.single()
        return str(row["id"]) if row else None

    def alias_map(self) -> dict[str, str]:
        """Принятые склейки: алиас → канон.

        Читается при разборе следующих импортов (``services/ingestion.py``), чтобы
        модель не сводила синонимы заново вопреки решению эксперта. В рабочем
        контуре берётся и из графа: ``ALIAS_OF`` переживает перезапуск процесса,
        а словарь предложений — нет.
        """
        aliases = {
            item.source.casefold(): item.target
            for item in self._proposals.values()
            if item.status == "accepted"
        }
        if self._driver is None:
            return aliases
        try:
            with self._driver.session() as session:
                rows = session.run(
                    "MATCH (s:Entity)-[r:ALIAS_OF]->(t:Entity) "
                    "WHERE r.active = true RETURN s.label AS source, t.label AS target"
                )
                for row in rows:
                    if row["source"] and row["target"]:
                        aliases[str(row["source"]).casefold()] = str(row["target"])
        except Exception as error:  # noqa: BLE001 — карта алиасов не роняет импорт
            logger.warning("Не удалось прочитать ALIAS_OF: %s", error)
        return aliases
