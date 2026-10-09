"""Опциональная проверка реальной модели и открытых источников.

STORMIDEA_LIVE_RESEARCH=1 python -m pytest tests_live -s
Обычная suite не обращается к платной модели. Проверка не доказывает отсутствие
семантических ошибок: опубликованный ответ дополнительно сверяется человеком.
"""

import json
import os
import re
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from scientific_tangle.agents.workflow import ResearchWorkflow
from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import QueryRequest
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from scientific_tangle.services.public_sources import PublicSourceSearch

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("STORMIDEA_LIVE_RESEARCH") != "1", reason="opt-in live model"),
]

TITLE = "Acute Caffeine Intake Does Not Modulate Active or Reactive Inhibition"


async def test_internal_public_research_and_missing_measurements(tmp_path: Path) -> None:
    settings = Settings(knowledge_backend="memory", accounts_backend="memory")
    assert settings.gigachat_api_key, "Для живой проверки нужна настроенная модель"
    fetched = await PublicSourceSearch().search(f'TITLE:"{TITLE}"')
    assert fetched.findings, "Реальная аннотация должна быть доступна"
    original = fetched.findings[0]
    imported = original.model_copy(update={
        "id": "live-source-import", "scope": {"origin": "test_import_of_real_publication"},
    })
    knowledge = InMemoryKnowledgeBase()
    knowledge.register_findings([imported])
    workflow = ResearchWorkflow(knowledge=knowledge, settings=settings,
                                checkpointer=InMemorySaver())
    questions = [
        f'Прочитай внутреннюю находку live-source-import и независимо найди её публикацию '
        f'через public_search с запросом TITLE:"{TITLE}". '
        'Ответь кратко: показало ли это исследование улучшение тормозного контроля после '
        'кофеина? Назови условия и границы вывода, со ссылками на внутреннюю находку '
        'и публикацию. Используй только прочитанные сведения.',
        'Предложи одну гипотезу для следующего исследования на основании этой публикации, '
        'и как её опровергнуть. Два коротких абзаца со ссылкой. '
        'Не придумывай измерения, размер выборки или сроки нового эксперимента.',
        'На сколько процентов кофеин повысил внимание у пользователей нашего приложения? '
        'Наших экспериментальных измерений ещё нет. Не подставляй чужие результаты '
        'вместо наших данных. Ответь одним коротким абзацем.',
    ]
    conversation = uuid4()
    answers = []
    for question in questions:
        answer = await workflow.run(QueryRequest(question=question, thread_id=conversation))
        answers.append(answer)
        (tmp_path / "live-research.json").write_text(json.dumps({
            "import_note": "Изолированный корпус с реально полученной аннотацией.",
            "original_source": original.model_dump(mode="json"),
            "answers": [item.model_dump(mode="json") for item in answers],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    assert all(not answer.limitations for answer in answers), [
        answer.limitations for answer in answers
    ]
    assert any(item.id == "live-source-import" for item in answers[0].findings)
    assert any(item.tool == "public_search" and item.status == "success"
               for item in answers[0].tool_observations)
    assert any(item.scope.get("origin") == "public_source" for item in answers[0].findings)
    assert answers[1].findings and "finding:" in answers[1].summary
    assert not re.search(r"\b\d+(?:[.,]\d+)?\s*%", answers[2].summary)
