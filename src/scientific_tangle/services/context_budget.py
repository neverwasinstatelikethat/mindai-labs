from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from scientific_tangle.config import CHARS_PER_TOKEN_RU
from scientific_tangle.domain.contracts import Finding


def to_prompt_json(model: BaseModel) -> str:
    """JSON секции промпта без экранирования не-ASCII в ``\\uXXXX``.

    ``model_dump_json`` экранирует кириллицу, и русское предложение раздувалось в
    ~8 раз: ``estimate_tokens`` считал эти экрани как текст, бюджет паниковал и
    fit_sections сбрасывал черновик/замечания. На проводке (API, чекпоинты) этот
    helper не используется — там важен канонический JSON модели.
    """
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False)


def estimate_tokens(text: str) -> int:
    """Оценка длины для GigaChat.

    Эвристика ``len/4`` рассчитана на латиницу и недооценивала русский текст
    примерно вдвое, из-за чего «бюджет» контекста не защищал от переполнения.
    """
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN_RU) + 1


@dataclass(frozen=True, slots=True)
class BudgetedContext:
    text: str
    dropped: tuple[str, ...] = ()
    truncated: bool = False


def fit_sections(
    sections: Sequence[tuple[str, str]],
    budget_tokens: int,
    *,
    keep_header_tokens: int = 64,
    protected: Collection[str] = (),
) -> BudgetedContext:
    """Собирает промпт из секций с приоритетом и укладывает его в бюджет.

    Секции идут от самых важных к наименее важным. При переполнении отбрасываются
    целые нижние секции, а не хвост текста: раньше обрезка по символам с конца
    первая лишала Reasoner/Critic/Improver именно доказательств и черновика,
    которые находились в хвосте.

    ``protected`` — метки, которым место резервируется до раскладки доказательств:
    они не отбрасываются вообще и могут только усечься (тогда это уходит в
    ``truncated``). Без резерва Critic получал [EVIDENCE] без DRAFT, а Improver —
    [EVIDENCE, DRAFT] без CRITIQUE, и цикл ревизий работал вслепую.
    """
    labelled = [(label, body.strip()) for label, body in sections if body and body.strip()]
    blocks = [f"{label}\n{body}" for label, body in labelled]
    protected_labels = set(protected)
    dropped: list[str] = []
    while blocks and estimate_tokens("\n\n".join(blocks)) > budget_tokens:
        victim = next(
            (
                index
                for index in range(len(blocks) - 1, -1, -1)
                if labelled[index][0] not in protected_labels
            ),
            None,
        )
        if victim is None or len(blocks) == 1:
            break
        dropped.append(labelled[victim][0])
        blocks.pop(victim)
        labelled.pop(victim)
    truncated = False
    if blocks and estimate_tokens("\n\n".join(blocks)) > budget_tokens:
        budget = max(budget_tokens - keep_header_tokens, 256)
        limit = int(budget * CHARS_PER_TOKEN_RU)
        blocks = ["\n\n".join(blocks)[:limit] + "\n… контекст усечён по бюджету токенов"]
        truncated = True
    return BudgetedContext(
        text="\n\n".join(blocks),
        dropped=tuple(dict.fromkeys(dropped)),
        truncated=truncated,
    )


def select_relevant(findings: Sequence[Finding], query: str, limit: int) -> list[Finding]:
    """Отбирает самые релевантные findings вместо первых N в порядке поступления.

    Порядок вставки зависел от последовательности tool-раундов, и Reasoner мог
    получить десять устаревших тезисов, тогда как лучший остался за пределами
    контекста.
    """
    tokens = {token.lower() for token in query.split() if len(token) >= 4}
    if not tokens:
        return sorted(findings, key=lambda item: item.confidence, reverse=True)[:limit]

    def relevance(finding: Finding) -> tuple[int, int, float]:
        statement = finding.statement.lower()
        evidence = " ".join(item.quote for item in finding.evidence).lower()
        hits = sum(token in statement for token in tokens)
        evidence_hits = sum(token in evidence for token in tokens)
        return (hits, evidence_hits, finding.confidence)

    return sorted(findings, key=relevance, reverse=True)[:limit]
