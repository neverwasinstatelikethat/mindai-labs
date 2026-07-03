from fastapi import FastAPI

from scientific_tangle import __version__
from scientific_tangle.domain.models import QueryPlan

app = FastAPI(
    title="Научный Клубок API",
    version=__version__,
    description="Evidence-centric Agentic GraphRAG API",
)


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/queries/validate", tags=["queries"])
async def validate_query_plan(plan: QueryPlan) -> QueryPlan:
    """Проверяет типизированный план до обращения к retrieval-контуру."""
    return plan
