from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field, field_validator

from scientific_tangle.domain.contracts import Finding, GraphSnapshot
from scientific_tangle.domain.hypotheses import HypothesisSignal
from scientific_tangle.domain.intelligence import ConflictCandidate, DataClass
from scientific_tangle.domain.models import EvidenceLocator
from scientific_tangle.services.provider import ModelProvider
from scientific_tangle.services.research_intelligence import (
    ResearchIntelligenceService,
    format_range,
    to_research_claims,
)

_HYPOTHESIS_NAMESPACE = uuid5(NAMESPACE_URL, "urn:stormidea:hypothesis-signal")
_MAX_BATCH_CHARACTERS = 12_000
_MAX_SOURCE_STATEMENT = 1_000
_MAX_SOURCE_QUOTE = 3_000
_DATA_CLASS_RANK = {
    DataClass.PUBLIC: 0,
    DataClass.INTERNAL: 1,
    DataClass.RESTRICTED: 2,
}
_CONFLICT_LANGUAGE = re.compile(
    r"conflict|discrep|contradict|inconsisten|mismatch|"
    r"расхожд|противореч|несоответ|нестыков|несовпад|различ|отлич",
    re.IGNORECASE,
)


class HypothesisDraft(BaseModel):
    kind: str = Field(min_length=1, max_length=80)
    statement: str = Field(min_length=1, max_length=600)
    proposal: str | None = Field(default=None, max_length=600)
    confidence: float = Field(default=0.7, ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1)

    @field_validator("kind", "statement")
    @classmethod
    def strip_nonempty_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("значение не должно быть пустым")
        return cleaned

    @field_validator("proposal")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        cleaned = value.strip() if value is not None else None
        return cleaned or None


class _HypothesisDraftBatch(BaseModel):
    hypotheses: list[HypothesisDraft] = Field(default_factory=list)


def _asserts_numeric_conflict(draft: HypothesisDraft) -> bool:
    return bool(_CONFLICT_LANGUAGE.search(f"{draft.kind} {draft.statement}"))


def _is_source_restatement(statement: str, evidence: Sequence[EvidenceLocator]) -> bool:
    """Reject a finding that only repeats one cited source as a hypothesis."""
    statement_tokens = set(
        re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", statement).casefold())
    )
    if len(statement_tokens) < 4:
        return False
    for item in evidence:
        quote_tokens = set(
            re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", item.quote).casefold())
        )
        overlap = len(statement_tokens & quote_tokens) / len(statement_tokens)
        if overlap >= 0.9:
            return True
    return False


