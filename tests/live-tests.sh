#!/usr/bin/env bash
# Одна команда вместо рецепта из README: поднял одноразовый контур -> прогнал живые
# тесты против настоящих Neo4j и Elasticsearch -> снял том. Тот же вызов стоит в
# CI (job live-stores, runner с тегом live-stores, по расписанию или рукой),
# поэтому исполнителю нужен только этот скрипт, а не знание портов и переменных.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_FILE=compose.live-test.yaml
NEO4J_PORT=45917
ES_PORT=43733

cleanup() { docker compose -f "$COMPOSE_FILE" down -v -t 10 >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "[1/3] подъём одноразового контура"
docker compose -f "$COMPOSE_FILE" up -d >/dev/null
for _ in $(seq 1 60); do
  status=$(docker compose -f "$COMPOSE_FILE" ps --format '{{.Service}} {{.Status}}' | tr '\n' ' ')
  if [[ "$status" == *healthy*healthy* ]]; then break; fi
  sleep 5
done
echo "      $status"

echo "[2/3] живые тесты: засебка только в одноразовый том (LIVE_SEED=throwaway)"
PYTHONIOENCODING=utf-8 \
KNOWLEDGE_BACKEND=neo4j \
LIVE_NEO4J_URI="bolt://127.0.0.1:${NEO4J_PORT}" \
LIVE_NEO4J_PASSWORD=live-test-password \
LIVE_ELASTICSEARCH_URL="http://127.0.0.1:${ES_PORT}" \
LIVE_SEED=throwaway \
PYTHONPATH="src;tests" python -m pytest tests/test_live_stores.py -q --tb=short -W ignore::DeprecationWarning

echo "[3/3] контур снимается traps'ом"
