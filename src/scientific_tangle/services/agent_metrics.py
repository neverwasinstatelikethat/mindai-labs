from __future__ import annotations

import threading
from collections import defaultdict, deque

from prometheus_client import Counter, Histogram

from scientific_tangle.domain.contracts import AgentMetricSnapshot, AgentMetricsResponse

AGENT_RUNS = Counter(
    "mindai_agent_runs_total",
    "Количество запусков узлов агентного графа",
    ["agent", "status"],
)
AGENT_DURATION = Histogram(
    "mindai_agent_duration_seconds",
    "Длительность узлов агентного графа",
    ["agent"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)


class AgentMetricsRegistry:
    def __init__(self) -> None:
        self._durations: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=1000))
        self._successes: dict[str, int] = defaultdict(int)
        self._failures: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def observe(self, agent: str, duration_ms: float, success: bool) -> None:
        with self._lock:
            self._durations[agent].append(duration_ms)
            if success:
                self._successes[agent] += 1
            else:
                self._failures[agent] += 1
        status = "success" if success else "failure"
        AGENT_RUNS.labels(agent=agent, status=status).inc()
        AGENT_DURATION.labels(agent=agent).observe(duration_ms / 1000)

    def snapshot(self) -> AgentMetricsResponse:
        with self._lock:
            agents = sorted(set(self._durations) | set(self._successes) | set(self._failures))
            snapshots = [self._snapshot_agent(agent) for agent in agents]
        return AgentMetricsResponse(agents=snapshots)

    def _snapshot_agent(self, agent: str) -> AgentMetricSnapshot:
        durations = sorted(self._durations[agent])
        successes = self._successes[agent]
        failures = self._failures[agent]
        calls = successes + failures
        return AgentMetricSnapshot(
            agent=agent,
            calls=calls,
            successes=successes,
            failures=failures,
            success_rate=round(successes / calls, 3) if calls else 0,
            average_duration_ms=round(sum(durations) / len(durations), 1) if durations else 0,
            p50_duration_ms=round(self._percentile(durations, 0.5), 1),
            p95_duration_ms=round(self._percentile(durations, 0.95), 1),
        )

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0
        index = min(round((len(values) - 1) * percentile), len(values) - 1)
        return values[index]


agent_metrics = AgentMetricsRegistry()