class HypothesisGenerator:
    """Строит сигналы из семантических находок и проверяет их ссылки."""

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider

    async def generate(
        self,
        graph: GraphSnapshot,
        findings: Sequence[Finding],
        conflicts: Sequence[ConflictCandidate],
    ) -> list[HypothesisSignal]:
        corpus = [
            finding
            for finding in findings
            if not finding.id.startswith("chunk-") and finding.superseded_by is None
        ]
        signals = self._numeric_signals(corpus, conflicts)

        evidence_records = self._evidence_records(corpus)
        numeric_pairs = self._validated_numeric_pairs(corpus, conflicts)
        for group in self._graph_groups(graph, corpus):
            for batch in self._batches(group):
                if not batch:
                    continue
                batch_evidence_ids: set[str] = set()
                for record in batch:
                    evidence_items = record.get("evidence")
                    if not isinstance(evidence_items, list):
                        continue
                    batch_evidence_ids.update(
                        evidence_id
                        for item in evidence_items
                        if isinstance(item, dict)
                        and isinstance((evidence_id := item.get("id")), str)
                    )
                batch_records = {
                    evidence_id: evidence_records[evidence_id]
                    for evidence_id in batch_evidence_ids
                    if evidence_id in evidence_records
                }
                user_text = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
                result = await self._provider.complete_model(
                    _HYPOTHESIS_SYSTEM,
                    user_text,
                    _HypothesisDraftBatch,
                )
                draft_batch = _HypothesisDraftBatch.model_validate(result)
                for draft in draft_batch.hypotheses:
                    signal = self._ground_draft(draft, batch_records, numeric_pairs)
                    if signal is not None:
                        signals.append(signal)

        unique: dict[UUID, HypothesisSignal] = {}
        for signal in signals:
            unique[signal.id] = signal
        return sorted(unique.values(), key=lambda item: (item.kind, item.statement, str(item.id)))

    @staticmethod
    def _validated_numeric_pairs(
        findings: Sequence[Finding], conflicts: Sequence[ConflictCandidate]
    ) -> set[frozenset[str]]:
        claims = to_research_claims(findings)
        by_claim = {claim.id: claim.finding_id for claim in claims}
        valid_ids = {
            item.id for item in ResearchIntelligenceService().detect_conflicts(claims)
        }
        return {
            frozenset(
                (
                    by_claim[conflict.left_claim_id],
                    by_claim[conflict.right_claim_id],
                )
            )
            for conflict in conflicts
            if conflict.id in valid_ids
            and conflict.left_claim_id in by_claim
            and conflict.right_claim_id in by_claim
        }

    @staticmethod
    def _evidence_records(
        findings: Sequence[Finding],
    ) -> dict[str, tuple[Finding, EvidenceLocator]]:
        records: dict[str, tuple[Finding, EvidenceLocator]] = {}
        for finding in findings:
            for index, locator in enumerate(finding.evidence):
                records[f"{finding.id}:{index}"] = (finding, locator)
        return records

    @staticmethod
    def _graph_groups(
        graph: GraphSnapshot, findings: Sequence[Finding]
    ) -> list[list[Finding]]:
        """Группирует находки по связным компонентам/сообществам графа."""
        if not findings:
            return []
        finding_ids = {finding.id for finding in findings}
        parent = {finding_id: finding_id for finding_id in finding_ids}

        def root(item: str) -> str:
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        def union(left: str, right: str) -> None:
            first, second = root(left), root(right)
            if first != second:
                parent[max(first, second)] = min(first, second)

        node_to_finding: dict[str, str] = {}
        community_members: dict[str, list[str]] = defaultdict(list)
        graph_nodes = {node.id: node for node in graph.nodes}
        for finding in findings:
            aliases = {
                finding.id,
                finding.id.removeprefix("finding-"),
                f"claim-{finding.id.removeprefix('finding-')}",
            }
            for node_id in aliases:
                node = graph_nodes.get(node_id)
                if node is None:
                    continue
                node_to_finding[node_id] = finding.id
                community = node.metadata.get("community")
                if isinstance(community, str) and community:
                    community_members[community].append(finding.id)

        adjacency: dict[str, set[str]] = defaultdict(set)
        for edge in graph.edges:
            adjacency[edge.source].add(edge.target)
            adjacency[edge.target].add(edge.source)
        visited: set[str] = set()
        for node_id in node_to_finding:
            if node_id in visited:
                continue
            stack = [node_id]
            component_findings: set[str] = set()
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                if current in node_to_finding:
                    component_findings.add(node_to_finding[current])
                stack.extend(adjacency[current] - visited)
            ordered_component = sorted(component_findings)
            for item in ordered_component[1:]:
                union(ordered_component[0], item)

        for members in community_members.values():
            for other in members[1:]:
                union(members[0], other)

        grouped: dict[tuple[str, DataClass], list[Finding]] = defaultdict(list)
        for finding in sorted(findings, key=lambda item: item.id):
            # Данные разных ACL-классов никогда не входят в один prompt: иначе
            # модель может перенести restricted-контекст в public-сигнал, даже
            # если укажет только открытую цитату.
            grouped[(root(finding.id), finding.data_class)].append(finding)
        return list(grouped.values())

    @staticmethod
    def _batches(group: Sequence[Finding]) -> list[list[dict[str, object]]]:
        batches: list[list[dict[str, object]]] = []
        current: list[dict[str, object]] = []
        current_size = 2
        for finding in sorted(group, key=lambda item: item.id):
            locators = list(enumerate(finding.evidence))
            for start in range(0, len(locators), 4):
                evidence = [
                    {
                        "id": f"{finding.id}:{index}",
                        "document_id": str(locator.document_id),
                        "source_title": locator.source_title[:500],
                        "page": locator.page,
                        "sheet": locator.sheet,
                        "cell_range": locator.cell_range,
                        "quote": locator.quote[:_MAX_SOURCE_QUOTE],
                    }
                    for index, locator in locators[start : start + 4]
                ]
                record: dict[str, object] = {
                    "finding_id": finding.id,
                    "statement": finding.statement[:_MAX_SOURCE_STATEMENT],
                    "evidence": evidence,
                }
                size = len(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                if current and current_size + size > _MAX_BATCH_CHARACTERS:
                    batches.append(current)
                    current = []
                    current_size = 2
                current.append(record)
                current_size += size + 1
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _ground_draft(
        draft: HypothesisDraft,
        records: dict[str, tuple[Finding, EvidenceLocator]],
        numeric_pairs: set[frozenset[str]],
    ) -> HypothesisSignal | None:
        if any(evidence_id not in records for evidence_id in draft.evidence_ids):
            return None
        cited = [records[evidence_id] for evidence_id in dict.fromkeys(draft.evidence_ids)]
        if not cited:
            return None
        evidence = _unique_locators([locator for _, locator in cited])
        if _is_source_restatement(draft.statement, evidence):
            return None
        if draft.kind == "cross_source_conflict":
            if len({item.document_id for item in evidence}) < 2:
                return None
        # Numerical расхождения публикуются только из детерминированного
        # ResearchIntelligenceService; модель не выбирает числа и пары сама.
        if draft.kind == "numeric_discrepancy":
            return None
        cited_findings = [finding for finding, _ in cited]
        if _asserts_numeric_conflict(draft) and len(cited_findings) > 1:
            observed = [finding for finding in cited_findings if finding.observations]
            if len(observed) > 1 and not any(
                frozenset((left.id, right.id)) in numeric_pairs
                for index, left in enumerate(observed)
                for right in observed[index + 1 :]
            ):
                return None
        return _make_signal(
            kind=draft.kind,
            statement=draft.statement.strip(),
            proposal=draft.proposal.strip() if draft.proposal and draft.proposal.strip() else None,
            confidence=min(draft.confidence, *(item.confidence for item in cited_findings)),
            findings=cited_findings,
            evidence=evidence,
        )

    @staticmethod
    def _numeric_signals(
        findings: Sequence[Finding], conflicts: Sequence[ConflictCandidate]
    ) -> list[HypothesisSignal]:
        claims = to_research_claims(findings)
        by_claim = {claim.id: claim for claim in claims}
        valid_conflict_ids = {
            item.id for item in ResearchIntelligenceService().detect_conflicts(claims)
        }
        by_finding = {finding.id: finding for finding in findings}
        signals: list[HypothesisSignal] = []
        for conflict in conflicts:
            if conflict.id not in valid_conflict_ids:
                continue
            left_claim = by_claim.get(conflict.left_claim_id)
            right_claim = by_claim.get(conflict.right_claim_id)
            if left_claim is None or right_claim is None:
                continue
            left = by_finding.get(left_claim.finding_id)
            right = by_finding.get(right_claim.finding_id)
            if left is None or right is None:
                continue
            if right.id < left.id:
                left, right = right, left
                left_claim, right_claim = right_claim, left_claim
            evidence = _unique_locators([*left.evidence, *right.evidence])
            if len(evidence) < 2:
                continue
            statement = (
                f"Проверить расхождение показателя: {format_range(left_claim.value)} "
                f"по данным «{_source_title(left)}», {format_range(right_claim.value)} "
                f"по данным «{_source_title(right)}»."
            )
            statement = statement[:597].rstrip() + "…" if len(statement) > 600 else statement
            signals.append(
                _make_signal(
                    kind="numeric_discrepancy",
                    statement=statement,
                    proposal="Сверить период, методику и условия измерения в обоих источниках.",
                    confidence=min(left.confidence, right.confidence),
                    findings=[left, right],
                    evidence=evidence,
                )
            )
        return signals


def _source_title(finding: Finding) -> str:
    """Заголовок источника для текста гипотезы.

    Имя свойства из онтологии (``salt_rejection``) в предложение не попадает:
    это служебный ключ, его переводит экран по своему словарю подписей. В фразе,
    которую читает аналитик, остаются значение и название источника — по ним
    расхождение и находится.
    """
    for item in finding.evidence:
        title = item.source_title.strip()
        if title:
            return title[:80]
    return "источник без названия"


def _unique_locators(evidence: Sequence[EvidenceLocator]) -> list[EvidenceLocator]:
    unique: dict[str, EvidenceLocator] = {}
    for item in evidence:
        unique[_locator_key(item)] = item
    return list(unique.values())


def _locator_key(item: EvidenceLocator) -> str:
    return json.dumps(
        {
            "document_id": str(item.document_id),
            "page": item.page,
            "sheet": item.sheet,
            "cell_range": item.cell_range,
            "char_start": item.char_start,
            "char_end": item.char_end,
            "quote": unicodedata.normalize("NFKC", item.quote),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _make_signal(
    *,
    kind: str,
    statement: str,
    proposal: str | None,
    confidence: float,
    findings: Sequence[Finding],
    evidence: list[EvidenceLocator],
) -> HypothesisSignal:
    normalized_statement = re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", statement)
    ).strip().casefold()
    evidence_keys = sorted({_locator_key(item) for item in evidence})
    identity = json.dumps(
        {"statement": normalized_statement, "evidence": evidence_keys},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    sensitivity = max((item.data_class for item in findings), key=_DATA_CLASS_RANK.__getitem__)
    return HypothesisSignal(
        id=uuid5(_HYPOTHESIS_NAMESPACE, identity),
        kind=kind,
        statement=statement,
        proposal=proposal,
        confidence=confidence,
        data_class=sensitivity,
        evidence=evidence,
    )


_HYPOTHESIS_SYSTEM = """Ты — аналитик, который ищет полезные проверяемые выводы в фактах и цитатах.
Входные statement — атомарные факты корпуса, а не готовые гипотезы. Не переносить
их в ответ дословно или в виде краткого пересказа. Гипотеза должна добавлять
аналитический шаг: показать несостыковку, возможное следствие, ограничение,
узкое место, скрытую зависимость, пробел или конкретную возможность улучшения.
Допустимы как сопоставления нескольких документов, так и осторожные идеи по одному
источнику, если прямо указать, что именно в тексте наводит на идею и чего пока
нельзя утверждать. Не делай вывод о дефиците или проблеме только из одной цифры.

В statement сформулируй именно аналитический вывод, а не факт из источника.
В proposal укажи конкретный следующий шаг, который может подтвердить или
опровергнуть вывод; не используй общие фразы вроде «изучить вопрос».
Полезный пример: факт «в лаборатории 18 сотрудников, из них 3 кандидата наук»
сам по себе не гипотеза. Аналитический сигнал может предложить сопоставить
квалификацию с составом задач лаборатории, если другие приведённые сведения
описывают эти задачи; без такого основания сигнал не создавай.
При сравнении источников проверь, сопоставимы ли периоды, определения и условия.
Если данные этого не позволяют, называй это вопросом для проверки, не доказанным
противоречием.

Категория свободная; используй точный короткий тип, например discrepancy,
improvement_opportunity, bottleneck, information_gap, dependency, risk или иной
подходящий тип. Не пересказывай биографии, справочные списки, заголовки,
учебные цели и отдельные факты. Каждый вывод должен ссылаться на переданные
evidence id. Не добавляй источники, страницы или цитаты; используй ID дословно.
Если после анализа данных действительно нет, верни пустой список. Пиши кратко,
осторожно и полезно для дальнейшей проверки."""
