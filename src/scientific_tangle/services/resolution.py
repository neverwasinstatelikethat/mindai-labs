from __future__ import annotations

from uuid import UUID

from neo4j import Driver, GraphDatabase

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import EntityMergeProposal, EntityResolutionProposal


class EntityResolutionWorkbench:
    def __init__(self, settings: Settings) -> None:
        self._proposals: dict[UUID, EntityMergeProposal] = {}
        self._driver: Driver | None = None
        if settings.knowledge_backend == "neo4j":
            self._driver = GraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_username, settings.neo4j_password),
            )

    def register(self, resolutions: list[EntityResolutionProposal]) -> None:
        for resolution in resolutions:
            if resolution.action != "link":
                continue
            proposal = EntityMergeProposal(
                source=resolution.mention,
                target=resolution.canonical_name,
                confidence=resolution.confidence,
                rationale=resolution.rationale,
            )
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
                        **proposal.model_dump(mode="json"),
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

    def review(self, proposal_id: UUID, action: str) -> EntityMergeProposal:
        proposals = {item.id: item for item in self.list_proposals()}
        current = proposals[proposal_id]
        allowed = {
            "accept": "accepted",
            "reject": "rejected",
            "revert": "reverted",
        }
        status = allowed[action]
        updated = current.model_copy(update={"status": status})
        self._proposals[proposal_id] = updated
        if self._driver:
            with self._driver.session() as session:
                session.run(
                    "MATCH (p:ResolutionProposal {id: $id}) SET p.status = $status",
                    id=str(proposal_id),
                    status=status,
                )
                if action == "accept":
                    session.run(
                        """
                        MERGE (source:Entity {label: $source})
                        ON CREATE SET source.id = 'alias-' + $id, source.type = 'material',
                                      source.confidence = $confidence, source.metadata = '{}'
                        MERGE (target:Entity {label: $target})
                        ON CREATE SET target.id = 'canonical-' + $id, target.type = 'material',
                                      target.confidence = $confidence, target.metadata = '{}'
                        MERGE (source)-[r:ALIAS_OF {proposal_id: $id}]->(target)
                        SET r.id = 'alias-edge-' + $id, r.confidence = $confidence, r.active = true
                        """,
                        id=str(proposal_id),
                        source=current.source,
                        target=current.target,
                        confidence=current.confidence,
                    )
                elif action == "revert":
                    session.run(
                        "MATCH ()-[r:ALIAS_OF {proposal_id: $id}]->() SET r.active = false",
                        id=str(proposal_id),
                    )
        return updated
