from __future__ import annotations

from pathlib import Path

from pyshacl import validate
from rdflib import RDF, Graph, Literal, Namespace, URIRef

from scientific_tangle.domain.contracts import ExtractionResult

MIND = Namespace("https://mindai.local/ontology/")


class OntologyValidationError(ValueError):
    pass


class OntologyValidator:
    def __init__(self) -> None:
        shape_path = Path(__file__).parents[1] / "ontology" / "shapes.ttl"
        self._shapes = Graph().parse(shape_path, format="turtle")

    def validate(self, extraction: ExtractionResult) -> None:
        known = {
            value.lower()
            for entity in extraction.entities
            for value in [entity.name, entity.canonical_name, *entity.aliases]
        }
        graph = Graph()
        for index, claim in enumerate(extraction.claims):
            if claim.subject.lower() not in known:
                raise OntologyValidationError(f"Неизвестный subject: {claim.subject}")
            node = URIRef(f"https://mindai.local/claim/{index}")
            graph.add((node, RDF.type, MIND.Claim))
            graph.add((node, MIND.predicate, Literal(claim.predicate)))
            graph.add((node, MIND.evidenceQuote, Literal(claim.evidence_quote)))
            graph.add((node, MIND.confidence, Literal(claim.confidence)))

        conforms, _, report = validate(
            data_graph=graph,
            shacl_graph=self._shapes,
            inference="rdfs",
            abort_on_first=False,
        )
        if not conforms:
            raise OntologyValidationError(str(report))
