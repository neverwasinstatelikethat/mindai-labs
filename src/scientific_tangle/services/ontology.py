"""SHACL-проверка результата извлечения перед записью в граф.

Словари сигнатуры живут в ``ontology/shapes.ttl`` и обязаны совпадать с реестром
отношений и ``NodeType``: предикат вне словаря, принятый молча, превращается в ребро
графа, которое allowlist обхода не пересечёт (``domain/relations.py``).
"""

from __future__ import annotations

from pathlib import Path

from pyshacl import validate
from rdflib import RDF, Graph, Literal, Namespace, URIRef
from rdflib.term import Node

from scientific_tangle.domain.contracts import ExtractionResult, NodeType
from scientific_tangle.domain.relations import relations_for_extraction

MIND = Namespace("https://mindai.local/ontology/")
SH = Namespace("http://www.w3.org/ns/shacl#")

# В текст повторного вызова уходит ограниченное число нарушений: модель обязана
# увидеть, ЧТО именно не так, но не получать простыню на каждый неверный claim.
MAX_REPORTED_VIOLATIONS = 8


class OntologyValidationError(ValueError):
    pass


class OntologyValidator:
    def __init__(self) -> None:
        shape_path = Path(__file__).parents[1] / "ontology" / "shapes.ttl"
        self._shapes = Graph().parse(shape_path, format="turtle")

    def validate(self, extraction: ExtractionResult) -> None:
        """Проверяет извлечение; текст нарушения читает repair-промпт импорта."""
        known = {
            value.lower()
            for entity in extraction.entities
            for value in [entity.name, entity.canonical_name, *entity.aliases]
        }
        graph = Graph()
        for index, entity in enumerate(extraction.entities):
            node = URIRef(f"https://mindai.local/entity/{index}")
            graph.add((node, RDF.type, MIND.Entity))
            graph.add((node, MIND.entityType, Literal(entity.type.value)))
        for index, claim in enumerate(extraction.claims):
            if claim.subject.lower() not in known:
                raise OntologyValidationError(f"Неизвестный subject: {claim.subject}")
            if claim.object.lower() not in known:
                raise OntologyValidationError(f"Неизвестный object: {claim.object}")
            node = URIRef(f"https://mindai.local/claim/{index}")
            graph.add((node, RDF.type, MIND.Claim))
            graph.add((node, MIND.predicate, Literal(claim.predicate)))
            graph.add((node, MIND.evidenceQuote, Literal(claim.evidence_quote)))
            graph.add((node, MIND.confidence, Literal(claim.confidence)))

        conforms, report, _ = validate(
            data_graph=graph,
            shacl_graph=self._shapes,
            inference="rdfs",
            abort_on_first=False,
        )
        if not conforms:
            raise OntologyValidationError(_describe(report))


# Компоненты SHACL, которые мы умеем объяснять модели по-русски и коротко: текст
# отчёта pySHACL перечисляет весь словарь заново и раздувает repair-промпт.
CONSTRAINT_HINTS: dict[str, str] = {
    "InConstraintComponent": "значения нет в словаре подписи",
    "PatternConstraintComponent": "значение не соответствует шаблону",
    "MinCountConstraintComponent": "обязательное свойство отсутствует",
    "MaxCountConstraintComponent": "повторяющееся значение",
    "DatatypeConstraintComponent": "неверный тип значения",
    "MinInclusiveConstraintComponent": "значение ниже разрешённой границы",
    "MaxInclusiveConstraintComponent": "значение выше разрешённой границы",
    "MinLengthConstraintComponent": "пустое значение",
}


def _describe(report: object) -> str:
    """Компактное русское описание нарушений + словарь, которым они ограничены.

    Именно этот текст уходит в повторный вызов модели (``services/ingestion.py``),
    поэтому «нарушение есть» бесполезно — модель должна видеть поле и значение.
    """
    results = _violations(report)
    if not results:
        return str(report)[:2000]
    lines = sorted(
        f"{item['path'].rsplit('/', 1)[-1]}={item['value'] or '<нет>'}: {item['message']}"
        for item in results[:MAX_REPORTED_VIOLATIONS]
    )
    hidden = len(results) - MAX_REPORTED_VIOLATIONS
    if hidden > 0:
        lines.append(f"и ещё {hidden} нарушений того же рода")
    paths = {item["path"] for item in results}
    hints: list[str] = []
    if f"{MIND}predicate" in paths:
        hints.append(f"Допустимые предикаты: {relations_for_extraction()}")
    if f"{MIND}entityType" in paths:
        hints.append("Допустимые типы сущностей: " + ", ".join(item.value for item in NodeType))
    return "; ".join(lines) + (". " + ". ".join(hints) if hints else "")


def _violations(report: object) -> list[dict[str, str]]:
    """Извлекает sh:ValidationResult из отчёта pySHACL в плоский вид."""
    graph = report if isinstance(report, Graph) else getattr(report, "graph", None)
    if not isinstance(graph, Graph):
        return []
    found: list[dict[str, str]] = []
    for result in graph.subjects(RDF.type, SH.ValidationResult):
        component = _text(graph, result, SH.sourceConstraintComponent).rsplit("#", 1)[-1]
        found.append(
            {
                "path": _text(graph, result, SH.resultPath),
                "value": _text(graph, result, SH.value),
                "message": CONSTRAINT_HINTS.get(component) or _clip(
                    _text(graph, result, SH.resultMessage)
                ),
            }
        )
    return found


def _clip(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _text(graph: Graph, subject: Node, predicate: URIRef) -> str:
    values = [str(item) for item in graph.objects(subject, predicate)]
    return ", ".join(values)
